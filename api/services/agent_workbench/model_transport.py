"""Reuse upstream connections within one job, never across tenant/task scopes."""

import asyncio
import contextlib
import hashlib
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx

_clients = ContextVar("workbench_model_clients", default=None)


async def finish_response(lines):
    """Drain only a bounded footer after a validated model completion.

    Reaching HTTP EOF lets HTTPX reuse the socket. Some providers keep their SSE
    stream open after completion; that must not delay or invalidate a valid result.
    """
    with contextlib.suppress(httpx.HTTPError, TimeoutError):
        async with asyncio.timeout(0.5):
            size = 0
            async for line in lines:
                size += len(line.encode("utf-8"))
                if size > 65536:
                    break


@asynccontextmanager
async def model_transport_scope():
    clients = {}
    token = _clients.set(clients)
    try:
        yield
    finally:
        _clients.reset(token)
        await asyncio.gather(*(client.aclose() for client in clients.values()))


def model_http_client(config, *, idle_timeout, trust_env, proxy):
    """Return (client, locally_owned); the job closes borrowed clients together.

    Origins, credentials and routing/timeout policies isolate pools and cookie
    jars. Headers and model/tool metadata remain per request in ModelGateway.
    """
    clients = _clients.get()
    key = (
        getattr(config, "base_url", None),
        hashlib.sha256((getattr(config, "api_key", None) or "").encode()).digest(),
        idle_timeout,
        trust_env,
        proxy,
    )
    if clients is not None and key in clients:
        return clients[key], False
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(idle_timeout, connect=15),
        trust_env=trust_env,
        proxy=proxy,
        limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60),
    )
    if clients is not None:
        clients[key] = client
    return client, clients is None
