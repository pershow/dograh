"""Reuse Jev connections across short-lived text turns, without sharing dialogue."""

import asyncio
import hashlib

from loguru import logger

from pipecat.classifiers.base_classifier import ClassifierError
from pipecat.classifiers.jev.client import JevClient

_IDLE_SECONDS = 240
_clients = {}
_closing = set()


class _Connection:
    def __init__(self, key, api_key, model):
        self.key = key
        self.client = JevClient(api_key=api_key, model=model, timeout=10, max_retries=0)
        self.users = 0
        self.idle_timer = None
        # Owned by the pool, not the turn's task manager: cancelling a decision
        # or finishing the opening turn must not cancel connection preparation.
        self.warmup = asyncio.create_task(self._warmup(), name="jev-connect")

    async def _warmup(self):
        try:
            async with asyncio.timeout(10):
                await self.client.connect()
        except (ClassifierError, TimeoutError) as error:
            logger.warning("Jev connection warmup failed: {}", type(error).__name__)

    async def ready(self):
        await asyncio.shield(self.warmup)
        # A failed warmup may be transient. connect() is a no-op on success;
        # on failure this retry stays inside the caller's decision deadline.
        await self.client.connect()

    async def close(self):
        if self.idle_timer is not None:
            self.idle_timer.cancel()
        self.warmup.cancel()
        await asyncio.gather(self.warmup, return_exceptions=True)
        await self.client.close()


def acquire_jev_connection(api_key, model):
    loop = asyncio.get_running_loop()
    key = (loop, hashlib.sha256(api_key.encode()).digest(), model)
    connection = _clients.get(key)
    if connection is None:
        connection = _clients[key] = _Connection(key, api_key, model)
    if connection.idle_timer is not None:
        connection.idle_timer.cancel()
        connection.idle_timer = None
    connection.users += 1
    return connection


def release_jev_connection(connection):
    connection.users -= 1
    if connection.users == 0 and _clients.get(connection.key) is connection:
        connection.idle_timer = asyncio.get_running_loop().call_later(
            _IDLE_SECONDS, _expire, connection
        )


def _expire(connection):
    if connection.users or _clients.get(connection.key) is not connection:
        return
    del _clients[connection.key]
    task = asyncio.create_task(connection.close(), name="jev-close")
    _closing.add(task)
    task.add_done_callback(_closing.discard)


async def close_jev_connections():
    """Close this worker's connections after its accepted turns have drained."""
    loop = asyncio.get_running_loop()
    connections = [c for key, c in list(_clients.items()) if key[0] is loop]
    for connection in connections:
        del _clients[connection.key]
    await asyncio.gather(*(c.close() for c in connections))
    await asyncio.gather(*(t for t in list(_closing) if t.get_loop() is loop))
