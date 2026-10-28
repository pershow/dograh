"""Reuse upstream connections within one job, never across tenant/task scopes."""

import asyncio
import contextlib
import hashlib
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx

from api.services.agent_workbench.errors import WorkbenchError

_clients = ContextVar("workbench_model_clients", default=None)
_health = ContextVar("workbench_model_health", default=None)


class ModelHealth:
    """Stop a task-wide outage from turning every remaining case into an error.

    Count exhausted requests, not their internal retries. A successful request
    resets only its own connection; unrelated models cannot conceal an outage.
    Once tripped, the task must be resumed explicitly with its saved evidence.
    """

    def __init__(self):
        self.failures = {}
        self.unavailable = False
        self.reason = ""

    def record(self, connection, code=None, detail=None):
        if code is None:
            self.failures.pop(connection, None)
        elif code in {
            "model_connection_error",
            "model_timeout",
            "rate_limited",
            "provider_http_error",
            "provider_client_restricted",
        }:
            self.failures[connection] = self.failures.get(connection, 0) + 1
            if self.failures[connection] >= 3 and not self.unavailable:
                self.unavailable = True
                self.reason = detail or code

    def check(self):
        if self.unavailable:
            raise WorkbenchError(
                "同一模型连接连续三次请求失败，已停止本次测试，"
                "避免继续消耗剩余用例。已保存实际对话和评审结果；"
                "恢复连接后可继续原任务，不必重跑已完成的有效结果。"
                + f" 最近原因：{self.reason}",
                503,
            )


def record_model_outcome(config, code=None, *, detail=None):
    if (health := _health.get()) is not None:
        from api.services.agent_workbench.transport_diagnostics import safe_message

        secret = getattr(config, "api_key", None) or ""
        if hasattr(secret, "get_secret_value"):
            secret = secret.get_secret_value()
        connection = (
            getattr(config, "provider", None),
            getattr(config, "base_url", None),
            getattr(config, "model", None),
            hashlib.sha256(str(secret).encode()).digest(),
        )
        health.record(
            connection,
            code,
            safe_message(
                f"{getattr(config, 'model', 'unknown')}: {detail or code}",
                (str(secret),),
            ),
        )


def check_model_availability():
    if (health := _health.get()) is not None:
        health.check()


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
    health_token = _health.set(ModelHealth())
    try:
        yield
    finally:
        _clients.reset(token)
        _health.reset(health_token)
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
