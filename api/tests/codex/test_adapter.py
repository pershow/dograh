"""Transport, build provenance, and real source-build contract coverage."""

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from api.services.agent_workbench.codex_client import CodexClient
from api.services.codex import runtime
from api.services.codex.connection import CodexConnection
from api.services.codex.runtime import (
    CodexError,
    default_binary,
    source_lock,
    validate_runtime,
)


def connection():
    rpc = CodexConnection(AsyncMock(), AsyncMock(), request_timeout=0.02)
    rpc.process = SimpleNamespace(
        stdin=SimpleNamespace(write=Mock(), drain=AsyncMock()),
        stdout=asyncio.StreamReader(),
    )
    return rpc


async def test_request_timeout_removes_waiter():
    rpc = connection()
    with pytest.raises(CodexError, match="timed out"):
        await rpc.request("thread/start", {})
    assert not rpc.pending


async def test_request_timeout_also_bounds_blocked_input_pipe():
    rpc = connection()
    blocked = asyncio.Event()
    rpc.process.stdin.drain = blocked.wait
    with pytest.raises(CodexError, match="timed out"):
        await rpc.request("turn/start", {})
    assert not rpc.pending


async def test_cancelled_request_removes_waiter():
    rpc = connection()
    task = asyncio.create_task(rpc.request("turn/start", {}))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not rpc.pending


async def test_eof_fails_pending_request_without_waiting_for_timeout():
    rpc = connection()
    rpc.request_timeout = 60
    request = asyncio.create_task(rpc.request("thread/start", {}))
    await asyncio.sleep(0)
    rpc.process.stdout.feed_eof()
    await rpc.read()
    with pytest.raises(CodexError, match="exited"):
        await asyncio.wait_for(request, 0.1)
    assert not rpc.pending


async def test_slow_tool_does_not_block_other_rpc_replies():
    rpc = connection()
    release = asyncio.Event()

    async def tool(_method, _params):
        await release.wait()
        return {"success": True}

    rpc.on_request = tool
    request = asyncio.create_task(rpc.request("thread/read", {}))
    await asyncio.sleep(0)
    for message in [
        {"id": "server-tool", "method": "item/tool/call", "params": {}},
        {"id": 1, "result": {"thread": "ready"}},
    ]:
        rpc.process.stdout.feed_data((json.dumps(message) + "\n").encode())
    reader = asyncio.create_task(rpc.read())
    try:
        assert await request == {"thread": "ready"}
        assert rpc.handlers
        release.set()
        await asyncio.gather(*rpc.handlers)
        reply = json.loads(rpc.process.stdin.write.call_args.args[0])
        assert reply == {"id": "server-tool", "result": {"success": True}}
    finally:
        rpc.process.stdout.feed_eof()
        await reader


async def test_unknown_approval_request_is_denied():
    client = CodexClient(SimpleNamespace(call=AsyncMock()), AsyncMock())
    rpc = connection()
    rpc.on_request = client.handle_request
    await rpc.respond(
        {"id": "approval", "method": "item/commandExecution/requestApproval"}
    )
    reply = json.loads(rpc.process.stdin.write.call_args.args[0])
    assert reply["id"] == "approval" and reply["error"]["code"] == -32601
    client.dispatcher.call.assert_not_called()


def test_build_record_rejects_foreign_revision_and_replaced_binary(
    tmp_path, monkeypatch
):
    executable = tmp_path / "codex-app-server"
    executable.write_text("compiled-test-fixture")
    executable.chmod(0o755)
    manifest = {
        **source_lock(),
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    metadata = tmp_path / "build.json"
    metadata.write_text(json.dumps({**manifest, "revision": "different"}))
    with pytest.raises(CodexError, match="different source pin"):
        validate_runtime(str(executable))
    metadata.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        runtime.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                stdout=f"codex-app-server {manifest['version']}\n"
            )
        ),
    )
    assert validate_runtime(str(executable))["revision"] == source_lock()["revision"]
    executable.write_text("replaced-test-fixture")
    with pytest.raises(CodexError, match="differs from"):
        validate_runtime(str(executable))


def test_missing_build_never_falls_back_to_desktop_codex(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKBENCH_CODEX_BINARY", raising=False)
    from api.services.agent_workbench.config import WorkbenchSettings

    assert WorkbenchSettings.load().codex_binary == default_binary()
    with pytest.raises(CodexError, match="Source-built Codex is missing"):
        validate_runtime(str(tmp_path / "codex-app-server"))


def test_source_digest_detects_source_changes_but_ignores_build_output(tmp_path):
    from scripts.build_codex import source_digest

    workspace = tmp_path / "codex-rs"
    workspace.mkdir()
    code = workspace / "main.rs"
    code.write_text("fn main() {}")
    original = source_digest(tmp_path)
    (workspace / "target").mkdir()
    (workspace / "target" / "binary").write_bytes(b"compiled output")
    assert source_digest(tmp_path) == original
    code.write_text("fn main() { panic!(); }")
    assert source_digest(tmp_path) != original


@pytest.fixture
def source_binary(monkeypatch):
    if os.getenv("RUN_CODEX_SOURCE_TESTS") != "1":
        pytest.skip("Set RUN_CODEX_SOURCE_TESTS=1 after building the pinned source.")
    binary = default_binary()
    validate_runtime(binary)
    monkeypatch.setenv("WORKBENCH_CODEX_BINARY", binary)
    return binary


@pytest.mark.parametrize(
    "protocol,model",
    [("chat", "test-model"), ("responses", "test-model"), ("responses", "gpt-6.1-sol")],
)
@pytest.mark.parametrize("malformed_first", [False, True])
async def test_source_build_completes_dynamic_tool_round_trip(
    source_binary, tmp_path, monkeypatch, protocol, model, malformed_first
):
    """Real Rust process -> Dograh gateway -> local model fixture -> real tool RPC."""
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    seen = []

    async def upstream(request):
        assert request.headers["Authorization"] == "Bearer local-test-key"
        if protocol == "responses":
            assert request.headers["User-Agent"].startswith("codex_exec/")
            assert "codex_exec; 0.1.0" in request.headers["User-Agent"]
            assert request.headers["Originator"] == "codex_exec"
            metadata = json.loads(request.headers["X-Codex-Turn-Metadata"])
            assert metadata["window_id"] == request.headers["X-Codex-Window-Id"]
            assert metadata["thread_id"] == request.headers["Thread-Id"]
        else:
            assert "Originator" not in request.headers
        body = await request.json()
        assert body["model"] == model
        if protocol == "responses":
            assert "reasoning.encrypted_content" in body["include"]
            assert body["prompt_cache_key"]
        seen.append(body)
        turn = len(seen) - int(malformed_first)
        if turn == 0:
            item = {
                "id": "fc_bad",
                "type": "function_call",
                "name": "finish_analysis",
                "call_id": "bad-call",
                "arguments": '{"answer":"bad\\q"}',
                "status": "completed",
            }
            frames = (
                [
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": item["call_id"],
                                            "function": {
                                                "name": item["name"],
                                                "arguments": item["arguments"],
                                            },
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ]
                    }
                ]
                if protocol == "chat"
                else [
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "arguments": "", "status": "in_progress"},
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": item["id"],
                        "delta": item["arguments"],
                    },
                    {
                        "type": "response.completed",
                        "response": {"status": "completed", "output": [item]},
                    },
                ]
            )
            return web.Response(
                text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
                content_type="text/event-stream",
            )
        if protocol == "chat":
            assert [tool["function"]["name"] for tool in body["tools"]] == [
                "finish_analysis"
            ]
            message = {"content": "Verified source adapter."}
            if turn == 1:
                message = {
                    "tool_calls": [
                        {
                            "id": "analysis-call",
                            "type": "function",
                            "function": {
                                "name": "finish_analysis",
                                "arguments": '{"answer":"Verified."}',
                            },
                        }
                    ]
                }
            return web.json_response(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls" if turn == 1 else "stop",
                            "message": message,
                        }
                    ],
                    "usage": {"total_tokens": 10},
                }
            )
        assert [tool["name"] for tool in body["tools"]] == ["finish_analysis"]
        output = (
            [
                {
                    "type": "function_call",
                    "id": "fc_analysis",
                    "call_id": "analysis-call",
                    "name": "finish_analysis",
                    "arguments": '{"answer":"Verified."}',
                    "status": "completed",
                }
            ]
            if turn == 1
            else [
                {
                    "type": "message",
                    "id": "msg_done",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Verified source adapter.",
                            "annotations": [],
                        }
                    ],
                }
            ]
        )
        return web.json_response(
            {"status": "completed", "output": output, "usage": {"total_tokens": 10}}
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", upstream)
    app.router.add_post("/v1/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    dispatcher = SimpleNamespace(intent="analyze", analysis_answer=None)
    dispatcher.specs = lambda: [
        {
            "name": "finish_analysis",
            "description": "Complete the user's analysis.",
            "inputSchema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        }
    ]

    async def call(name, arguments):
        assert name == "finish_analysis"
        dispatcher.analysis_answer = arguments["answer"]
        return {"answer": dispatcher.analysis_answer}

    dispatcher.call = AsyncMock(side_effect=call)
    client = CodexClient(dispatcher, AsyncMock())
    try:
        result = await asyncio.wait_for(
            client.run(
                "Verify the adapter.",
                SimpleNamespace(
                    model=model,
                    api_key="local-test-key",
                    base_url=f"http://127.0.0.1:{port}/v1",
                ),
                protocol=protocol,
            ),
            45,
        )
        assert dispatcher.analysis_answer == "Verified."
        dispatcher.call.assert_awaited_once()
        assert result["messages"] == ["Verified source adapter."]
        assert result["model_requests"] == 2 + int(malformed_first)
        assert client.rpc.process.returncode is not None
        assert not list((tmp_path / "jobs").iterdir())
        assert len(seen) == 2 + int(malformed_first)
    finally:
        await runner.cleanup()


async def test_source_build_repairs_citations_and_finishes_at_repaired_receipt(
    source_binary, tmp_path, monkeypatch
):
    from api.services.agent_workbench.citations import (
        CitationSourceReader,
        CitationSubmission,
    )
    from api.services.agent_workbench.test_compilation import RUBRIC, validate_rubric
    from api.services.agent_workbench.test_tasks import StructuredDecision

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    record = {
        "id": "basis:policy:0",
        "source": "basis",
        "source_kind": "material",
        "text": "Retries must not exceed two.",
    }
    reader = CitationSourceReader([record])
    recovery = CitationSubmission(
        RUBRIC,
        "requirements",
        reader,
        lambda v: validate_rubric(v, {record["id"]: record}),
    )
    dispatcher = StructuredDecision(
        "compile_business_rubric",
        "Select original evidence.",
        recovery.schema,
        validate=recovery,
        reader=reader,
        recovery=recovery,
    )
    events = AsyncMock()
    client = CodexClient(dispatcher, events)
    seen = []

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        assert {t["name"] for t in body["tools"]} == {
            "compile_business_rubric",
            "repair_citations",
            "read_review_source",
        }
        if len(seen) == 1:
            tool = "compile_business_rubric"
            arguments = {
                "requirements": [
                    {
                        "description": "Retry limit",
                        "applies_when": "An operation fails",
                        "expected_behavior": "At most two retries",
                        "severity": "blocking",
                        "citations": [{"span_id": "incorrect"}],
                    }
                ],
                "questions": [],
            }
        else:
            assert len(seen) == 2 and dispatcher.report is None
            assert recovery.draft_id in json.dumps(body)
            tool = "repair_citations"
            arguments = {
                "draft_id": recovery.draft_id,
                "patches": [
                    {
                        "item_index": 0,
                        "citation_index": 0,
                        "span_id": next(iter(reader.spans)),
                    }
                ],
            }
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{len(seen)}",
                        "call_id": f"call_{len(seen)}",
                        "name": tool,
                        "arguments": json.dumps(arguments),
                        "status": "completed",
                    }
                ],
                "usage": {"total_tokens": 10},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        result = await asyncio.wait_for(
            client.run(
                json.dumps(reader.packet),
                SimpleNamespace(
                    model="test-model",
                    api_key="local-test-key",
                    base_url=f"http://127.0.0.1:{port}/v1",
                ),
                protocol="responses",
            ),
            timeout=45,
        )
        assert result["model_requests"] == len(seen) == 2
        rule = dispatcher.report["requirements"][0]
        assert rule["expected_behavior"] == "At most two retries"
        assert rule["severity"] == "blocking"
        assert rule["citations"] == [{"id": record["id"], "quote": record["text"]}]
        assert any(
            c.args[0] == "tool.failed" and c.args[1]["validation"]["issue_count"] == 1
            for c in events.call_args_list
        )
        assert client.rpc.process.returncode is not None
        assert not list((tmp_path / "jobs").iterdir())
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("auto_compact", [False, True])
async def test_native_thread_survives_process_restart_and_imports_all_legacy_requests(
    source_binary, tmp_path, monkeypatch, auto_compact
):
    import uuid

    from api.services.agent_workbench import conversation

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    bindings, seen = {}, []
    events = [
        {
            "sequence": i,
            "type": "task.queued",
            "payload": {
                "kind": "build",
                "input": {"text": f"Original requirement {i}"},
            },
        }
        for i in range(1, 76)
    ]

    async def history(_org, _sid, *, before, limit):
        rows = [e for e in events if e["sequence"] < before] if _sid == sid else []
        return {
            "events": rows[-limit:],
            "before": rows[-limit]["sequence"] if len(rows) > limit else None,
        }

    async def emit(_org, sid, kind, payload, **_):
        if kind == "codex.bound":
            bindings[sid] = payload

    monkeypatch.setattr(
        conversation.db_client, "wb_conversation", AsyncMock(side_effect=history)
    )
    monkeypatch.setattr(
        conversation.db_client,
        "wb_codex_binding",
        AsyncMock(side_effect=lambda _o, sid: bindings.get(sid)),
    )
    monkeypatch.setattr(conversation.db_client, "wb_emit", AsyncMock(side_effect=emit))

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        compacting = "CONTEXT CHECKPOINT COMPACTION" in json.dumps(body)
        reply = (
            "Summary: Original requirement 1; Original requirement 75; continuity-marker."
            if compacting
            else "Acknowledged."
        )
        used = 100000 if auto_compact and len(seen) == 1 else 10
        return web.json_response(
            {
                "choices": [{"finish_reason": "stop", "message": {"content": reply}}],
                "usage": {
                    "prompt_tokens": used,
                    "completion_tokens": 10,
                    "total_tokens": used + 10,
                },
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    config = SimpleNamespace(
        model="test-model",
        api_key="test",
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
    )
    sid = str(uuid.uuid4())
    dispatcher = SimpleNamespace(
        intent="probe",
        tuning=False,
        org=1,
        session_id=sid,
        context={"session": {"workflow_id": 7, "event_sequence": 75}},
        job={"id": "j", "lease_token": "lease", "output": {"history_before": 76}},
        specs=list,
        thread_specs=list,
    )
    try:
        first = CodexClient(dispatcher, AsyncMock())
        await asyncio.wait_for(first.run("Remember continuity-marker.", config), 45)
        assert "Original requirement 1" in json.dumps(seen[0])
        assert "Original requirement 75" in json.dumps(seen[0])
        assert first.rpc.process.returncode is not None
        notifications = AsyncMock()
        second = CodexClient(dispatcher, notifications)
        await asyncio.wait_for(
            second.run("Continue this same conversation.", config), 45
        )
        assert second.thread_id == first.thread_id
        assert "continuity-marker" in json.dumps(seen[-1])
        assert "Original requirement 1" in json.dumps(seen[-1])
        assert (
            conversation.db_client.wb_conversation.await_count == 2
        )  # one import, not one per job
        assert list(tmp_path.rglob("config.toml"))
        if auto_compact:
            assert any(
                c.args[0] == "context.compaction" for c in notifications.await_args_list
            )
            assert "Summary: Original requirement 1" in json.dumps(seen[-1])
        else:
            dispatcher.session_id = str(uuid.uuid4())
            third = CodexClient(dispatcher, AsyncMock())
            await asyncio.wait_for(
                third.run("A separate session in this Agent.", config), 45
            )
            assert third.thread_id != first.thread_id
            assert (
                bindings[dispatcher.session_id]["project_id"]
                == bindings[sid]["project_id"]
            )
            assert "continuity-marker" not in json.dumps(seen[-1])
            assert "Original requirement 1" not in json.dumps(seen[-1])
    finally:
        await runner.cleanup()


async def test_missing_native_storage_never_silently_starts_a_blank_thread(
    tmp_path, monkeypatch
):
    import uuid

    from api.services.agent_workbench import conversation
    from api.services.agent_workbench.conversation import DurableConversation
    from api.services.agent_workbench.errors import WorkbenchError

    monkeypatch.setattr(
        conversation.db_client,
        "wb_codex_binding",
        AsyncMock(return_value={"thread_id": "missing"}),
    )
    dispatcher = SimpleNamespace(
        org=1, session_id=str(uuid.uuid4()), context={"session": {"workflow_id": 7}}
    )
    rpc = SimpleNamespace(
        request=AsyncMock(side_effect=RuntimeError("storage unavailable"))
    )
    with pytest.raises(WorkbenchError, match="没有创建空白会话"):
        await DurableConversation(dispatcher, tmp_path).open(rpc, {"dynamicTools": []})
    rpc.request.assert_awaited_once_with(
        "thread/resume", {"threadId": "missing", "excludeTurns": True}
    )
