"""Wire-level regressions: deltas arrive early, partial tools never execute."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from api.services.agent_workbench.model_gateway import ModelGateway


def frame(data):
    return ("data: " + json.dumps(data) + "\n\n").encode()


def chat_delta(arguments, *, first=False, finish=None):
    function = {"arguments": arguments}
    call = {"index": 0, "function": function}
    if first:
        call.update(id="call-original", type="function")
        function["name"] = "save_candidate"
    return {
        "choices": [
            {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": finish}
        ]
    }


class Bytes(httpx.AsyncByteStream):
    def __init__(self, iterator):
        self.iterator = iterator
        self.closed = asyncio.Event()

    async def __aiter__(self):
        async for chunk in self.iterator():
            yield chunk

    async def aclose(self):
        self.closed.set()


def gateway(**kwargs):
    return ModelGateway(
        SimpleNamespace(
            model="qwen", base_url="https://model.example/v1", api_key="private-key"
        ),
        ["save_candidate"],
        **kwargs,
    )


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_durable_catalog_is_filtered_to_current_permissions_and_schemas(protocol):
    current = [
        {
            "name": "read_conversation",
            "description": "Read history",
            "inputSchema": {"type": "object", "properties": {}},
        }
    ]
    gw = ModelGateway(
        SimpleNamespace(model="test"),
        ["read_conversation", "save_candidate"],
        protocol=protocol,
        tool_specs=lambda: current,
    )
    gw.base = "https://model.example/v1"
    body = {
        "tools": [
            {"type": "function", "name": "read_conversation", "parameters": {}},
            {
                "type": "function",
                "name": "save_candidate",
                "parameters": {"properties": {"code": {"type": "string"}}},
            },
        ]
    }
    try:
        payload, _ = gw.payload(body)
        visible = [t.get("function", t) for t in payload["tools"]]
        assert [t["name"] for t in visible] == ["read_conversation"]
        current.append(
            {
                "name": "save_candidate",
                "description": "Local edits",
                "inputSchema": {
                    "type": "object",
                    "properties": {"node_updates": {"type": "array"}},
                },
            }
        )
        payload, _ = gw.payload(body)
        visible = [t.get("function", t) for t in payload["tools"]]
        assert "code" not in visible[1]["parameters"]["properties"]
        assert "node_updates" in visible[1]["parameters"]["properties"]
    finally:
        await gw.client.aclose()


async def mock_client(gw, handler):
    await gw.client.aclose()
    gw.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("reasoning", [False, True])
async def test_keepalives_do_not_hide_a_stall_but_reasoning_activity_keeps_stream_alive(
    reasoning,
):
    async def chunks():
        for _ in range(8):
            await asyncio.sleep(0.02)
            yield frame(
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "PRIVATE_THOUGHT"
                                if reasoning
                                else ""
                            }
                        }
                    ]
                }
            )
            yield b": heartbeat\n\n"
        yield frame(
            {
                "choices": [
                    {"delta": {"content": "Final answer"}, "finish_reason": "stop"}
                ]
            }
        )
        yield b"data: [DONE]\n\n"

    async with gateway(idle_timeout=0.07, max_retries=0, progress_interval=0.01) as gw:
        await mock_client(
            gw,
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Bytes(chunks)
            ),
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        final = events(response)[-1]
        assert final["type"] == (
            "response.completed" if reasoning else "response.failed"
        )
        assert (
            "PRIVATE_THOUGHT" not in response.text
            and "dograh.reasoning_activity" not in response.text
        )
        if not reasoning:
            assert final["response"]["error"]["code"] == "model_timeout"


def request(gw):
    return {
        "url": gw.url + "/responses",
        "headers": {"Authorization": "Bearer " + gw.token},
        "json": {
            "input": "build",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "save_candidate",
                    "parameters": {"type": "object"},
                }
            ],
        },
    }


def events(response):
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_first_delta_is_forwarded_before_upstream_finishes(protocol):
    release = asyncio.Event()
    tool = {
        "id": "fc-native",
        "type": "function_call",
        "call_id": "call-original",
        "name": "save_candidate",
        "arguments": '{"code":"你好"}',
        "status": "completed",
    }

    async def chunks():
        if protocol == "chat":
            # Split both UTF-8 bytes and SSE boundaries across HTTP chunks.
            raw = frame(chat_delta('{"code":"你', first=True))
            for index in range(0, len(raw), 7):
                yield raw[index : index + 7]
            await release.wait()
            yield frame(chat_delta('好"}', finish="tool_calls"))
            yield frame(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 20,
                        "total_tokens": 30,
                    },
                }
            )
            yield b"data: [DONE]\n\n"
        else:
            yield frame(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**tool, "arguments": "", "status": "in_progress"},
                }
            )
            yield frame(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": tool["id"],
                    "delta": '{"code":"你',
                }
            )
            await release.wait()
            yield frame(
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": tool["id"],
                    "delta": '好"}',
                }
            )
            yield frame(
                {"type": "response.output_item.done", "output_index": 0, "item": tool}
            )
            yield frame(
                {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [tool],
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 20,
                            "total_tokens": 30,
                        },
                    },
                }
            )

    stream = Bytes(chunks)
    seen = []

    def upstream(req):
        seen.append(json.loads(req.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=stream
        )

    async with gateway(protocol=protocol) as gw:
        await mock_client(gw, upstream)
        all_events = []
        async with (
            httpx.AsyncClient(timeout=2) as client,
            client.stream("POST", **request(gw)) as response,
        ):
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                all_events.append(event)
                if (
                    event["type"] == "response.function_call_arguments.delta"
                    and not release.is_set()
                ):
                    assert not any(
                        e["type"] == "response.output_item.done" for e in all_events
                    )
                    release.set()
        assert release.is_set()
        assert all_events[-1]["type"] == "response.completed"
        item = all_events[-1]["response"]["output"][0]
        assert (
            item["arguments"] == '{"code":"你好"}'
            and item["call_id"] == "call-original"
        )
        assert gw.tokens == 30 and seen[0]["stream"] is True
        assert seen[0].get("max_tokens", seen[0].get("max_output_tokens")) == 32768
        assert stream.closed.is_set()


@pytest.mark.parametrize("failure", ["length", "eof", "timeout"])
async def test_partial_tool_is_never_completed_or_retried(failure):
    calls = []

    async def chunks():
        yield frame(chat_delta('{"code":"unfinished', first=True))
        if failure == "timeout":
            raise httpx.ReadTimeout("private upstream detail")
        if failure == "length":
            yield frame(
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
                    "usage": {"total_tokens": 40},
                }
            )

    def upstream(req):
        calls.append(req)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Bytes(chunks)
        )

    notices = []

    async def notice(kind, payload):
        notices.append((kind, payload))

    async with gateway(on_event=notice) as gw:
        await mock_client(gw, upstream)
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        data = events(response)
        assert data[-1]["type"] == "response.failed"
        assert not any(
            e["type"] in {"response.output_item.done", "response.completed"}
            for e in data
        )
        assert len(calls) == 1
        assert notices[-1][1]["phase"] == "failed"
        assert (
            "private upstream detail" not in response.text
            and "private-key" not in response.text
        )
        if failure == "length":
            assert gw.tokens == 40 and "32768 token limit" in gw.last_error


async def test_transient_timeout_has_bounded_visible_retries_and_preserves_required_tool():
    seen, notices = [], []

    def upstream(req):
        seen.append(json.loads(req.content))
        if len(seen) < 3:
            raise httpx.ReadTimeout("secret private detail")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]
            },
        )

    async def notice(kind, payload):
        notices.append(payload)

    async with gateway(on_event=notice) as gw:
        await mock_client(gw, upstream)
        gw.next_required_tool = "save_candidate"
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        assert events(response)[-1]["type"] == "response.completed"
        assert len(seen) == 3
        assert all(
            p["tool_choice"]["function"]["name"] == "save_candidate" for p in seen
        )
        retries = [p for p in notices if p["phase"] == "retrying"]
        assert [p["next_attempt"] for p in retries] == [2, 3]
        assert all(p["reason"] == "model_timeout" for p in retries)
        assert gw.last_error is None


async def test_retry_exhaustion_stops_at_configured_bound():
    notices = []

    async def notice(kind, payload):
        notices.append(payload)

    def upstream(req):
        raise httpx.ConnectTimeout("secret")

    async with gateway(max_retries=1, on_event=notice) as gw:
        await mock_client(gw, upstream)
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        assert gw.request_count == 2
        assert events(response)[-1]["response"]["error"]["code"] == "model_timeout"
        assert "connection timed out after 15s" in gw.last_error
        assert notices[-1]["phase"] == "failed"


async def test_json_fallback_never_completes_malformed_tool_arguments():
    async with gateway() as gw:
        await mock_client(
            gw,
            lambda req: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "partial",
                                        "type": "function",
                                        "function": {
                                            "name": "save_candidate",
                                            "arguments": '{"code":',
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                },
            ),
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        data = events(response)
        assert data[-1]["type"] == "response.failed"
        assert not any(e["type"] == "response.output_item.done" for e in data)
        assert gw.last_error_code == "model_invalid_tool_arguments"


async def test_streamed_invalid_tool_json_is_actionable_without_completing_any_call():
    async def chunks():
        yield frame(chat_delta('{"code":"bad\\q"}', first=True, finish="tool_calls"))

    async with gateway() as gw:
        await mock_client(
            gw,
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Bytes(chunks)
            ),
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        data = events(response)
        assert gw.last_error_code == "model_invalid_tool_arguments"
        assert gw.invalid_tool_name == "save_candidate"
        assert "column" in gw.last_error and "bad" not in gw.last_error
        assert not any(e["type"] == "response.output_item.done" for e in data)


async def test_codex_reasks_invalid_arguments_in_same_thread_without_replaying_tools():
    from unittest.mock import AsyncMock

    from api.services.agent_workbench.codex_client import CodexClient
    from api.services.agent_workbench.errors import WorkbenchError

    client = CodexClient(SimpleNamespace(), AsyncMock())
    client.thread_id = "same-thread"
    client.rpc.wait = AsyncMock(
        side_effect=[WorkbenchError("turn failed"), {"status": "completed"}]
    )
    client.rpc.request = AsyncMock()
    gw = SimpleNamespace(
        last_error_code="model_invalid_tool_arguments",
        last_error="invalid JSON",
        invalid_tool_name="save_candidate",
        next_required_tool=None,
    )
    await client.wait_with_repair(gw, SimpleNamespace(timeout_seconds=None))
    assert client.rpc.request.call_args.args[1]["threadId"] == "same-thread"
    assert gw.next_required_tool == "save_candidate"
    assert client.on_event.call_args.args[1]["phase"] == "repairing"


async def test_format_repair_stops_after_two_corrections():
    from unittest.mock import AsyncMock

    from api.services.agent_workbench.codex_client import CodexClient
    from api.services.agent_workbench.errors import WorkbenchError

    client = CodexClient(SimpleNamespace(), AsyncMock())
    client.thread_id = "thread"
    gw = SimpleNamespace(
        last_error_code=None, last_error=None, invalid_tool_name="save_candidate"
    )

    async def fail(*_):
        gw.last_error_code = "model_invalid_tool_arguments"
        gw.last_error = "invalid JSON"
        raise WorkbenchError("turn failed")

    client.rpc.wait = AsyncMock(side_effect=fail)
    client.rpc.request = AsyncMock()
    with pytest.raises(WorkbenchError, match="turn failed"):
        await client.wait_with_repair(gw, SimpleNamespace(timeout_seconds=None))
    assert client.rpc.request.await_count == 2
    assert client.rpc.wait.await_count == 3


async def test_absolute_deadline_bounds_even_an_active_stream():
    async def chunks():
        yield frame(chat_delta('{"code":"', first=True))
        while True:
            await asyncio.sleep(0.01)
            yield frame(chat_delta("x"))

    stream = Bytes(chunks)
    notices = []

    async def notice(kind, payload):
        notices.append(payload)

    async with gateway(
        request_timeout=0.08, progress_interval=0.02, on_event=notice
    ) as gw:
        await mock_client(
            gw,
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            ),
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        assert events(response)[-1]["response"]["error"]["code"] == "model_deadline"
        assert any(
            p["phase"] == "generating" and p["output_characters"] > 0 for p in notices
        )
        assert stream.closed.is_set()


async def test_downstream_disconnect_closes_upstream():
    async def chunks():
        yield frame(chat_delta('{"code":"', first=True))
        while True:
            await asyncio.sleep(0.01)
            yield frame(chat_delta("x"))

    stream = Bytes(chunks)
    async with gateway(progress_interval=0.02) as gw:
        await mock_client(
            gw,
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            ),
        )
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", **request(gw)) as response:
                async for line in response.aiter_lines():
                    if "arguments.delta" in line:
                        break
            await asyncio.wait_for(stream.closed.wait(), 2)


@pytest.mark.parametrize("bad_tool", [True, False])
async def test_native_responses_rejects_unapproved_tools_and_incomplete_final_response(
    bad_tool,
):
    item = {
        "id": "fc1",
        "type": "function_call",
        "name": "shell" if bad_tool else "save_candidate",
        "call_id": "c1",
        "arguments": "{}",
        "status": "completed",
    }

    async def chunks():
        yield frame(
            {"type": "response.output_item.done", "item": item, "output_index": 0}
        )
        yield frame(
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "usage": {"total_tokens": 42},
                },
            }
        )

    async with gateway(protocol="responses") as gw:
        await mock_client(
            gw,
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Bytes(chunks)
            ),
        )
        async with httpx.AsyncClient() as client:
            response = await client.post(**request(gw))
        assert not any(
            e["type"] == "response.output_item.done" for e in events(response)
        )
        assert events(response)[-1]["type"] == "response.failed"
        if not bad_tool:
            assert gw.tokens == 42


@pytest.mark.parametrize("when", ["waiting", "heartbeat", "completed"])
async def test_cancelled_lease_closes_upstream_without_reporting_provider_failure(
    when, caplog
):
    from api.services.agent_workbench.errors import LeaseLost

    observed = []
    waiting = 0

    async def progress(kind, payload):
        nonlocal waiting
        if kind == "model.progress" and payload["phase"] == "waiting":
            waiting += 1
        if kind == "model.progress" and (
            payload["phase"] == when or (when == "heartbeat" and waiting > 1)
        ):
            raise LeaseLost()
        observed.append((kind, payload))

    async def chunks():
        if when == "completed":
            yield frame(
                {"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}
            )
            yield b"data: [DONE]\n\n"
        else:
            await asyncio.Event().wait()
            yield b"never"

    stream = Bytes(chunks)
    async with gateway(on_event=progress, progress_interval=0.01, idle_timeout=2) as gw:
        await mock_client(
            gw,
            lambda _: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            ),
        )
        async with httpx.AsyncClient(timeout=2) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.post(**request(gw))
        if when != "waiting":
            await asyncio.wait_for(stream.closed.wait(), 1)
        assert gw.last_error is None
        assert not any(
            kind == "model.error" or p.get("phase") == "failed" for kind, p in observed
        )
    assert not any(
        "Error handling request" in r.message or "LeaseLost" in r.message
        for r in caplog.records
    )
