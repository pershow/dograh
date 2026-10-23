"""Public activity events preserve progress without exposing tool data or reasoning."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from api.services.agent_workbench.codex_client import CodexClient
from api.services.agent_workbench.errors import WorkbenchError


async def test_tool_receipts_have_matching_ids_without_raw_arguments_or_results():
    event = AsyncMock()
    client = CodexClient(
        SimpleNamespace(call=AsyncMock(return_value={"secret": "private-result"})),
        event,
    )
    result = await client.handle_request(
        "item/tool/call",
        {
            "tool": "read_node",
            "callId": "call-a",
            "arguments": {"key": "private-input"},
        },
    )
    assert result["success"]
    assert [c.args[0] for c in event.await_args_list] == [
        "tool.started",
        "tool.completed",
    ]
    assert all(c.args[1]["call_id"] == "call-a" for c in event.await_args_list)
    assert "private" not in str(event.await_args_list)
    assert event.await_args_list[-1].args[1]["elapsed_seconds"] >= 0


async def test_failure_does_not_get_a_success_receipt_and_internal_errors_are_safe():
    for error, expected in [
        (WorkbenchError("Invalid node"), "Invalid node"),
        (RuntimeError("secret"), "Tool failed internally"),
    ]:
        event = AsyncMock()
        client = CodexClient(SimpleNamespace(call=AsyncMock(side_effect=error)), event)
        result = await client.handle_request(
            "item/tool/call",
            {
                "tool": "save_candidate",
                "callId": "call-a",
                "arguments": {},
            },
        )
        assert not result["success"]
        assert [c.args[0] for c in event.await_args_list] == [
            "tool.started",
            "tool.failed",
        ]
        assert expected in event.await_args_list[-1].args[1]["error"]
        assert "secret" not in str(event.await_args_list)


async def test_public_message_stream_snapshots_are_throttled_and_final_replaces_them(
    monkeypatch,
):
    from api.services.agent_workbench import codex_client

    event = AsyncMock()
    client = CodexClient(SimpleNamespace(), event)
    clock = [100.0]
    monkeypatch.setattr(codex_client.time, "monotonic", lambda: clock[0])
    base = {"turnId": "turn", "itemId": "message"}
    await client.handle_notification(
        "item/started",
        {
            "turnId": "turn",
            "item": {"id": "message", "type": "agentMessage", "phase": "commentary"},
        },
    )
    for delta in ["我会", "先检查", "附件"]:
        await client.handle_notification(
            "item/agentMessage/delta", {**base, "delta": delta}
        )
    assert event.await_count == 1
    clock[0] += 1.1
    await client.handle_notification("item/agentMessage/delta", {**base, "delta": "。"})
    assert event.await_args.args[1]["text"] == "我会先检查附件。"
    await client.handle_notification(
        "item/completed",
        {
            "turnId": "turn",
            "item": {
                "id": "message",
                "type": "agentMessage",
                "text": "我会先检查附件。",
                "phase": "commentary",
            },
        },
    )
    assert [c.args[0] for c in event.await_args_list] == [
        "assistant.message.progress",
        "assistant.message.progress",
        "assistant.message",
    ]
    assert all(c.args[1]["message_id"] == "turn:message" for c in event.await_args_list)
    assert not client.streaming_messages
    assert client.messages == ["我会先检查附件。"]


async def test_reasoning_deltas_are_not_exposed_as_public_progress():
    event = AsyncMock()
    client = CodexClient(SimpleNamespace(), event)
    for method in ["item/reasoning/textDelta", "item/reasoning/summaryTextDelta"]:
        await client.handle_notification(
            method, {"itemId": "r", "delta": "private reasoning"}
        )
    event.assert_not_called()


async def test_cancellation_leaves_no_false_tool_completion():
    event = AsyncMock()
    client = CodexClient(
        SimpleNamespace(call=AsyncMock(side_effect=asyncio.CancelledError())), event
    )
    try:
        await client.handle_request(
            "item/tool/call", {"tool": "read_node", "arguments": {}}
        )
    except asyncio.CancelledError:
        pass
    assert [c.args[0] for c in event.await_args_list] == ["tool.started"]
