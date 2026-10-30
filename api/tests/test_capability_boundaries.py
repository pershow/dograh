from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pipecat.frames.frames import BotStartedSpeakingFrame, TranscriptionFrame
from pipecat.turns.types import ProcessFrameResult

from api.schemas.agent_behavior import AgentBehavior
from api.services.observability.call_events import runtime
from api.services.pipecat.speech_units import (
    SpeechUnitsUserTurnStartStrategy,
    speech_unit_count,
)
from api.services.workflow.action_guard import acknowledge_proposals
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.task_state import TaskState


async def test_short_chinese_interrupts_but_single_ack_does_not():
    strategy = SpeechUnitsUserTurnStartStrategy(min_words=2)
    strategy.trigger_user_turn_started = AsyncMock()
    strategy.trigger_reset_aggregation = AsyncMock()
    await strategy.process_frame(BotStartedSpeakingFrame())
    assert (
        await strategy.process_frame(TranscriptionFrame("嗯", "user", ""))
        == ProcessFrameResult.CONTINUE
    )
    assert (
        await strategy.process_frame(TranscriptionFrame("不是", "user", ""))
        == ProcessFrameResult.STOP
    )
    assert speech_unit_count("don't stop") == 2
    assert speech_unit_count("X200 手机") == 3
    assert speech_unit_count("，。？！") == 0


def test_terminal_success_requires_matching_action_and_current_facts():
    state = TaskState(
        AgentBehavior(
            enabled=True,
            facts=[{"name": "day"}],
            tool_policies={
                "book": {
                    "effect": "write",
                    "success_path": "data.booked",
                    "required_facts": ["day"],
                }
            },
            stages=[{"name": "done", "terminal": True, "required_actions": ["book"]}],
        )
    )
    state.observe_user("Friday")
    state.update(facts={"day": "Friday"}, quote="Friday")
    actions = {
        "op": {
            "task_id": state.data["task_id"],
            "tool_uuid": "book",
            "status": "unknown",
            "fact_revisions": {"day": 1},
        }
    }
    assert (
        state.update(stage="done", quote="Friday", actions=actions)["status"]
        == "rejected"
    )
    actions["op"]["status"] = "succeeded"
    state.observe_user("Thursday")
    state.update(facts={"day": "Thursday"}, quote="Thursday")
    assert (
        state.update(stage="done", quote="Thursday", actions=actions)["status"]
        == "rejected"
    )


async def test_only_complete_exact_output_acknowledges_action_proposal(monkeypatch):
    from api.services.workflow import action_guard

    mark = AsyncMock(return_value=True)
    monkeypatch.setattr(action_guard.db_client, "mark_action_proposed", mark)
    state = TaskState(AgentBehavior(enabled=True))
    state.data["action_proposals"] = {
        "op": {"text": "请确认预约周四。", "arguments_hash": "hash"}
    }
    engine = SimpleNamespace(
        task_state=state,
        _get_organization_id=AsyncMock(return_value=7),
        record_context=Mock(),
    )
    await acknowledge_proposals(engine, "请确认预约")
    mark.assert_not_awaited()
    await acknowledge_proposals(engine, "请确认预约周四。")
    assert mark.await_args.kwargs["organization_id"] == 7
    assert not state.data["action_proposals"]


async def test_internal_evidence_survives_external_sink_configuration_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        runtime, "load_settings", AsyncMock(side_effect=RuntimeError("unavailable"))
    )
    publish = Mock()
    monkeypatch.setattr(runtime.delivery, "submit", publish)
    engine = SimpleNamespace(
        runtime_config={"runtime_evidence_enabled": True},
        _gathered_context={},
        record_context=Mock(),
    )
    session = await runtime.create_session(
        organization_id=7, run_id=8, workflow_id=9, engine=engine
    )
    assert session is not None and session.capture_internal
    await session.finish()
    assert engine.record_context.call_args.args[0]["runtime_evidence"]["events"]
    publish.assert_not_called()


def test_transfer_preparation_keeps_source_state_and_filters_fact_types():
    engine = PipecatEngine.__new__(PipecatEngine)
    engine._business_states = {}
    engine._active_agent = SimpleNamespace()
    engine.task_state = TaskState(
        AgentBehavior(enabled=True, facts=[{"name": "device"}, {"name": "count"}])
    )
    engine.task_state.observe_user("X200 two")
    engine.task_state.update(facts={"device": "X200", "count": "two"}, quote="X200 two")
    before = engine.task_state.snapshot()
    destination = SimpleNamespace(
        workflow_id=9,
        business_state=None,
        workflow_configurations={
            "agent_behavior": {
                "enabled": True,
                "facts": [
                    {"name": "device"},
                    {"name": "count", "value_type": "number"},
                ],
            }
        },
    )
    state = engine.state_for_agent(destination)
    assert state.data["facts"]["device"]["value"] == "X200"
    assert "count" not in state.data["facts"]
    assert state.data["task_id"] == before["task_id"]
    assert engine.task_state.snapshot() == before


def test_pending_user_speech_invalidates_confirmation_and_fact_updates():
    state = TaskState(AgentBehavior(enabled=True, facts=[{"name": "day"}]))
    state.observe_user("确认")
    assert state.confirmation()
    state.observe_activity()
    assert not state.confirmation()
    assert state.update(facts={"day": "Friday"}, quote="确认")["status"] == "rejected"
    assert not state.data["facts"]


async def test_new_speech_during_claim_releases_action_before_http(monkeypatch):
    from api.services.workflow import action_guard
    from api.tests.test_business_task_state import engine, tool

    runtime = engine()
    runtime.task_state.observe_user("确认")

    async def claim(**kwargs):
        runtime.task_state.observe_activity()
        return {
            "id": "op",
            "state": "executing",
            "execute": True,
            "owner_token": "owner",
        }

    monkeypatch.setattr(action_guard.db_client, "claim_action", claim)
    release = AsyncMock()
    monkeypatch.setattr(action_guard.db_client, "release_unsubmitted_action", release)
    execute = AsyncMock()
    result = await action_guard.execute_guarded_http(
        engine=runtime, tool=tool(), arguments={}, organization_id=7, executor=execute
    )
    assert result["reason"] == "conversation_changed_before_dispatch"
    release.assert_awaited_once()
    execute.assert_not_called()
