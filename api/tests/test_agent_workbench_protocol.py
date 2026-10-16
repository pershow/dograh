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
        1, "session", {}, {"target": {}, "target_fingerprint": "hash"}
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
