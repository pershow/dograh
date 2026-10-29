"""API reachability is independent of optional public-tunnel discovery."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from api import constants
from api.routes.main import health
from api.utils import tunnel


@pytest.fixture
def cache(monkeypatch):
    cache = tunnel.TunnelURLCache()
    monkeypatch.setattr(tunnel, "tunnel_url_cache", cache)
    monkeypatch.setattr(constants, "BACKEND_API_ENDPOINT", "http://127.0.0.1:8000")
    return cache


@pytest.fixture
async def client():
    app = FastAPI()
    app.get("/health")(health)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_health_returns_during_stalled_discovery(client, cache, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stalled_probe():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    probe = AsyncMock(side_effect=stalled_probe)
    monkeypatch.setattr(tunnel.TunnelURLProvider, "get_tunnel_urls", probe)
    monkeypatch.setattr(constants, "AUTH_PROVIDER", "stack")
    monkeypatch.setattr(constants, "STACK_AUTH_PROJECT_ID", "public-project")
    monkeypatch.setattr(constants, "STACK_PUBLISHABLE_CLIENT_KEY", "public-client-key")
    cache.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        # Concurrent health traffic must neither await nor multiply probes.
        responses = await asyncio.wait_for(
            asyncio.gather(*(client.get("/health") for _ in range(20))), timeout=1
        )
        for response in responses:
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert data["version"] == constants.APP_VERSION
            assert data["backend_api_endpoint"] == "http://127.0.0.1:8000"
            assert data["tunnel_url"] is None
            assert data["auth_provider"] == "stack"
            assert data["stack_project_id"] == "public-project"
            assert data["stack_publishable_client_key"] == "public-client-key"
        probe.assert_awaited_once()
    finally:
        await asyncio.wait_for(cache.stop(), timeout=1)
    assert cancelled.is_set()
    assert cache.get_url() is None


async def test_cold_health_read_never_starts_network_io(client, cache, monkeypatch):
    probe = AsyncMock(side_effect=AssertionError("Health must not discover tunnels"))
    monkeypatch.setattr(tunnel.TunnelURLProvider, "get_tunnel_urls", probe)
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["tunnel_url"] is None
    probe.assert_not_called()


@pytest.mark.parametrize("failure", [TimeoutError(), OSError("DNS failed"), None])
async def test_tunnel_loss_clears_url_and_recovers(client, cache, monkeypatch, failure):
    requests = asyncio.Queue()

    async def controlled_probe():
        result = asyncio.get_running_loop().create_future()
        requests.put_nowait(result)
        return await result

    monkeypatch.setattr(tunnel.TunnelURLProvider, "get_tunnel_urls", controlled_probe)
    monkeypatch.setattr(cache, "REFRESH_INTERVAL", 0)
    cache.start()
    try:
        pending = await asyncio.wait_for(requests.get(), timeout=1)
        for result, expected_url in [
            (
                ("https://first.example.com", "wss://first.example.com"),
                "https://first.example.com",
            ),
            (failure, None),
            (
                ("https://replacement.example.com", "wss://replacement.example.com"),
                "https://replacement.example.com",
            ),
        ]:
            if isinstance(result, Exception):
                pending.set_exception(result)
            else:
                pending.set_result(result)
            # Starting the next discovery proves the previous result is visible.
            pending = await asyncio.wait_for(requests.get(), timeout=1)
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json()["tunnel_url"] == expected_url
    finally:
        await cache.stop()
    assert pending.cancelled()


async def test_stalled_refresh_cannot_advertise_expired_tunnel(
    client, cache, monkeypatch
):
    requests = asyncio.Queue()
    clock = [100.0]

    async def controlled_probe():
        result = asyncio.get_running_loop().create_future()
        requests.put_nowait(result)
        return await result

    monkeypatch.setattr(tunnel, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tunnel.TunnelURLProvider, "get_tunnel_urls", controlled_probe)
    monkeypatch.setattr(cache, "REFRESH_INTERVAL", 0)
    cache.start()
    try:
        first = await asyncio.wait_for(requests.get(), timeout=1)
        first.set_result(("https://old.example.com", "wss://old.example.com"))
        stalled = await asyncio.wait_for(requests.get(), timeout=1)
        assert (await client.get("/health")).json()[
            "tunnel_url"
        ] == "https://old.example.com"
        clock[0] += cache.MAX_AGE
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["tunnel_url"] is None
        assert not stalled.done()
    finally:
        await cache.stop()


@pytest.mark.parametrize(
    "endpoint,cached_url,expected_url",
    [
        ("https://api.example.com", "https://tunnel.example.com", None),
        (
            "http://10.2.3.4:8000",
            "https://tunnel.example.com",
            "https://tunnel.example.com",
        ),
        (
            "http://100.64.0.1:8000",
            "https://tunnel.example.com",
            "https://tunnel.example.com",
        ),
        ("http://localhost:8000", "http://127.0.0.1:8000", None),
    ],
)
async def test_health_preserves_public_endpoint_semantics(
    client, cache, monkeypatch, endpoint, cached_url, expected_url
):
    monkeypatch.setattr(constants, "BACKEND_API_ENDPOINT", endpoint)
    monkeypatch.setattr(cache, "get_url", lambda: cached_url)
    data = (await client.get("/health")).json()
    assert data["backend_api_endpoint"] == endpoint
    assert data["tunnel_url"] == expected_url


async def test_refresh_starts_only_once_and_can_restart(cache, monkeypatch):
    started = asyncio.Queue()
    cancelled = []

    async def probe():
        started.put_nowait(True)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    mock = AsyncMock(side_effect=probe)
    monkeypatch.setattr(tunnel.TunnelURLProvider, "get_tunnel_urls", mock)
    for _ in range(2):
        cache.start()
        cache.start()
        await asyncio.wait_for(started.get(), timeout=1)
        await cache.stop()
        await cache.stop()
    assert mock.await_count == 2
    assert len(cancelled) == 2


@pytest.mark.parametrize(
    "endpoint,should_discover",
    [
        ("http://localhost:8000", True),
        ("http://192.168.1.5", True),
        ("https://api.example.com", False),
    ],
)
async def test_app_lifespan_owns_discovery(monkeypatch, endpoint, should_discover):
    import api.app as app_module
    from api.services.observability import loop_exceptions, loop_lag, metrics
    from api.services.observability.call_events import delivery
    from api.services.pipecat import jev_client_pool
    from api.services.workflow import text_chat_stream

    @asynccontextmanager
    async def mcp_lifespan(_app):
        yield

    cache = Mock(start=Mock(), stop=AsyncMock())
    sync = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(app_module, "BACKEND_API_ENDPOINT", endpoint)
    monkeypatch.setattr(app_module, "tunnel_url_cache", cache)
    monkeypatch.setattr(app_module, "mcp_app", Mock(lifespan=mcp_lifespan))
    monkeypatch.setattr(app_module, "get_arq_redis", AsyncMock())
    monkeypatch.setattr(app_module, "load_all_org_langfuse_credentials", AsyncMock())
    monkeypatch.setattr(app_module, "WorkerSyncManager", Mock(return_value=sync))
    monkeypatch.setattr(app_module, "set_worker_sync_manager", Mock())
    monkeypatch.setattr(app_module, "close_speech_cache", AsyncMock())
    monkeypatch.setattr(jev_client_pool, "close_jev_connections", AsyncMock())
    monkeypatch.setattr(
        text_chat_stream, "finish_streaming_text_chat_turns", AsyncMock()
    )
    for module in (delivery, metrics, loop_lag):
        monkeypatch.setattr(module, "start", Mock())
    monkeypatch.setattr(delivery, "shutdown", AsyncMock())
    monkeypatch.setattr(metrics, "stop", Mock())
    monkeypatch.setattr(loop_lag, "stop", AsyncMock())
    monkeypatch.setattr(loop_exceptions, "install", Mock())

    # Cleanup must also run when the application exits due to an error.
    with pytest.raises(RuntimeError, match="application stopped"):
        async with app_module.lifespan(app_module.app):
            assert cache.start.call_count == int(should_discover)
            cache.stop.assert_not_awaited()
            raise RuntimeError("application stopped")
    cache.stop.assert_awaited_once()
