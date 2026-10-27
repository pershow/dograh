"""Evidence, memory and read-only analysis boundaries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import context, jobs
from api.services.agent_workbench.codex_client import tool_content
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.model_gateway import chat_messages
from api.services.agent_workbench.tools import WorkbenchTools


@pytest.fixture
def evidence(monkeypatch):
    run = {
        "id": 42,
        "workflow_id": 7,
        "definition_id": 8,
        "state": "completed",
        "initial_context": {"customer": "Alice", "api_key": "never-send"},
        "gathered_context": {"intent": "callback"},
        "logs": [
            {
                "event": "fetch",
                "headers": {"Authorization": "secret"},
                "text": "Bearer abc123",
            }
        ],
        "transcript_url": "transcripts/42.txt",
        "storage_backend": "minio",
    }
    monkeypatch.setattr(
        context.db_client, "wb_agent_runs", AsyncMock(return_value=[run])
    )
    monkeypatch.setattr(
        context.db_client, "wb_run_definition", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        context,
        "download_run_transcript_text",
        AsyncMock(return_value="User: tomorrow\nAgent: callback"),
    )
    return run


async def test_latest_run_is_attributable_paginated_and_redacted(evidence):
    result = await context.read_agent_run(1, "session", section="transcript", limit=8)
    assert result["run"]["id"] == 42 and result["next_offset"] == 8
    assert result["text"] == "User: to"
    context.db_client.wb_agent_runs.assert_awaited_with(
        1, "session", run_id=None, limit=1
    )
    context.download_run_transcript_text.assert_awaited_once_with(
        transcript_url="transcripts/42.txt",
        storage_backend="minio",
        max_characters=2_000_000,
    )
    variables = await context.read_agent_run(1, "session", 42, "variables")
    assert "Alice" in variables["text"] and "never-send" not in variables["text"]
    logs = await context.read_agent_run(1, "session", 42, "logs")
    assert "secret" not in logs["text"] and "abc123" not in logs["text"]
    missing = await context.read_agent_run(1, "session", 42, "definition")
    assert "current draft is not historical evidence" in missing["warning"]


async def test_absent_run_never_downloads_another_transcript(evidence, monkeypatch):
    monkeypatch.setattr(context.db_client, "wb_agent_runs", AsyncMock(return_value=[]))
    with pytest.raises(WorkbenchError):
        await context.read_agent_run(1, "session", 999, "transcript")
    context.download_run_transcript_text.assert_not_called()


async def test_simulation_evidence_is_scoped_paged_and_redacted(monkeypatch):
    import json

    tools = WorkbenchTools(
        9,
        "session",
        {"id": "job", "lease_token": "lease", "input": {"mode": "analyze"}},
        {"target": {}, "target_fingerprint": "hash"},
    )
    get = AsyncMock(
        return_value={
            "id": "evaluation",
            "status": "completed",
            "summary": {"failed": 1},
            "results": [
                {
                    "name": "row 267",
                    "status": "failed",
                    "run_id": 42,
                    "source": {"row": 267, "column": "adjusted"},
                    "transcript": [{"role": "assistant", "text": "Actual response"}],
                    "assertions": [{"passed": False, "reason": "missing confirmation"}],
                    "gathered_context": {"api_key": "never-send"},
                    "reference_review": {"reason": "missing confirmation"},
                    "model_configuration": {"api_key": "never-send-model"},
                }
            ],
        }
    )
    monkeypatch.setattr(context.db_client, "wb_get", get)
    monkeypatch.setattr(context.db_client, "wb_emit", AsyncMock())
    index = await tools.read_evaluation("evaluation")
    assert json.loads(index["text"])["cases"][0]["case_index"] == 0
    first = await tools.read_evaluation("evaluation", 0, limit=30)
    rest = await tools.read_evaluation("evaluation", 0, offset=first["next_offset"])
    content = first["text"] + rest["text"]
    assert "Actual response" in content and "missing confirmation" in content
    assert "never-send" not in content and "model_configuration" not in content
    assert first["actual_run_id"] == 42 and first["evaluation_id"] == "evaluation"
    with pytest.raises(WorkbenchError, match="actual numeric run_id"):
        await tools.finish_analysis("已执行，编号是 evaluation")
    await tools.finish_analysis("运行 #42 的实际回复未满足确认要求。")
    get.assert_awaited_with(9, "session", "evaluations", "evaluation")
    with pytest.raises(WorkbenchError, match="Case index"):
        await tools.read_evaluation("evaluation", 1)
    get.side_effect = WorkbenchError("Item not found", 404)
    with pytest.raises(WorkbenchError, match="not found"):
        await tools.read_evaluation("foreign-evaluation", 0)


async def test_analysis_has_no_write_tools_and_auto_requires_explicit_build_intent(
    monkeypatch,
):
    ctx = {"target": {}, "target_fingerprint": "hash"}
    readonly = WorkbenchTools(1, "s", {"input": {"mode": "analyze"}}, ctx)
    assert not {"save_candidate", "save_suite"} & {s["name"] for s in readonly.specs()}
    auto = WorkbenchTools(1, "s", {"input": {}}, ctx)
    with pytest.raises(WorkbenchError, match="analysis"):
        await auto.call("save_candidate", {"code": "code", "summary": "changed"})


async def test_analysis_verifies_claims_against_actual_evidence_before_publishing(
    monkeypatch,
):
    from api.services.agent_workbench import test_tasks

    tools = WorkbenchTools(
        1,
        "s",
        {
            "id": "j",
            "lease_token": "l",
            "input": {"mode": "analyze", "text": "分析测试"},
        },
        {
            "target": {},
            "target_fingerprint": "h",
            "session": {"model_protocol": "chat"},
        },
    )
    emit = AsyncMock()
    monkeypatch.setattr(context.db_client, "wb_emit", emit)
    tools.analysis_evidence = [{"evaluation_id": "batch", "case_index": None}]
    with pytest.raises(WorkbenchError, match="只读取了测试索引"):
        await tools.finish_analysis("运行 #42 配置有问题")
    tools.simulation_run_ids.add(42)
    tools.analysis_evidence.append(
        {
            "actual_run_id": 42,
            "transcript": [{"role": "assistant", "text": "您好，请问需要什么帮助？"}],
        }
    )
    tools.analysis_reviewer = object()
    tools.analysis_event = AsyncMock()
    verify = AsyncMock(
        side_effect=[
            {"supported": False, "feedback": "措辞差异不能证明配置缺陷"},
            {"supported": True, "feedback": ""},
        ]
    )
    monkeypatch.setattr(test_tasks, "decision", verify)
    with pytest.raises(WorkbenchError, match="证据检查未通过"):
        await tools.finish_analysis("运行 #42 配置有问题")
    emit.assert_not_awaited()
    await tools.finish_analysis("运行 #42 回复是问候，尚不能据此确认配置有问题。")
    emit.assert_awaited_once()


@pytest.mark.parametrize("tuning", [False, True])
async def test_explicit_builds_do_not_offer_analysis_completion(tuning):
    tools = WorkbenchTools(
        1,
        "s",
        {"input": {"mode": "build"}},
        {"target": {}, "target_fingerprint": "hash"},
        tuning=tuning,
    )
    names = {s["name"] for s in tools.specs()}
    assert "save_candidate" in names
    assert ("save_suite" in names) is not tuning
    assert "finish_analysis" not in names
    with pytest.raises(WorkbenchError, match="unavailable"):
        await tools.call("finish_analysis", {"answer": "Done"})


async def test_auto_build_completion_does_not_tell_model_to_resave(monkeypatch):
    tools = WorkbenchTools(
        1,
        "s",
        {"id": "j", "lease_token": "lease", "input": {}},
        {"target": {}, "target_fingerprint": "hash"},
    )
    monkeypatch.setattr(context.db_client, "wb_heartbeat", AsyncMock())
    emit = AsyncMock()
    monkeypatch.setattr(context.db_client, "wb_emit", emit)
    assert "finish_analysis" in {s["name"] for s in tools.specs()}
    await tools.set_task_intent("build")
    tools.candidate_ids.append("candidate")
    tools.suite_ids.append("suite")
    with pytest.raises(WorkbenchError, match="already saved.*do not save"):
        await tools.call("finish_analysis", {"answer": "Done"})
    assert tools.analysis_answer is None
    assert all(call.args[2] != "assistant.message" for call in emit.await_args_list)


async def test_build_runner_uses_native_history_without_a_fixed_tail_and_keeps_analysis_readonly(
    monkeypatch,
):
    from api.services.agent_workbench import basis
    monkeypatch.setattr(basis, "ensure_basis", AsyncMock(return_value={"sources": [], "fingerprint": "basis", "target_fingerprint": "f", "memory_revision": "m", "warnings": []}))
    memory = {
        "messages": [
            {"role": "user", "text": "earlier"},
            {"role": "assistant", "text": "answer"},
        ]
    }
    monkeypatch.setattr(
        WorkbenchTools, "read_conversation", AsyncMock(return_value=memory)
    )
    monkeypatch.setattr(
        WorkbenchTools, "cached_run_evidence", AsyncMock(return_value=[])
    )

    async def load_agent_memory(self):
        self.agent_memory_view = {"confirmed": []}

    monkeypatch.setattr(WorkbenchTools, "load_agent_memory", load_agent_memory)

    async def complete(client, *args, **kwargs):
        client.dispatcher.analysis_answer = "Analysis complete"
        return {"messages": []}

    # Retain a recorded call while setting the explicit completion receipt.
    called = AsyncMock(side_effect=lambda *args, **kwargs: None)

    async def finish(self, *args, **kwargs):
        await called(*args, **kwargs)
        return await complete(self, *args, **kwargs)

    monkeypatch.setattr(jobs.CodexClient, "run", finish)
    ctx = {
        "session": {"model_protocol": "chat"},
        "target": {},
        "target_fingerprint": "hash",
        "jobs": [
            *[
                {
                    "id": f"analysis-{i}",
                    "status": "completed",
                    "output": {"outcome": "analysis"},
                }
                for i in range(5)
            ],
            {
                "id": "actual-test",
                "status": "completed",
                "output": {"evaluation_ids": ["real-evaluation"]},
            },
        ],
        "evaluations": [
            {
                "id": "real-evaluation",
                "job_id": "actual-test",
                "candidate_id": "tested-candidate",
                "status": "completed",
                "results": [{"run_id": 42}],
            }
        ],
        "assets": [
            {
                "id": "image",
                "name": "picture",
                "status": "ready",
                "parsed": {"kind": "image", "model_image": "PIXELS_NOT_AUTOMATIC"},
            }
        ],
    }
    job = {
        "id": "j",
        "lease_token": "lease",
        "input": {"mode": "analyze", "asset_ids": ["image"]},
    }
    result = await jobs._build(1, "s", job, ctx, SimpleNamespace(llm={}), "Why?")
    assert result["outcome"] == "analysis" and "candidate_id" not in result
    prompt = called.call_args.args[0]
    assert "Codex durable thread" in prompt
    WorkbenchTools.read_conversation.assert_not_awaited()
    assert "PIXELS_NOT_AUTOMATIC" not in prompt
    assert "images" not in called.call_args.kwargs
    assert "real-evaluation" in prompt and '"actual_run_ids": [42]' in prompt


async def test_history_scans_are_batched_but_specific_truncated_records_can_expand(
    monkeypatch,
):
    from api.services.agent_workbench import tools as tools_module

    read = AsyncMock(
        return_value={
            "messages": [{"sequence": 50, "truncated": True}],
            "before": 40,
            "guidance": "History.",
        }
    )
    monkeypatch.setattr(tools_module, "conversation", read)
    tools = WorkbenchTools(
        1,
        "s",
        {"input": {}, "output": {"history_before": 100}},
        {"target": {}, "target_fingerprint": "hash"},
    )
    await tools.read_conversation(before=90, limit=1)
    read.assert_awaited_with(1, "s", 90, 40)
    await tools.read_conversation(before=51, limit=1)
    read.assert_awaited_with(1, "s", 51, 1)
    await tools.read_conversation(before=40, limit=1)
    read.assert_awaited_with(1, "s", 40, 40)


async def test_completed_evidence_reuses_content_but_invalidates_changed_or_live_runs(
    evidence,
):
    evidence["is_completed"] = True
    first = await context.read_agent_run(1, "session", 42, "transcript")
    again = await context.read_agent_run(1, "session", 42, "transcript", cached=first)
    assert again["reused"] and again["text"] == first["text"]
    context.download_run_transcript_text.assert_awaited_once()
    evidence["logs"].append({"event": "updated"})
    assert (
        await context.read_agent_run(
            1, "session", 42, "transcript", cached=first, reuse_only=True
        )
        is None
    )
    changed = await context.read_agent_run(1, "session", 42, "transcript", cached=first)
    assert not changed["reused"]
    evidence["is_completed"] = False
    assert (
        await context.read_agent_run(
            1, "session", 42, "transcript", cached=changed, reuse_only=True
        )
        is None
    )


async def test_cached_definition_does_not_hide_draft_edits(evidence, monkeypatch):
    evidence["is_completed"] = True
    definition = {
        "id": 8,
        "status": "draft",
        "version_number": 1,
        "workflow_json": {"nodes": [], "edges": []},
        "template_context_variables": {},
    }
    monkeypatch.setattr(
        context.db_client, "wb_run_definition", AsyncMock(return_value=definition)
    )
    first = await context.read_agent_run(1, "session", 42, "definition")
    definition["template_context_variables"]["company"] = "Updated"
    assert (
        await context.read_agent_run(
            1, "session", 42, "definition", cached=first, reuse_only=True
        )
        is None
    )


async def test_followup_receives_prior_evidence_without_downloading_again(
    evidence, monkeypatch
):
    evidence["is_completed"] = True
    page = await context.read_agent_run(1, "s", 42, "transcript")
    monkeypatch.setattr(
        context.db_client,
        "wb_run_evidence",
        AsyncMock(return_value={"42:transcript:0:16000": page}),
    )
    tools = WorkbenchTools(
        1, "s", {"id": "new", "input": {}}, {"target": {}, "target_fingerprint": "hash"}
    )
    cached = await tools.cached_run_evidence()
    assert cached[0]["text"] == page["text"] and cached[0]["reused"]
    context.download_run_transcript_text.assert_awaited_once()


def test_requested_images_use_native_tool_content_and_chat_vision_conversion():
    assert tool_content({"name": "image", "_image": "data:image/png;base64,AA=="})[
        1
    ] == {
        "type": "inputImage",
        "imageUrl": "data:image/png;base64,AA==",
    }
    messages = chat_messages(
        {
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": [
                        {"type": "input_text", "text": "metadata"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                    ],
                },
            ]
        }
    )
    assert messages[0]["role"] == "tool" and messages[0]["content"] == "metadata"
    assert messages[1]["content"][1]["type"] == "image_url"


def test_repeated_logs_preserve_counts_and_time_ranges_without_dropping_distinct_errors():
    logs = {
        "realtime_feedback_events": [
            {"type": "error", "payload": {"error": "network"}, "timestamp": "t1"},
            {"type": "error", "payload": {"error": "auth"}, "timestamp": "t2"},
            {"type": "error", "payload": {"error": "network"}, "timestamp": "t3"},
        ]
    }
    grouped = context.compact_logs(logs)
    assert grouped["total_events"] == 3
    groups = grouped["logs"]["realtime_feedback_events"]
    assert len(groups) == 2 and groups[0]["count"] == 2
    assert (groups[0]["first_timestamp"], groups[0]["last_timestamp"]) == ("t1", "t3")
    assert len(logs["realtime_feedback_events"]) == 3


def test_chat_adapter_distinguishes_tool_progress_from_final_answer():
    from api.services.agent_workbench.model_gateway import responses_output

    assert responses_output({"content": "answer"}, [])[0]["phase"] == "final_answer"
    assert (
        responses_output(
            {
                "content": "Reading",
                "tool_calls": [
                    {
                        "id": "1",
                        "function": {"name": "read_agent_run", "arguments": "{}"},
                    }
                ],
            },
            ["read_agent_run"],
        )[0]["phase"]
        == "commentary"
    )


async def test_voice_reads_same_actual_run_evidence_and_records_source(
    evidence, monkeypatch
):
    from api.schemas.agent_workbench import VoiceToolRequest
    from api.services.agent_workbench.voice_commands import voice_command

    monkeypatch.setattr(context.db_client, "wb_context", AsyncMock(return_value={}))
    emit = AsyncMock()
    monkeypatch.setattr(context.db_client, "wb_emit", emit)
    result = await voice_command(
        1,
        "s",
        VoiceToolRequest(
            name="read_agent_run", call_id="voice", arguments={"section": "variables"}
        ),
    )
    assert result["run"]["id"] == 42 and "Alice" in result["text"]
    assert emit.call_args.args[3]["run_id"] == 42
