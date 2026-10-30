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


@pytest.mark.parametrize(
    "citation",
    [
        {"span_id": "incorrect"},
        {},
        {"id": "legacy", "quote": "Retries must not exceed two."},
    ],
)
async def test_source_build_repairs_citations_and_finishes_at_repaired_receipt(
    source_binary, tmp_path, monkeypatch, citation
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
        assert {t["name"] for t in body["tools"]} == (
            {"compile_business_rubric", "repair_citations", "read_review_source"}
            if len(seen) == 1
            else {"repair_citations", "read_review_source"}
        )
        if len(seen) == 1:
            tool = "compile_business_rubric"
            arguments = {
                "requirements": [
                    {
                        "description": "Retry limit",
                        "applies_when": "An operation fails",
                        "expected_behavior": "At most two retries",
                        "severity": "blocking",
                        "citations": [citation],
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


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("verification", [False, True])
async def test_large_fixture_schema_survives_real_sdk_without_losing_constraints(
    source_binary, tmp_path, monkeypatch, protocol, verification
):
    from copy import deepcopy

    from api.services.agent_workbench.fixtures import FixtureCodec, FixtureSubmission
    from api.services.agent_workbench.test_tasks import StructuredDecision
    from api.tests.test_workbench_test_compilation import fixture

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    case = fixture()
    plan = case["compiled"]["scenario"]
    for index in range(64):
        turn = len(case["reference_turns"])
        case["reference_turns"].append(
            {"role": "user", "text": f"收到，第 {index} 项。"}
        )
        plan["source_coverage"].append(
            {
                "turn": turn,
                "disposition": "filler",
                "scenario_ids": [],
                "reason": "独立的传输测试样本",
            }
        )
    codec = FixtureCodec(case["reference_turns"])
    wire = codec.encode(plan)
    recovery = FixtureSubmission(
        codec,
        draft=wire if verification else None,
        original=plan if verification else None,
    )
    name = "verify_customer_fixture" if verification else "compile_customer_fixture"
    dispatcher = StructuredDecision(
        name,
        "Return the fixture",
        recovery.schema,
        validate=recovery,
        recovery=recovery,
    )
    specs = {s["name"]: deepcopy(s) for s in dispatcher.specs()}
    assert len(json.dumps(specs[name]["inputSchema"]).encode()) > 5000
    args = (
        {
            "verdict": "supported",
            "reason": "Checked",
            "corrections": {key: None for key in wire},
        }
        if verification
        else wire
    )
    seen = []
    failures = []

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        supplied = (
            body["tools"]
            if protocol == "responses"
            else [t["function"] for t in body["tools"]]
        )
        try:
            assert {t["name"] for t in supplied} == set(specs)
            for tool in supplied:
                # Exact equality covers nested arrays, enum catalogs, nullable
                # corrections, required fields and scalar validation keywords.
                assert tool["parameters"] == specs[tool["name"]]["inputSchema"]
                assert tool["description"] == specs[tool["name"]]["description"]
        except AssertionError as error:
            failures.append(error)
        if protocol == "chat":
            return web.json_response(
                {
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_fixture",
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": json.dumps(args),
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                    "usage": {"total_tokens": 10},
                }
            )
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_fixture",
                        "call_id": "call_fixture",
                        "name": name,
                        "arguments": json.dumps(args),
                        "status": "completed",
                    }
                ],
                "usage": {"total_tokens": 10},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    app.router.add_post("/v1/chat/completions", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = CodexClient(dispatcher, AsyncMock())
    try:
        result = await asyncio.wait_for(
            client.run(
                "Submit the fixture",
                SimpleNamespace(
                    model="local-mock",
                    api_key="local-test-key",
                    base_url=f"http://127.0.0.1:{port}/v1",
                ),
                protocol=protocol,
            ),
            45,
        )
        assert len(seen) == result["model_requests"] == 1
        assert not failures, "Provider received a lossy SDK schema"
        accepted = dispatcher.report["scenario"] if verification else dispatcher.report
        assert (
            accepted["actions"][0]["preconditions"]
            == plan["actions"][0]["preconditions"]
        )
        assert accepted["actions"][0]["effects"] == plan["actions"][0]["effects"]
        assert len(accepted["source_coverage"]) == 65
        assert client.rpc.process.returncode is not None
        assert not list((tmp_path / "jobs").iterdir())
    finally:
        await runner.cleanup()


async def test_source_build_repairs_bound_fixture_without_rewriting_valid_content(
    source_binary, tmp_path, monkeypatch
):
    from api.services.agent_workbench.fixtures import FixtureCodec, FixtureSubmission
    from api.services.agent_workbench.test_tasks import StructuredDecision
    from api.tests.test_workbench_test_compilation import fixture

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    case = fixture()
    codec = FixtureCodec(case["reference_turns"])
    wire = codec.encode(case["compiled"]["scenario"])
    wire["actions"][0]["effects"][0]["key"] = "undeclared"
    recovery = FixtureSubmission(codec)
    saved = []

    async def save():
        saved.append(json.loads(json.dumps(recovery.draft)))

    recovery.save = save
    dispatcher = StructuredDecision(
        "compile_customer_fixture",
        "Bind source evidence",
        recovery.schema,
        validate=recovery,
        recovery=recovery,
    )
    events = AsyncMock()
    client = CodexClient(dispatcher, events)
    seen = []

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        specs = {t["name"]: t for t in body["tools"]}
        if len(seen) == 1:
            assert set(specs) == {"compile_customer_fixture", "repair_fixture"}
            assert specs["compile_customer_fixture"]["parameters"] == recovery.schema
            assert set(
                specs["compile_customer_fixture"]["parameters"]["properties"][
                    "source_coverage"
                ]["properties"]
            ) == set(codec.sources)
            tool, args = "compile_customer_fixture", wire
        else:
            assert set(specs) == {"repair_fixture"}
            assert (
                specs["repair_fixture"]["parameters"] == recovery.spec()["inputSchema"]
            )
            assert len(seen) == 2 and saved and recovery.draft_id in json.dumps(body)
            problem = recovery._error().issues[0]
            tool, args = (
                "repair_fixture",
                {
                    "draft_id": recovery.draft_id,
                    "patches": [
                        {"repair_id": problem["repair_id"], "value": "enabled"}
                    ],
                },
            )
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{len(seen)}",
                        "call_id": f"call_{len(seen)}",
                        "name": tool,
                        "arguments": json.dumps(args),
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
                json.dumps(codec.evidence),
                SimpleNamespace(
                    model="test-model",
                    api_key="local-test-key",
                    base_url=f"http://127.0.0.1:{port}/v1",
                ),
                protocol="responses",
            ),
            45,
        )
        assert result["model_requests"] == len(seen) == 2
        assert dispatcher.report["behavior"] == wire["behavior"]
        assert dispatcher.report["actions"][0]["effects"] == [
            {"key": "enabled", "value": "yes"}
        ]
        assert (
            dispatcher.report["facts"][0]["quote"] == case["reference_turns"][1]["text"]
        )
        assert saved[0]["actions"][0]["effects"][0]["key"] == "undeclared"
        assert client.rpc.process.returncode is not None
        assert not list((tmp_path / "jobs").iterdir())
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("saved_checkpoint", [False, True])
@pytest.mark.parametrize("malformation", ["nested_field", "review_envelope"])
async def test_real_sdk_verification_repair_keeps_typed_fields_and_saved_plan(
    source_binary, tmp_path, monkeypatch, saved_checkpoint, malformation
):
    from copy import deepcopy

    import jsonschema

    from api.services.agent_workbench.fixtures import FixtureCodec, FixtureSubmission
    from api.services.agent_workbench.test_tasks import StructuredDecision
    from api.tests.test_workbench_fixture_recovery import corrections, repair_payload
    from api.tests.test_workbench_test_compilation import fixture

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    case = fixture()
    plan = deepcopy(case["compiled"]["scenario"])
    codec = FixtureCodec(case["reference_turns"])
    wire = codec.encode(plan)
    value = corrections(codec)
    value["verdict"] = "corrected"
    if malformation == "nested_field":
        value["corrections"]["goals"] = deepcopy(wire["goals"])
        value["corrections"]["goals"][0]["quote_turn_id"] = "unknown-source"
        replacements = {
            ("corrections", "goals", 0, "quote_turn_id"): next(iter(codec.sources))
        }
    else:
        value = {
            **deepcopy(wire),
            "verdict": "corrected",
            "reason": "Independent review",
        }
        replacements = {(key,): None for key in wire}
        replacements[("corrections",)] = deepcopy(wire)
    recovery = FixtureSubmission(
        codec, draft=wire, original=plan, checkpoint=value if saved_checkpoint else None
    )
    dispatcher = StructuredDecision(
        "verify_customer_fixture",
        "Verify source evidence",
        recovery.schema,
        validate=recovery,
        recovery=recovery,
    )
    client = CodexClient(dispatcher, AsyncMock())
    seen = []

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        specs = {t["name"]: t for t in body["tools"]}
        if not recovery.pending:
            assert set(specs) == {"verify_customer_fixture", "repair_fixture"}
            assert (
                specs["verify_customer_fixture"]["parameters"]
                == codec.verification_schema
            )
            tool, arguments = "verify_customer_fixture", deepcopy(value)
        else:
            assert set(specs) == {"repair_fixture"}
            schema = specs["repair_fixture"]["parameters"]
            assert schema == recovery.spec()["inputSchema"]
            assert {tuple(e["path"]) for e in recovery.errors} == set(replacements)
            arguments = repair_payload(recovery, replacements)
            jsonschema.validate(arguments, schema)
            bad = deepcopy(arguments)
            bad["patches"][0]["value"] = 123
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)
            assert recovery.base == wire
            tool = "repair_fixture"
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
                json.dumps(codec.evidence),
                SimpleNamespace(
                    model="test-model",
                    api_key="local-test-key",
                    base_url=f"http://127.0.0.1:{port}/v1",
                ),
                protocol="responses",
            ),
            45,
        )
        assert result["model_requests"] == len(seen) == (1 if saved_checkpoint else 2)
        assert dispatcher.report["scenario"] == plan
        assert dispatcher.report["verdict"] == "corrected"
        assert recovery.base == wire
        assert not recovery.pending
        assert client.rpc.process.returncode is not None
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("failure_mode", ["once", "always", "non_decision"])
async def test_real_sdk_broken_response_stream_has_bounded_safe_recovery(
    source_binary, tmp_path, monkeypatch, failure_mode
):
    """Abort actual loopback HTTP chunks consumed by the pinned Rust SDK."""
    from api.services.agent_workbench import codex_client, test_tasks
    from api.services.agent_workbench.errors import ModelTransportError

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    original = codex_client.ModelGateway.handle
    attempts, upstream_calls, notices = [], [], []
    validator = Mock()

    async def handle(gateway, request):
        attempts.append(gateway)
        if failure_mode != "once" or len(attempts) == 1:
            body = await request.json()
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            item = {
                "id": "fc_partial",
                "type": "function_call",
                "call_id": "partial",
                "name": body["tools"][0]["name"],
                "arguments": "",
                "status": "in_progress",
            }
            frames = [
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_partial",
                        "status": "in_progress",
                        "output": [],
                    },
                },
                {"type": "response.output_item.added", "output_index": 0, "item": item},
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": item["id"],
                    "delta": '{"answer":"unfinished',
                },
            ]
            for frame in frames:
                await response.write(("data: " + json.dumps(frame) + "\n\n").encode())
            request.transport.abort()  # No final HTTP chunk or response.completed.
            return response
        return await original(gateway, request)

    monkeypatch.setattr(codex_client.ModelGateway, "handle", handle)

    async def upstream(request):
        body = await request.json()
        upstream_calls.append(body)
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_valid",
                        "call_id": "valid",
                        "name": "submit_record",
                        "arguments": '{"answer":"complete"}',
                        "status": "completed",
                    }
                ],
            }
        )

    async def event(kind, payload):
        notices.append((kind, payload))

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    llm = SimpleNamespace(
        model="test-model",
        api_key="local-only",
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
    )
    schema = test_tasks.object_schema({"answer": {"type": "string"}})
    try:
        if failure_mode == "non_decision":
            dispatcher = test_tasks.StructuredDecision(
                "submit_record", "rules", schema, validator
            )
            with pytest.raises(ModelTransportError):
                await asyncio.wait_for(
                    CodexClient(dispatcher, event).run(
                        "request", llm, protocol="responses"
                    ),
                    30,
                )
            assert len(attempts) == 1
        else:
            operation = test_tasks.decision(
                "submit_record",
                "rules",
                schema,
                {},
                llm,
                event,
                protocol="responses",
                validate=validator,
            )
            if failure_mode == "always":
                with pytest.raises(ModelTransportError):
                    await asyncio.wait_for(operation, 30)
            else:
                assert await asyncio.wait_for(operation, 30) == {"answer": "complete"}
            assert len(attempts) == 2
            retries = [
                p
                for k, p in notices
                if k == "model.progress" and p.get("phase") == "retrying"
            ]
            assert len(retries) == 1
            assert retries[0]["next_attempt"] == retries[0]["max_attempts"] == 2
        if failure_mode == "once":
            validator.assert_called_once_with({"answer": "complete"})
            assert len(upstream_calls) == 1
        else:
            validator.assert_not_called()
            assert not upstream_calls
        failures = [p for k, p in notices if k == "model.error"]
        assert failures and all(
            p["code"] == "model_stream_interrupted" for p in failures
        )
    finally:
        await runner.cleanup()


async def test_real_sdk_continuation_receives_latest_confirmed_facts_and_checkpoint(
    source_binary, tmp_path, monkeypatch
):
    from api.services.agent_workbench import test_tasks
    from api.services.agent_workbench.recovery import continued_test_input

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    confirmed_text = "Prepare cases using revision_b as reference only; derive customer facts from customer replies."
    root = {
        "id": "question",
        "status": "awaiting_input",
        "input": {"text": "Prepare cases", "mode": "test"},
        "output": {
            "outcome": "needs_input",
            "task_mode": "test",
            "question": "Which column?",
        },
    }
    latest = {
        "id": "latest",
        "status": "failed",
        "input": {
            "text": confirmed_text,
            "mode": "test",
            "prepare_only": True,
            "continue_test_job_id": "question",
            "asset_ids": ["source"],
        },
        "output": {
            "test_operation": "prepare",
            "preparation_receipts": [{"name": "retained", "suite_id": "checkpoint"}],
        },
    }
    current = {
        "id": "current",
        "lease_token": "lease",
        "input": {"text": "接着完成", "mode": "auto"},
        "output": {},
    }
    seen = []

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        tool = next(t for t in body["tools"] if t["name"] == "classify_request")
        assert tool["parameters"]["properties"]["source_job_id"]["enum"] == [
            None,
            "latest",
        ]
        user = next(
            item for item in reversed(body["input"]) if item.get("role") == "user"
        )
        evidence = json.loads(user["content"][0]["text"])
        assert evidence["current_request"] == "接着完成"
        assert evidence["recent_requests"][0]["request"] == confirmed_text
        assert evidence["recent_requests"][0]["pending_question"] is None
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_route",
                        "call_id": "route",
                        "status": "completed",
                        "name": "classify_request",
                        "arguments": json.dumps(
                            {
                                "intent": "test",
                                "source_job_id": "latest",
                                "reason": "resume confirmed preparation",
                            }
                        ),
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    llm = SimpleNamespace(
        model="test-model",
        api_key="local-only",
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
    )
    monkeypatch.setattr(test_tasks.db_client, "wb_get", AsyncMock(return_value=latest))

    async def restore(org, sid, jid, token, source, **kwargs):
        assert (org, sid, jid, token, source) == (
            7,
            "session",
            "current",
            "lease",
            "latest",
        )
        return {
            "input": continued_test_input(latest, current["input"]),
            "output": {"test_source_job_id": source},
        }

    monkeypatch.setattr(test_tasks.db_client, "wb_resume_test_request", restore)
    try:
        assert await asyncio.wait_for(
            test_tasks.route_test_request(
                7,
                "session",
                current,
                {"jobs": [current, latest, root]},
                llm,
                AsyncMock(),
                "responses",
            ),
            30,
        )
        assert len(seen) == 1
        assert current["input"]["text"] == confirmed_text
        assert current["input"]["prepare_only"] is True
        assert current["input"]["resume_test_job_id"] == "latest"
        assert current["input"]["asset_ids"] == ["source"]
    finally:
        await runner.cleanup()


async def test_real_sdk_binds_unselected_upload_with_confirmed_history_without_generating_fake_cases(
    source_binary, tmp_path, monkeypatch
):
    from copy import deepcopy

    from api.services.agent_workbench import test_sources

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "jobs"))
    asset = {
        "id": "uploaded",
        "name": "examples.csv",
        "sha256": "immutable",
        "status": "ready",
    }
    history = [
        {
            "job_id": "previous",
            "request": "Use revised; customer replies are evidence, operator replies only reference.",
            "source_manifest": [{"sha256": "immutable", "dialogue_column": "revised"}],
        }
    ]
    monkeypatch.setattr(
        test_sources.db_client,
        "wb_test_source_context",
        AsyncMock(return_value={"assets": [asset], "source_history": history}),
    )
    monkeypatch.setattr(
        test_sources.db_client, "wb_memory", AsyncMock(return_value={"entries": []})
    )
    monkeypatch.setattr(test_sources.db_client, "wb_get", AsyncMock(return_value=asset))
    job = {
        "id": "new",
        "lease_token": "lease",
        "input": {"text": "Prepare the supplied examples", "asset_ids": []},
        "output": {},
    }

    async def save(org, sid, jid, token, selection):
        return {
            "input": {
                **job["input"],
                "asset_ids": [a["id"] for a in selection["assets"]],
            },
            "output": {"test_source_selection": deepcopy(selection)},
        }

    monkeypatch.setattr(test_sources.db_client, "wb_set_test_sources", save)
    seen = []

    async def upstream(request):
        body = await request.json()
        seen.append(body)
        user = next(i for i in reversed(body["input"]) if i.get("role") == "user")
        evidence = json.loads(user["content"][0]["text"])
        assert evidence["source_history"] == history
        assert evidence["assets"] == [asset] and evidence["selected_asset_ids"] == []
        tool = next(t for t in body["tools"] if t["name"] == "resolve_test_sources")
        assert tool["parameters"]["properties"]["asset_ids"]["items"]["enum"] == [
            "uploaded"
        ]
        return web.json_response(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_bind",
                        "call_id": "bind",
                        "status": "completed",
                        "name": "resolve_test_sources",
                        "arguments": json.dumps(
                            {
                                "kind": "attachments",
                                "asset_ids": ["uploaded"],
                                "inline_quotes": [],
                                "question": "",
                            }
                        ),
                    }
                ],
            }
        )

    app = web.Application()
    app.router.add_post("/v1/responses", upstream)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    llm = SimpleNamespace(
        model="local-test",
        api_key="local-only",
        base_url=f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1",
    )
    try:
        selection = await asyncio.wait_for(
            test_sources.resolve_sources(7, "s", job, llm, AsyncMock(), "responses"), 45
        )
        assert job["input"]["asset_ids"] == ["uploaded"]
        assert selection["source_history"] == history
        assert (
            await test_sources.resolve_sources(
                7, "s", job, llm, AsyncMock(), "responses"
            )
            == selection
        )
        assert len(seen) == 1
    finally:
        await runner.cleanup()
