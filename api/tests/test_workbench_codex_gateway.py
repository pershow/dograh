"""Codex metadata survives the Responses relay without leaking credentials."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from api.services.agent_workbench.model_gateway import (
    CODEX_CLIENT_RESTRICTION,
    ModelGateway,
    provider_http_failure,
)


def gateway(**kwargs):
    return ModelGateway(
        SimpleNamespace(
            model="configured",
            api_key="provider-secret",
            base_url="https://model.example/v1",
        ),
        ["read_node"],
        **kwargs,
    )


def completed(protocol="responses"):
    return httpx.Response(
        200,
        json=(
            {"status": "completed", "output": []}
            if protocol == "responses"
            else {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
        ),
    )


async def mock_upstream(gw, handler):
    await gw.client.aclose()
    gw.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_transport_errors_preserve_cause_phase_and_retry_without_secrets():
    notices = []

    async def notice(kind, payload):
        notices.append((kind, payload))

    async def upstream(request):
        await request.extensions["trace"]("http11.receive_response_headers.failed", {})
        try:
            raise ConnectionResetError("peer reset https://user:password@example.com/v1?token=private-query")
        except ConnectionResetError as cause:
            raise httpx.ReadError("provider-secret Authorization: Bearer private-header", request=request) from cause

    async with gateway(protocol="responses", max_retries=1, on_event=notice) as gw:
        await mock_upstream(gw, upstream)
        async with httpx.AsyncClient() as client:
            response = await client.post(gw.url + "/responses", headers={"Authorization": "Bearer " + gw.token}, json={})
        errors = [data for kind, data in notices if kind == "model.error"]
        assert [e["attempt"] for e in errors] == [1, 2]
        assert [e["type"] for e in errors[0]["exceptions"]] == ["ProviderFailure", "ReadError", "ConnectionResetError"]
        assert errors[0]["transport"]["failed_phase"] == "http11.receive_response_headers"
        assert errors[0]["output_exposed"] is False
        encoded = json.dumps(notices)
        assert all(secret not in encoded for secret in ("provider-secret", "private-query", "private-header", "user:password"))
        assert response.status_code == 502


@pytest.mark.parametrize("protocol", ["responses", "chat"])
async def test_metadata_and_options_are_forwarded_only_for_responses(protocol):
    seen = []

    def upstream(request):
        seen.append(request)
        return completed(protocol)

    metadata = {
        "User-Agent": "dograh_workbench/0.163.0 (dograh_workbench; 0.1.0)",
        "Originator": "dograh_workbench",
        "Session-Id": "session-one",
        "Thread-Id": "thread-one",
        "X-Client-Request-Id": "request-one",
        "X-Codex-Beta-Features": "remote_compaction_v2",
        "X-Codex-Turn-Metadata": '{"turn_id":"turn-one"}',
        "X-Codex-Turn-State": "state-one",
        "X-Codex-Window-Id": "window-one",
    }
    options = {
        "reasoning": {"effort": "medium"},
        "include": ["reasoning.encrypted_content"],
        "text": {"verbosity": "low"},
        "prompt_cache_key": "thread-one",
        "client_metadata": {"turn_id": "turn-one"},
        "parallel_tool_calls": False,
    }
    async with gateway(protocol=protocol, max_output_tokens=123) as gw:
        await mock_upstream(gw, upstream)
        async with httpx.AsyncClient() as client:
            result = await client.post(
                gw.url + "/responses",
                headers={
                    **metadata,
                    "Authorization": "Bearer " + gw.token,
                    "Cookie": "private-cookie",
                    "X-Api-Key": "private-key",
                    "X-Codex-Unknown-Secret": "private-extra",
                },
                json={
                    **options,
                    "input": "hello",
                    "model": "untrusted-model",
                    "store": True,
                    "max_output_tokens": 99999,
                    "tools": [
                        {"type": "function", "name": "read_node", "parameters": {}},
                        {"type": "function", "name": "shell", "parameters": {}},
                    ],
                },
            )
        assert result.status_code == 200
        request = seen[0]
        assert request.headers["authorization"] == "Bearer provider-secret"
        assert gw.token not in str(request.headers)
        assert all(
            secret not in str(request.headers)
            for secret in ("private-cookie", "private-key", "private-extra")
        )
        for name, value in metadata.items():
            if protocol == "responses":
                assert request.headers[name] == value
            else:
                assert request.headers.get(name) != value
        body = json.loads(request.content)
        assert body["model"] == "configured"
        if protocol == "responses":
            assert all(body[key] == value for key, value in options.items())
            assert body["store"] is False
            assert body["max_output_tokens"] == 123
            assert [tool["name"] for tool in body["tools"]] == ["read_node"]
        else:
            assert all(
                key not in body for key in options if key != "parallel_tool_calls"
            )
            assert [tool["function"]["name"] for tool in body["tools"]] == ["read_node"]


async def test_missing_identity_is_not_invented_and_unauthorized_requests_do_not_relay():
    seen = []

    def upstream(request):
        seen.append(request)
        return completed()

    async with gateway(protocol="responses") as gw:
        await mock_upstream(gw, upstream)
        async with httpx.AsyncClient() as client:
            rejected = await client.post(
                gw.url + "/responses",
                headers={"Originator": "dograh_workbench"},
                json={},
            )
            assert rejected.status_code == 401 and not seen
            await client.post(
                gw.url + "/responses",
                headers={"Authorization": "Bearer " + gw.token},
                json={},
            )
        assert "originator" not in seen[0].headers
        assert not any(key.startswith("x-codex-") for key in seen[0].headers)


async def test_concurrent_requests_keep_their_own_headers_on_retries():
    seen = {"first": [], "second": []}

    async def upstream(request):
        key = json.loads(request.content)["input"]
        seen[key].append(request.headers["x-codex-window-id"])
        await asyncio.sleep(0)
        return httpx.Response(429, json={}) if len(seen[key]) == 1 else completed()

    async with gateway(protocol="responses", max_retries=1) as gw:
        await mock_upstream(gw, upstream)
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *[
                    client.post(
                        gw.url + "/responses",
                        headers={
                            "Authorization": "Bearer " + gw.token,
                            "X-Codex-Window-Id": key,
                        },
                        json={"input": key},
                    )
                    for key in seen
                ]
            )
        assert all(result.status_code == 200 for result in results)
        assert seen == {"first": ["first", "first"], "second": ["second", "second"]}


@pytest.mark.parametrize(
    "suffix", ["", 'event: response.failed\ndata: {"secret":"private-details"}\n\n']
)
@pytest.mark.parametrize("stream", [False, True])
async def test_client_restriction_is_actionable_and_never_retried(suffix, stream):
    calls = []

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            403,
            text=json.dumps({"error": {"message": CODEX_CLIENT_RESTRICTION}}) + suffix,
        )

    async with gateway(protocol="responses") as gw:
        await mock_upstream(gw, upstream)
        async with httpx.AsyncClient() as client:
            result = await client.post(
                gw.url + "/responses",
                headers={"Authorization": "Bearer " + gw.token},
                json={"stream": stream},
            )
        assert len(calls) == 1
        assert gw.last_error_code == "provider_client_restricted"
        assert "client access policy" in gw.last_error
        assert "provider_client_restricted" in result.text
        assert "private-details" not in result.text
        assert "protocol compatibility" not in result.text


@pytest.mark.parametrize(
    "content", [b'{"error":{"message":"private-secret"}}', b"not JSON", b"[]"]
)
async def test_unknown_forbidden_body_is_not_exposed(content):
    error = await provider_http_failure(
        httpx.Response(403, content=content), "responses"
    )
    assert error.code == "provider_http_error" and not error.retryable
    assert "HTTP 403" in str(error) and "access policy" in str(error)
    assert "private-secret" not in str(error)


async def test_error_read_is_bounded_and_does_not_drain_a_trailing_stream():
    class ErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 8192
            pytest.fail("Oversized provider error was not bounded")

    error = await provider_http_failure(
        httpx.Response(403, stream=ErrorStream()), "responses"
    )
    assert error.code == "provider_http_error"

    class MixedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            body = json.dumps({"error": {"message": CODEX_CLIENT_RESTRICTION}}).encode()
            yield body[:10]
            yield body[10:]
            pytest.fail("Parsed JSON error should not drain the trailing SSE stream")

    error = await provider_http_failure(
        httpx.Response(403, stream=MixedStream()), "responses"
    )
    assert error.code == "provider_client_restricted"
