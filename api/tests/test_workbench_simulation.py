from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import SimulationCase
from api.services.agent_workbench import evaluation, simulation
from api.services.agent_workbench.errors import InvalidDecision, WorkbenchError


def source_case(mode="dynamic"):
    return SimulationCase(
        name="Library",
        mode=mode,
        user_messages=["Reserve it", "On Tuesday"],
        reference_dialogue="Assistant: Which book?\nUser: Reserve it\nAssistant: When?\nUser: On Tuesday",
        reference_turns=[
            {"role": "assistant", "text": "Which book?"},
            {"role": "user", "text": "Reserve it"},
            {"role": "assistant", "text": "When?"},
            {"role": "user", "text": "On Tuesday"},
        ],
    )


def scenario():
    return {
        "facts": [
            {
                "id": "f1",
                "statement": "Tuesday",
                "provenance": "customer_quote",
                "quote": "On Tuesday",
                "reason": "source",
            }
        ],
        "goals": [{"id": "g1", "description": "reserve a book", "quote": "Reserve it"}],
        "behavior": "Ask to reserve the book",
        "limitations": [],
    }


def proposal(text="Tuesday"):
    return {
        "action": "reply",
        "text": text,
        "reason": "answer date",
        "goal_ids": ["g1"],
        "fact_ids": ["f1"],
        "simulated_action": "",
        "responds_to": "When?",
    }


def check(verdict="aligned", text="Tuesday"):
    return {
        "verdict": verdict,
        "reason": "causal check",
        "actual_quote": "When?",
        "proposed_quote": text,
    }


def test_single_steps_preserve_only_authentic_prefix_and_never_future_answer():
    case = source_case().model_dump()
    original = deepcopy(case)
    result = simulation.configure_cases([case], "single_step", [2])
    assert case == original
    assert len(result) == 1
    step = result[0]
    assert step["history"] == case["reference_turns"][:3]
    assert step["user_messages"] == ["On Tuesday"]
    assert step["source_turn"] == 2
    assert step["reference_turns"][-1]["role"] == "user"
    assert len(simulation.configure_cases([case], "single_step")) == 2
    with pytest.raises(WorkbenchError, match="轮次不存在"):
        simulation.configure_cases([case], "single_step", [30])


def test_quote_normalizes_only_whitespace_without_accepting_changed_identifiers():
    assert (
        simulation.original_quote("Hi. Which device?", "Hi.\n\nWhich device?")
        == "Hi.\n\nWhich device?"
    )
    assert simulation.original_quote("id 123", "id 124") is None
    assert simulation.original_quote("Which device?", "Which account?") is None


async def test_customer_never_receives_candidate_policy_or_reference_assistant(
    monkeypatch,
):
    driver = simulation.ConversationDriver(
        source_case(),
        {"snapshot": {"secret_policy": "never leak"}},
        None,
        AsyncMock(),
        "chat",
    )
    driver.scenario = scenario()
    calls = []

    async def decide(name, rules, schema, evidence, validate=None):
        calls.append((name, evidence))
        value = proposal() if name == "respond_as_customer" else check()
        if validate:
            validate(value)
        return value

    monkeypatch.setattr(driver, "decide", decide)
    assert await driver.next([{"role": "assistant", "text": "When?"}], 0) == "Tuesday"
    assert "secret_policy" not in repr(calls)
    assert "Which book?" not in repr(calls)
    assert driver.checks[0]["delivered"]
    assert driver.addressed == {"g1"}


async def test_dynamic_rejects_wrong_branch_and_repairs_customer_only(monkeypatch):
    driver = simulation.ConversationDriver(source_case(), {}, None, AsyncMock(), "chat")
    driver.scenario = scenario()
    decide = AsyncMock(
        side_effect=[proposal("Done"), check("misaligned", "Done"), proposal(), check()]
    )
    monkeypatch.setattr(driver, "decide", decide)
    assert await driver.next([{"role": "assistant", "text": "When?"}], 0) == "Tuesday"
    assert [c["delivered"] for c in driver.checks] == [False, True]
    assert driver.receipt()["status"] == "valid"


async def test_repeated_mismatch_stops_without_sending_invalid_reply(monkeypatch):
    driver = simulation.ConversationDriver(source_case(), {}, None, AsyncMock(), "chat")
    driver.scenario = scenario()
    monkeypatch.setattr(
        driver,
        "decide",
        AsyncMock(side_effect=[proposal("Done"), check("misaligned", "Done")] * 2),
    )
    assert await driver.next([{"role": "assistant", "text": "When?"}], 0) is None
    assert driver.receipt()["status"] == "invalid"
    assert not driver.addressed
    assert not any(c["delivered"] for c in driver.checks)


async def test_invalid_customer_structure_uses_repair_before_delivery(monkeypatch):
    driver = simulation.ConversationDriver(source_case(), {}, None, AsyncMock(), "chat")
    driver.scenario = scenario()
    decide = AsyncMock(
        side_effect=[InvalidDecision("invalid citation"), proposal(), check()]
    )
    monkeypatch.setattr(driver, "decide", decide)
    assert await driver.next([{"role": "assistant", "text": "When?"}], 0) == "Tuesday"
    assert decide.call_args_list[1].args[0] == "repair_customer_reply"
    assert [c["delivered"] for c in driver.checks] == [False, True]


async def test_pure_decision_retries_transport_but_not_semantic_errors(monkeypatch):
    from api.services.agent_workbench import test_tasks

    driver = simulation.ConversationDriver(source_case(), {}, None, AsyncMock(), "chat")
    call = AsyncMock(
        side_effect=[WorkbenchError("Responses request did not complete"), scenario()]
    )
    monkeypatch.setattr(test_tasks, "decision", call)
    monkeypatch.setattr(simulation.asyncio, "sleep", AsyncMock())
    assert await driver.decide("scenario", "rules", {}, {}) == scenario()
    assert call.await_count == 2
    call.reset_mock(side_effect=True)
    call.side_effect = InvalidDecision("bad source")
    with pytest.raises(InvalidDecision):
        await driver.decide("scenario", "rules", {}, {})
    assert call.await_count == 1


async def test_scripted_response_cannot_be_sent_after_context_diverges(monkeypatch):
    driver = simulation.ConversationDriver(
        source_case("scripted"), {}, None, AsyncMock(), "chat"
    )
    monkeypatch.setattr(
        driver, "decide", AsyncMock(return_value=check("misaligned", "Reserve it"))
    )
    assert await driver.next([{"role": "assistant", "text": "When?"}], 0) is None
    assert driver.stop_reason == "script_mismatch"


async def test_scenario_cannot_promote_assistant_claim_to_customer_fact(monkeypatch):
    driver = simulation.ConversationDriver(source_case(), {}, None, AsyncMock(), "chat")
    value = scenario()
    value["facts"][0]["quote"] = "Which book?"
    monkeypatch.setattr(driver, "decide", AsyncMock(return_value=value))
    with pytest.raises(WorkbenchError, match="客户事实"):
        await driver.prepare()


def test_invalid_evidence_cannot_be_promoted_to_pass_by_rereview_or_autotune():
    from api.services.agent_workbench.repair import failure_diagnostics

    outcome = {
        "name": "wrong context",
        "status": "failed",
        "validity": {"status": "invalid"},
        "assertions": [{"kind": "contains", "passed": False}],
    }
    result = evaluation.apply_reference_review(
        outcome, {"status": "passed", "checks": []}
    )
    assert result["status"] == "inconclusive"
    assert failure_diagnostics({"results": [outcome]}) == []


async def test_single_step_includes_start_node_and_seeds_only_authentic_messages(
    monkeypatch,
):
    case = SimulationCase.model_validate(
        simulation.configure_cases([source_case().model_dump()], "single_step", [1])[0]
    )
    graph = {
        "nodes": [
            {"id": "start", "type": "startCall", "data": {"prompt": "Which book?"}}
        ],
        "edges": [],
    }
    driver = simulation.ConversationDriver(
        case, {"snapshot": {"workflow_json": graph}}, None, AsyncMock(), "chat"
    )

    async def decide(name, rules, schema, evidence, validate=None):
        assert evidence["nodes"][0]["id"] == "start"
        assert "On Tuesday" not in repr(evidence)
        value = {
            "node_id": "start",
            "reason": "original opening",
            "history_quote": "Which book?",
            "ambiguous": False,
        }
        validate(value)
        return value

    monkeypatch.setattr(driver, "decide", decide)
    await driver.prepare()
    checkpoint = driver.checkpoint()
    assert checkpoint["current_node_id"] == "start"
    assert "Which book?" in repr(checkpoint["messages"])
    assert "Reserve it" not in repr(checkpoint["messages"])
    assert "On Tuesday" not in repr(checkpoint["messages"])


async def test_real_execution_stops_before_invalid_user_and_persists_live_evidence(
    monkeypatch,
):
    from api.services.workflow import text_chat_runner

    case = source_case("scripted")

    async def get(org, sid, table, key):
        return (
            {"snapshot": {"workflow_json": {"nodes": []}}, "fingerprint": "c"}
            if table == "candidates"
            else {
                "payload": {"name": "suite", "cases": [case.model_dump()]},
                "fingerprint": "s",
            }
        )

    monkeypatch.setattr(evaluation.db_client, "wb_get", get)
    for method, value in [
        ("wb_context", {"session": {"workflow_id": 1}}),
        ("wb_evaluation", {"id": "e"}),
        ("wb_evaluation_result", None),
        ("wb_create_run", 7),
        ("wb_heartbeat", None),
        ("wb_emit", None),
        ("update_workflow_run", None),
    ]:
        monkeypatch.setattr(evaluation.db_client, method, AsyncMock(return_value=value))
    execute = AsyncMock(
        return_value=SimpleNamespace(
            checkpoint={},
            gathered_context={},
            is_completed=False,
            assistant_text="When?",
            events=[],
            usage={},
        )
    )
    monkeypatch.setattr(text_chat_runner, "execute_text_chat_pending_turn", execute)
    monkeypatch.setattr(
        simulation.ConversationDriver,
        "decide",
        AsyncMock(return_value=check("misaligned", "Reserve it")),
    )
    result = await evaluation.evaluate(
        1,
        "s",
        {"id": "j", "lease_token": "l"},
        "c",
        "s",
        SimpleNamespace(llm=SimpleNamespace(model="runtime")),
        driver_llm=object(),
        reference_judge=AsyncMock(return_value={"status": "passed", "checks": []}),
    )
    assert (
        execute.await_count == 1
    )  # Only actual opening; never send the mismatched reply.
    assert result["results"][0]["status"] == "inconclusive"
    assert result["summary"]["invalid"] == 1
    writes = evaluation.db_client.wb_evaluation_result.call_args_list
    assert any(c.args[-2] == "running" and c.args[-3][0]["transcript"] for c in writes)
    assert writes[-1].args[-2] == "completed"
