"""Voice commands share attachment authorization and the frozen evaluation contract."""

from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import VoiceSelection, VoiceToolRequest
from api.services.agent_workbench import attachments, voice_commands
from api.services.agent_workbench.errors import WorkbenchError


@pytest.fixture
def context(monkeypatch):
    state = {
        "assets": [
            {
                "id": "a",
                "name": "rules.md",
                "status": "ready",
                "parsed": {"kind": "text"},
            }
        ],
        "jobs": [
            {
                "id": "tune",
                "kind": "tune",
                "status": "completed",
                "error": None,
                "input": {"suite_id": "frozen"},
                "output": {"candidate_id": "recommended", "improved": False},
            },
        ],
        "evaluations": [
            {
                "id": "rejected-eval",
                "candidate_id": "rejected",
                "suite_id": "frozen",
                "status": "completed",
                "summary": {"passed": 0},
            },
            {
                "id": "accepted-eval",
                "candidate_id": "recommended",
                "suite_id": "frozen",
                "status": "completed",
                "summary": {"passed": 2},
            },
        ],
        "candidates": [{"id": "rejected"}, {"id": "recommended"}],
        "suites": [{"id": "unrelated"}, {"id": "frozen"}],
    }
    monkeypatch.setattr(
        voice_commands.db_client, "wb_context", AsyncMock(return_value=state)
    )
    monkeypatch.setattr(
        voice_commands, "enqueue", AsyncMock(return_value={"id": "job"})
    )
    monkeypatch.setattr(voice_commands.db_client, "wb_emit", AsyncMock())
    return state


@pytest.mark.parametrize(
    "name,expected_kind",
    [
        ("submit_instruction", "build"),
        ("run_tests", "evaluate"),
        ("tune_agent", "tune"),
    ],
)
async def test_voice_commands_use_explicit_selection_and_preserve_attachments(
    context, name, expected_kind
):
    await voice_commands.voice_command(
        8,
        "session",
        VoiceToolRequest(
            name=name,
            call_id="same-call",
            candidate_id="chosen",
            suite_id="chosen-suite",
            asset_ids=["business-rules", "updated-prices"],
            arguments={"text": "build", "instruction": "fix prices"},
        ),
    )
    org, session, kind, payload, key = voice_commands.enqueue.call_args.args
    assert (org, session, kind, key) == (8, "session", expected_kind, "voice:same-call")
    assert payload["candidate_id"] == "chosen"
    if name != "submit_instruction":
        assert payload["suite_id"] == "chosen-suite"
    if name != "run_tests":
        assert payload["asset_ids"] == ["business-rules", "updated-prices"]


async def test_default_never_selects_rejected_tuning_candidate_or_unrelated_suite(
    context,
):
    await voice_commands.voice_command(
        8, "session", VoiceToolRequest(name="run_tests", call_id="call")
    )
    payload = voice_commands.enqueue.call_args.args[3]
    assert payload["candidate_id"] == "recommended" and payload["suite_id"] == "frozen"


async def test_incomplete_build_cannot_supply_default_candidate(context):
    context["jobs"][0]["status"] = "failed"
    with pytest.raises(WorkbenchError, match="Build a candidate"):
        await voice_commands.voice_command(
            8, "session", VoiceToolRequest(name="run_tests", call_id="call")
        )
    voice_commands.enqueue.assert_not_called()


async def test_status_reports_only_selected_evaluations_and_selected_file_metadata(
    context, monkeypatch
):
    get = AsyncMock(
        return_value={
            "id": "a",
            "name": "rules.md",
            "status": "ready",
            "parsed": {"kind": "text", "text": "private contents"},
            "object_key": "private-object",
        }
    )
    monkeypatch.setattr(voice_commands.db_client, "wb_get", get)
    result = await voice_commands.voice_command(
        8,
        "session",
        VoiceToolRequest(name="get_status", call_id="call", asset_ids=["a"]),
    )
    assert result["selection"]["attachments"] == [
        {"id": "a", "name": "rules.md", "kind": "text", "focused": True}
    ]
    assert [e["id"] for e in result["evaluations"]] == ["accepted-eval"]
    get.assert_awaited_once_with(8, "session", "assets", "a")


async def test_voice_status_distinguishes_analysis_from_build_and_returns_bounded_answer(
    context,
):
    context["jobs"][0]["output"] = {"outcome": "analysis", "answer": "x" * 5000}
    context["jobs"][0]["input"] = {"mode": "analyze"}
    result = await voice_commands.voice_command(
        8, "session", VoiceToolRequest(name="get_status", call_id="status")
    )
    job = result["jobs"][0]
    assert job["mode"] == "analyze" and job["output"]["outcome"] == "analysis"
    assert len(job["output"]["answer"]) == 4000


async def test_attachment_reader_is_selected_scoped_paginated_and_never_returns_object_keys(
    context, monkeypatch
):
    get = AsyncMock(
        return_value={
            "name": "rules.md",
            "status": "ready",
            "object_key": "hidden",
            "parsed": {
                "text": "x" * 8000 + "price",
                "full_text": "x" * 8000 + "price",
                "segments": [{"location": "page 1"}],
                "truncated": True,
            },
        }
    )
    monkeypatch.setattr(attachments.db_client, "wb_get", get)
    request = VoiceToolRequest(
        name="read_attachment",
        call_id="call",
        asset_ids=[],
        arguments={"asset_id": "a"},
    )
    first = await voice_commands.voice_command(8, "session", request)
    assert len(first["text"]) == 8000 and first["next_offset"] == 8000
    assert first["source_locations"] == ["page 1"] and not first["truncated_by_parser"]
    assert "object_key" not in first
    request.arguments["offset"] = first["next_offset"]
    assert (await voice_commands.voice_command(8, "session", request))[
        "text"
    ] == "price"
    get.assert_awaited_with(8, "session", "assets", "a")
    get.reset_mock()
    request.arguments["asset_id"] = "not-selected"
    with pytest.raises(WorkbenchError, match="not selected"):
        await voice_commands.voice_command(8, "session", request)
    get.assert_not_awaited()


async def test_unready_or_cross_session_voice_references_are_rejected(monkeypatch):
    get = AsyncMock(return_value={"status": "parsing"})
    monkeypatch.setattr(voice_commands.db_client, "wb_get", get)
    with pytest.raises(WorkbenchError, match="解析"):
        await voice_commands.validate_voice_selection(
            8, "session", VoiceSelection(asset_ids=["a"])
        )
    get.side_effect = WorkbenchError("not found", 404)
    with pytest.raises(WorkbenchError):
        await voice_commands.validate_voice_selection(
            8, "session", VoiceSelection(candidate_id="other-session")
        )
    get.assert_awaited_with(8, "session", "candidates", "other-session")


@pytest.mark.parametrize("offset,limit", [(-1, 20), (True, 20), (0, 24001), (0, 0)])
async def test_attachment_paging_bounds(offset, limit):
    with pytest.raises(WorkbenchError, match="offset"):
        await attachments.read_selected_attachment(
            8, "session", ["a"], "a", offset, limit
        )


async def test_codex_validation_failure_keeps_rpc_id_and_allows_repair():
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from api.services.agent_workbench.codex_client import CodexClient

    dispatcher = SimpleNamespace(
        call=AsyncMock(
            side_effect=[WorkbenchError("repair the edge"), {"candidate_id": "fixed"}]
        )
    )
    client = CodexClient(dispatcher, AsyncMock())
    requests = [
        {
            "id": "bad-save",
            "method": "item/tool/call",
            "params": {"tool": "save_candidate", "arguments": {}},
        },
        {
            "id": "fixed-save",
            "method": "item/tool/call",
            "params": {"tool": "save_candidate", "arguments": {}},
        },
    ]
    stdin = SimpleNamespace(write=Mock(), drain=AsyncMock())
    client.rpc.process = SimpleNamespace(stdin=stdin)
    for message in requests:
        await client.rpc.respond(message)
    await client.handle_notification(
        "turn/completed", {"turn": {"status": "completed"}}
    )
    assert (await client.done)["status"] == "completed"
    replies = [json.loads(call.args[0]) for call in stdin.write.call_args_list]
    assert replies[0]["id"] == "bad-save" and not replies[0]["result"]["success"]
    assert replies[0]["result"]["contentItems"][0]["text"] == "repair the edge"
    assert replies[1]["id"] == "fixed-save" and replies[1]["result"]["success"]


@pytest.mark.parametrize(
    "fatal,usable,expected",
    [(False, True, "warning"), (True, True, "error"), (False, False, "error")],
)
def test_voice_allows_provider_recovery_but_reports_terminal_errors(
    fatal, usable, expected
):
    from types import SimpleNamespace

    from api.services.agent_workbench.voice import voice_error_event

    event = voice_error_event(
        SimpleNamespace(fatal=fatal, processor=SimpleNamespace(is_usable=usable))
    )
    assert event["type"] == expected and "后台任务继续运行" in event["message"]
