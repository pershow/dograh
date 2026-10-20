"""The Codex model gateway must preserve tool semantics in both configured modes."""

from types import SimpleNamespace

import httpx
import jsonschema
import pytest

from api.schemas.agent_workbench import SimulationSuite
from api.services.agent_workbench.model_gateway import (
    ModelGateway,
    chat_messages,
)
from api.services.agent_workbench.tools import WorkbenchTools


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_required_completion_tool_is_scoped_to_one_request(protocol):
    import json

    seen = []

    def upstream(request):
        seen.append(json.loads(request.content))
        if protocol == "responses":
            return httpx.Response(
                200, json={"status": "completed", "output": [], "usage": {}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Done"}, "finish_reason": "stop"}]
            },
        )

    async with ModelGateway(
        SimpleNamespace(
            model="test", base_url="https://model.example/v1", api_key="private"
        ),
        ["finish_analysis"],
        protocol=protocol,
    ) as gateway:
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        gateway.next_required_tool = "finish_analysis"
        async with httpx.AsyncClient() as client:
            for _ in range(2):
                response = await client.post(
                    gateway.url + "/responses",
                    headers={"Authorization": "Bearer " + gateway.token},
                    json={
                        "input": "finish",
                        "tools": [
                            {
                                "type": "function",
                                "name": "finish_analysis",
                                "parameters": {"type": "object"},
                            }
                        ],
                    },
                )
                assert response.status_code == 200
    choice = seen[0]["tool_choice"]
    assert (choice.get("function") or choice)["name"] == "finish_analysis"
    assert "tool_choice" not in seen[1]


@pytest.mark.parametrize("limit", ["requests", "tokens"])
async def test_budget_error_is_nonretryable_reports_usage_and_stops_upstream(limit):
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "Done"}}],
                "usage": {"total_tokens": 20},
            },
        )

    async with ModelGateway(
        SimpleNamespace(
            model="test", base_url="https://model.example/v1", api_key="secret"
        ),
        [],
        max_requests=1 if limit == "requests" else 10,
        max_tokens=20,
    ) as gateway:
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient() as client:
            body = {"input": "hello"}
            headers = {"Authorization": "Bearer " + gateway.token}
            assert (
                await client.post(
                    gateway.url + "/responses", json=body, headers=headers
                )
            ).status_code == 200
            refused = await client.post(
                gateway.url + "/responses", json=body, headers=headers
            )
        assert refused.status_code == 400
        assert refused.json()["error"]["code"] == "workbench_budget_exceeded"
        assert "20/20 cumulative tokens" in gateway.last_error
        assert (
            "request budget" if limit == "requests" else "token budget"
        ) in gateway.last_error
        assert gateway.budget_exceeded and gateway.request_count == 1
    assert len(requests) == 1


def test_model_budget_can_be_configured_without_removing_default_limit(monkeypatch):
    from api.services.agent_workbench.config import WorkbenchSettings

    monkeypatch.delenv("WORKBENCH_MAX_MODEL_TOKENS", raising=False)
    monkeypatch.delenv("WORKBENCH_MAX_MODEL_REQUESTS", raising=False)
    assert WorkbenchSettings.load().max_model_tokens == 150000
    monkeypatch.setenv("WORKBENCH_MAX_MODEL_TOKENS", "600000")
    monkeypatch.setenv("WORKBENCH_MAX_MODEL_REQUESTS", "24")
    settings = WorkbenchSettings.load()
    assert (settings.max_model_tokens, settings.max_model_requests) == (600000, 24)


@pytest.mark.parametrize(
    "protocol,path", [("chat", "/chat/completions"), ("responses", "/responses")]
)
async def test_protocol_routes_and_filters_hosted_tools(protocol, path):
    seen = []

    def upstream(request):
        import json

        payload = json.loads(request.content)
        seen.append((request.url.path, payload, request.headers.get("authorization")))
        usage = {"total_tokens": 12, "prompt_tokens": 8, "completion_tokens": 4}
        if protocol == "chat":
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call1",
                                        "function": {
                                            "name": "get_context",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": usage,
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "id": "fc1",
                        "type": "function_call",
                        "call_id": "call1",
                        "name": "get_context",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ],
                "usage": usage,
            },
        )

    config = SimpleNamespace(
        model="configured-model",
        base_url="https://model.example/v1",
        api_key="secret-provider",
    )
    async with ModelGateway(config, ["get_context"], protocol=protocol) as gateway:
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient() as client:
            body = {
                "input": [{"role": "developer", "content": "Build"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "name": "get_context",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "shell", "parameters": {}},
                    {"type": "web_search"},
                ],
            }
            unauthorized = await client.post(gateway.url + "/responses", json=body)
            assert unauthorized.status_code == 401
            result = await client.post(
                gateway.url + "/responses",
                json=body,
                headers={"Authorization": "Bearer " + gateway.token},
            )
    assert result.status_code == 200
    assert "response.completed" in result.text and "call1" in result.text
    assert "secret-provider" not in result.text
    assert seen[0][0] == "/v1" + path and seen[0][1]["model"] == "configured-model"
    assert len(seen[0][1]["tools"]) == 1
    assert seen[0][2] == "Bearer secret-provider"
    assert gateway.request_count == 1


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_protocol_failure_never_falls_back(protocol):
    paths = []

    def reject(request):
        paths.append(request.url.path)
        return httpx.Response(400, json={"error": "upstream private diagnostics"})

    async with ModelGateway(
        SimpleNamespace(
            model="qwen", base_url="https://model.example/v1", api_key="secret"
        ),
        [],
        protocol=protocol,
    ) as gateway:
        await gateway.client.aclose()
        gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(reject))
        async with httpx.AsyncClient() as client:
            response = await client.post(
                gateway.url + "/responses",
                json={"input": "hello"},
                headers={"Authorization": "Bearer " + gateway.token},
            )
    assert response.status_code == 502
    assert len(paths) == 1
    assert "private diagnostics" not in response.text
    assert "HTTP 400" in response.text


def test_chat_preserves_call_ids_and_developer_instructions():
    messages = chat_messages(
        {
            "instructions": "system",
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "rules"}],
                },
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "get_context",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "c1", "output": "result"},
            ],
        }
    )
    assert [m["role"] for m in messages] == ["system", "system", "assistant", "tool"]
    assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]


def test_nested_suite_schema_refs_are_resolvable():
    tools = WorkbenchTools(
        1, "session", {"input": {}}, {"target": {}, "target_fingerprint": "hash"}
    )
    schema = next(s["inputSchema"] for s in tools.specs() if s["name"] == "save_suite")
    suite = {
        "name": "price",
        "cases": [
            {
                "name": "price",
                "user_messages": ["How much?"],
                "assertions": [{"kind": "contains", "value": "free"}],
            }
        ],
    }
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate({"suite": suite}, schema)
    SimulationSuite.model_validate(suite)
    assert not any(
        t["name"] == "save_suite"
        for t in WorkbenchTools(
            1, "s", {}, {"target": {}, "target_fingerprint": "h"}, tuning=True
        ).specs()
    )
