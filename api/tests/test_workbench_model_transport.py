import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web

from api.services.agent_workbench.model_gateway import ModelGateway
from api.services.agent_workbench.model_transport import model_transport_scope


def gateway(url, key="first", **kwargs):
    return ModelGateway(
        SimpleNamespace(base_url=url, api_key=key, model="test"),
        [],
        trust_env=False,
        **kwargs,
    )


async def test_separate_decisions_reuse_upstream_socket_but_not_credentials_or_cookies(
    aiohttp_server,
):
    connections, cookies = [], []

    async def upstream(request):
        connections.append(request.transport)
        cookies.append(request.cookies.get("session"))
        response = web.Response(text="ok")
        response.set_cookie("session", "private-session")
        return response

    app = web.Application()
    app.router.add_get("/probe", upstream)
    server = await aiohttp_server(app)
    url = str(server.make_url("/probe"))
    async with model_transport_scope():
        async with gateway(url) as first:
            assert (await first.client.get(url)).text == "ok"
            client = first.client
        assert not client.is_closed
        async with gateway(url) as second:
            await second.client.get(url)
            assert second.client is client
        async with gateway(url, key="different-credential") as other:
            await other.client.get(url)
            assert other.client is not client
        assert connections[0] is connections[1]
        assert connections[2] is not connections[0]
        assert cookies == [None, "private-session", None]
    assert client.is_closed and other.client.is_closed


async def test_concurrent_jobs_never_share_clients_and_cancellation_closes_them():
    clients = []
    ready = asyncio.Event()

    async def job():
        async with (
            model_transport_scope(),
            gateway("https://model.example/v1") as current,
        ):
            clients.append(current.client)
            if len(clients) == 2:
                ready.set()
            await asyncio.Event().wait()

    tasks = [asyncio.create_task(job()) for _ in range(2)]
    await asyncio.wait_for(ready.wait(), 2)
    assert clients[0] is not clients[1]
    for task in tasks:
        task.cancel()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "different", [{"idle_timeout": 30}, {"proxy": "http://localhost:8989"}]
)
async def test_connection_policy_changes_do_not_reuse_an_existing_pool(different):
    async with (
        model_transport_scope(),
        gateway("https://model.example/v1") as first,
        gateway("https://model.example/v1", **different) as second,
    ):
        assert first.client is not second.client


@pytest.mark.parametrize("protocol", ["responses", "chat"])
async def test_completed_sse_calls_reuse_the_socket_across_gateways(
    aiohttp_server, protocol
):
    connections = []

    async def upstream(request):
        connections.append(request.transport)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        event = (
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": []},
            }
            if protocol == "responses"
            else {
                "choices": [
                    {"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}
                ]
            }
        )
        await response.write(("data: " + json.dumps(event) + "\n\n").encode())
        await asyncio.sleep(0.01)
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    app.router.add_post("/v1/chat/completions", upstream)
    server = await aiohttp_server(app)
    async with model_transport_scope(), httpx.AsyncClient(trust_env=False) as local:
        for _ in range(2):
            async with gateway(
                str(server.make_url("/v1")), protocol=protocol
            ) as current:
                response = await local.post(
                    current.url + "/responses",
                    headers={"Authorization": "Bearer " + current.token},
                    json={},
                )
                assert response.status_code == 200
                assert '"status": "completed"' in response.text
    assert len(connections) == 2
    assert connections[0] is connections[1]


async def test_completion_does_not_wait_for_a_provider_that_never_closes_its_stream():
    from api.services.agent_workbench.model_transport import finish_response

    async def never_ends():
        await asyncio.Event().wait()
        yield ""

    await asyncio.wait_for(finish_response(never_ends()), timeout=1)


async def test_outage_is_task_scoped_and_other_connections_cannot_reset_it():
    from api.services.agent_workbench.errors import WorkbenchError
    from api.services.agent_workbench.model_transport import (
        check_model_availability,
        record_model_outcome,
    )

    failing = SimpleNamespace(
        base_url="https://one.example", api_key="first", model="m"
    )
    healthy = SimpleNamespace(
        base_url="https://one.example", api_key="other", model="m"
    )
    async with model_transport_scope():
        for _ in range(2):
            record_model_outcome(failing, "model_connection_error")
            record_model_outcome(healthy)
            check_model_availability()
        # Invalid structured output isn't evidence that the connection is down.
        record_model_outcome(failing, "model_invalid_tool_arguments")
        check_model_availability()
        record_model_outcome(failing, "model_connection_error")
        with pytest.raises(WorkbenchError, match="已停止本次测试"):
            check_model_availability()
        # The interruption is sticky until a new task; late successes do not
        # race with cancellation to relabel an incomplete task as successful.
        record_model_outcome(failing)
        with pytest.raises(WorkbenchError):
            check_model_availability()
        async with model_transport_scope():
            check_model_availability()
        with pytest.raises(WorkbenchError):
            check_model_availability()
    check_model_availability()


async def test_success_on_same_connection_resets_consecutive_failure_count():
    from api.services.agent_workbench.model_transport import (
        check_model_availability,
        record_model_outcome,
    )

    config = SimpleNamespace(base_url="https://one.example", api_key="first", model="m")
    async with model_transport_scope():
        for _ in range(4):
            record_model_outcome(config, "model_timeout")
            record_model_outcome(config, "model_timeout")
            check_model_availability()
            record_model_outcome(config)


async def test_gateway_outage_counts_exhausted_requests_not_retry_attempts(
    aiohttp_server,
):
    from api.services.agent_workbench.errors import WorkbenchError
    from api.services.agent_workbench.model_transport import check_model_availability

    attempts = 0

    async def unavailable(request):
        nonlocal attempts
        attempts += 1
        return web.Response(status=503)

    app = web.Application()
    app.router.add_post("/v1/responses", unavailable)
    server = await aiohttp_server(app)
    async with model_transport_scope(), httpx.AsyncClient(trust_env=False) as local:
        for index in range(3):
            async with gateway(
                str(server.make_url("/v1")), protocol="responses", max_retries=1
            ) as current:
                response = await local.post(
                    current.url + "/responses",
                    headers={"Authorization": "Bearer " + current.token},
                    json={},
                )
                assert response.status_code == 502
            assert attempts == (index + 1) * 2
            if index < 2:
                check_model_availability()
        with pytest.raises(WorkbenchError, match="连续三次"):
            check_model_availability()
