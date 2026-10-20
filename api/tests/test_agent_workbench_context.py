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


async def test_analysis_has_no_write_tools_and_auto_requires_explicit_build_intent(
    monkeypatch,
):
    ctx = {"target": {}, "target_fingerprint": "hash"}
    readonly = WorkbenchTools(1, "s", {"input": {"mode": "analyze"}}, ctx)
    assert not {"save_candidate", "save_suite"} & {s["name"] for s in readonly.specs()}
    auto = WorkbenchTools(1, "s", {"input": {}}, ctx)
    with pytest.raises(WorkbenchError, match="analysis"):
        await auto.call("save_candidate", {"code": "code", "summary": "changed"})


async def test_build_runner_injects_both_roles_and_does_not_force_artifacts_for_analysis(
    monkeypatch,
):
    memory = {
        "messages": [
            {"role": "user", "text": "earlier"},
            {"role": "assistant", "text": "answer"},
        ]
    }
    monkeypatch.setattr(
        WorkbenchTools, "read_conversation", AsyncMock(return_value=memory)
    )

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
    assert "earlier" in prompt and "answer" in prompt
    assert "PIXELS_NOT_AUTOMATIC" not in prompt
    assert "images" not in called.call_args.kwargs


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
