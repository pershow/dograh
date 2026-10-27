from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_behavior import AgentBehavior
from api.services.workflow.task_state import TaskState
from api.services.workflow.knowledge_evidence import (
    applicable_documents,
    bind_knowledge_evidence,
)
from api.services.workflow import action_guard
from api.services.agent_workbench.experiment_metrics import summarize_runs


def task():
    return TaskState(
        AgentBehavior(
            enabled=True,
            facts=[{"name": "day"}],
            stages=[{"name": "done", "required_facts": ["day"], "terminal": True}],
        )
    )


def test_fact_revision_only_changes_for_a_real_correction():
    state = task()
    state.observe_user("Friday please")
    assert state.update(facts={"day": "Friday"}, quote="Friday")["status"] == "updated"
    state.observe_user("Friday is correct")
    state.update(facts={"day": "Friday"}, quote="Friday")
    assert state.data["facts"]["day"]["revision"] == 1
    state.observe_user("Actually Thursday")
    state.update(facts={"day": "Thursday"}, quote="Thursday")
    assert state.data["facts"]["day"]["revision"] == 2


def test_stage_and_new_task_rejections_leave_state_unchanged():
    state = task()
    state.observe_user("finish")
    before = state.snapshot()
    assert state.update(stage="done", quote="finish")["status"] == "rejected"
    assert state.snapshot() == before
    assert state.update(new_task=True, quote="finish")["status"] == "rejected"
    assert state.snapshot() == before


def test_deliberate_repeat_gets_new_task_identity():
    state = task()
    state.observe_user("Friday please")
    state.update(facts={"day": "Friday"}, stage="done", quote="Friday")
    previous = state.data["task_id"]
    assert state.update(new_task=True, quote="Friday")["status"] == "rejected"
    state.observe_user("Another booking please")
    assert state.update(new_task=True, quote="Another booking")["status"] == "updated"
    assert state.data["task_id"] != previous
    assert not state.data["facts"]


def test_confirmation_requires_the_entire_current_utterance():
    state = task()
    for utterance in ("不要确认", "确认之前先改时间", "还没有confirm"):
        state.observe_user(utterance)
        assert not state.confirmation()
    state.observe_user("确认。")
    assert state.confirmation()


def test_knowledge_requires_applicability_and_matching_version():
    state = task()
    state.contract.knowledge_bindings = AgentBehavior(
        facts=[{"name": "day"}],
        knowledge_bindings={
            "doc": {"applies_when": {"day": "Friday"}, "version": "a" * 64}
        },
    ).knowledge_bindings
    assert applicable_documents(["doc"], state) == []
    state.observe_user("Friday")
    state.update(facts={"day": "Friday"}, quote="Friday")
    assert applicable_documents(["doc"], state) == ["doc"]
    result = bind_knowledge_evidence(
        {
            "chunks": [
                {
                    "document_uuid": "doc",
                    "document_version": "b" * 64,
                    "text": "Old rule",
                }
            ]
        },
        state,
    )
    assert result["status"] == "insufficient_evidence"
    assert not result["chunks"]


def tool():
    return SimpleNamespace(
        name="book",
        description="Book an appointment",
        tool_uuid="tool",
        definition={
            "type": "http_api",
            "config": {"parameters": []},
            "execution_policy": {
                "effect": "write",
                "success_path": "data.booked",
                "success_equals": True,
            },
        },
    )


def engine():
    state = task()
    state.observe_user("Friday")
    return SimpleNamespace(
        task_state=state,
        _call_context_vars={},
        _gathered_context={},
        _workflow_run_id=42,
    )


async def test_pending_or_unknown_action_never_reexecutes(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(
        action_guard.db_client,
        "claim_action",
        AsyncMock(
            return_value={
                "id": "op",
                "state": "unknown",
                "execute": False,
                "result": None,
            }
        ),
    )
    result = await action_guard.execute_guarded_http(
        engine=engine(), tool=tool(), arguments={}, organization_id=1, executor=execute
    )
    assert result["status"] == "unknown"
    execute.assert_not_awaited()


async def test_http_success_does_not_prove_business_success(monkeypatch):
    monkeypatch.setattr(
        action_guard.db_client,
        "claim_action",
        AsyncMock(
            return_value={
                "id": "op",
                "state": "executing",
                "execute": True,
                "owner_token": "owner",
            }
        ),
    )
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(action_guard.db_client, "finish_action", finish)
    execute = AsyncMock(return_value={"status": "success", "data": {"booked": False}})
    result = await action_guard.execute_guarded_http(
        engine=engine(), tool=tool(), arguments={}, organization_id=1, executor=execute
    )
    assert result["status"] == "unknown"
    assert finish.await_args.kwargs["state"] == "unknown"


async def test_cancelled_execution_is_persisted_as_unknown(monkeypatch):
    import asyncio

    monkeypatch.setattr(
        action_guard.db_client,
        "claim_action",
        AsyncMock(
            return_value={
                "id": "op",
                "state": "executing",
                "execute": True,
                "owner_token": "owner",
            }
        ),
    )
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(action_guard.db_client, "finish_action", finish)
    with pytest.raises(asyncio.CancelledError):
        await action_guard.execute_guarded_http(
            engine=engine(),
            tool=tool(),
            arguments={},
            organization_id=1,
            executor=AsyncMock(side_effect=asyncio.CancelledError),
        )
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["state"] == "unknown"


def test_missing_audio_is_not_zero_latency():
    result = summarize_runs([{"gathered_context": {}}])[0]
    assert result["missing_evidence"] == 1
    assert result["p95_ms"] is None
    assert result["meaningful_response_ms"] is None


async def test_reconciliation_uses_saved_business_identity_and_records_verified_failure(
    monkeypatch,
):
    guarded = tool()
    guarded.definition["execution_policy"].update(
        reconcile_tool_uuid="lookup",
        reconcile_arguments={"booking_id": "arguments.booking_id"},
        failure_path="data.rejected",
        failure_equals=True,
    )
    lookup = tool()
    lookup.tool_uuid = "lookup"
    lookup.definition["execution_policy"] = {"effect": "read"}
    monkeypatch.setattr(
        action_guard.db_client,
        "claim_action",
        AsyncMock(
            return_value={
                "id": "op",
                "state": "unknown",
                "execute": False,
                "owner_token": "owner",
                "arguments": {"booking_id": "original"},
                "result": None,
            }
        ),
    )
    monkeypatch.setattr(
        action_guard.db_client, "get_tool_by_uuid", AsyncMock(return_value=lookup)
    )
    finish = AsyncMock(return_value=True)
    monkeypatch.setattr(action_guard.db_client, "finish_action", finish)
    execute = AsyncMock(return_value={"status": "success", "data": {"rejected": True}})
    result = await action_guard.execute_guarded_http(
        engine=engine(), tool=guarded, arguments={}, organization_id=1, executor=execute
    )
    assert result["status"] == "failed"
    assert execute.await_args.kwargs["tool"] is lookup
    assert execute.await_args.kwargs["arguments"] == {"booking_id": "original"}
    assert finish.await_args.kwargs["state"] == "failed"
