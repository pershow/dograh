import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import SimulationCase, TuneRequest
from api.services.agent_workbench import (
    review,
    simulation,
    test_tasks,
    tuning,
)
from api.services.agent_workbench.errors import WorkbenchError


def replay_case():
    return SimulationCase(
        name="identity",
        user_messages=["Yes", "Completed"],
        reference_dialogue="Assistant: Cedar Clinic?\nUser: Yes\nAssistant: Submit\nUser: Completed",
        reference_turns=[
            {"role": "assistant", "text": "Cedar Clinic?"},
            {"role": "user", "text": "Yes"},
            {"role": "assistant", "text": "Submit"},
            {"role": "user", "text": "Completed"},
        ],
    )


def candidate():
    return {
        "snapshot": {
            "template_context_variables": {
                "recipient": "Sample Clinic",
                "operator": "17",
            },
            "workflow_json": {
                "nodes": [{"id": "s", "data": {"greeting": "Hello {{recipient}}?"}}]
            },
        }
    }


async def test_replay_context_has_literal_provenance_without_future_state_or_agent_mutation(
    monkeypatch,
):
    original = candidate()
    frozen = deepcopy(original)
    driver = simulation.ConversationDriver(
        replay_case(), original, None, AsyncMock(), "chat"
    )
    bound = {
        "bindings": [
            {
                "variable": "recipient",
                "value": "Cedar Clinic",
                "quote": "Cedar Clinic?",
                "source_turn": 0,
                "reason": "named recipient",
            }
        ],
        "limitations": [],
    }
    call = AsyncMock(
        side_effect=[bound, {"supported": True, "reason": "static recipient identity"}]
    )
    monkeypatch.setattr(driver, "decide", call)
    await driver.prepare()
    assert original == frozen
    assert driver.replay_context["variables"] == {
        "recipient": "Cedar Clinic",
        "operator": "17",
    }
    evidence = call.call_args_list[0].args[3]
    assert (
        evidence["source_conversation"] == replay_case().model_dump()["reference_turns"]
    )
    assert evidence["eligible_variables"] == {"recipient": "Sample Clinic"}
    assert "Completed" not in str(driver.replay_context)
    assert driver.receipt()["replay_context"]["bindings"] == bound["bindings"]


@pytest.mark.parametrize(
    "change",
    [
        {"source_turn": 3},
        {"value": "Invented"},
        {"variable": "operator"},
        {"quote": "Submit"},
    ],
)
async def test_context_binding_rejects_future_or_invented_facts(monkeypatch, change):
    driver = simulation.ConversationDriver(
        replay_case(), candidate(), None, AsyncMock(), "chat"
    )
    binding = {
        "variable": "recipient",
        "value": "Cedar Clinic",
        "quote": "Cedar Clinic?",
        "source_turn": 0,
        "reason": "initial identity",
        **change,
    }
    monkeypatch.setattr(
        driver,
        "decide",
        AsyncMock(return_value={"bindings": [binding], "limitations": []}),
    )
    with pytest.raises(WorkbenchError, match="初始身份绑定"):
        await driver.prepare()


async def test_shared_alignment_cache_coalesces_identical_evidence_but_not_changed_inputs(
    monkeypatch,
):
    cache = simulation.DecisionCache()
    call = AsyncMock(
        return_value={
            "verdict": "aligned",
            "reason": "same subject",
            "actual_quote": "Hello?",
            "proposed_quote": "Yes",
        }
    )
    monkeypatch.setattr(test_tasks, "decision", call)
    drivers = [
        simulation.ConversationDriver(
            replay_case(), {}, None, AsyncMock(), "chat", decision_cache=cache
        )
        for _ in range(5)
    ]
    transcript = [{"role": "assistant", "text": "Hello?"}]
    await asyncio.gather(*(d.check(transcript, {"text": "Yes"}, []) for d in drivers))
    assert call.await_count == 1
    drivers[0].checks[0]["reason"] = "changed receipt"
    assert drivers[1].checks[0]["reason"] == "same subject"
    await drivers[0].check(transcript, {"text": "Yes, later"}, [])
    assert call.await_count == 2


async def test_identity_after_greetings_is_bound_but_later_operations_are_not(
    monkeypatch,
):
    case = replay_case()
    case.reference_turns = [
        type(case.reference_turns[0])(role="assistant", text="Hello"),
        type(case.reference_turns[0])(role="user", text="Hello"),
        *case.reference_turns,
    ]
    d = simulation.ConversationDriver(case, candidate(), None, AsyncMock(), "chat")
    binding = {
        "variable": "recipient",
        "value": "Cedar Clinic",
        "quote": "Cedar Clinic?",
        "source_turn": 2,
        "reason": "initial identity exchange",
    }
    monkeypatch.setattr(
        d,
        "decide",
        AsyncMock(
            side_effect=[
                {"bindings": [binding], "limitations": [], "status": "bound"},
                {
                    "supported": True,
                    "reason": "static intended recipient, before operations",
                },
            ]
        ),
    )
    await d.prepare()
    assert d.replay_context["variables"]["recipient"] == "Cedar Clinic"
    assert "Completed" not in str(d.replay_context)
    assert d.stop_reason is None
    # A literal quote alone is insufficient: an operation outcome cannot become
    # the called subject identity even if a planner proposes it.
    d = simulation.ConversationDriver(
        replay_case(), candidate(), None, AsyncMock(), "chat"
    )
    binding.update(value="Completed", quote="Completed", source_turn=3)
    monkeypatch.setattr(
        d,
        "decide",
        AsyncMock(
            side_effect=[
                {"bindings": [binding], "limitations": [], "status": "bound"},
                {
                    "supported": False,
                    "reason": "This is a later operation result, not identity",
                },
            ]
        ),
    )
    await d.prepare()
    assert d.replay_context["variables"]["recipient"] == "Sample Clinic"
    assert d.stop_reason == "replay_context_ambiguous"


async def test_alignment_cache_does_not_remember_transport_failure():
    cache = simulation.DecisionCache()
    call = AsyncMock(
        side_effect=[
            WorkbenchError("connection interrupted"),
            {"verdict": "misaligned"},
        ]
    )
    with pytest.raises(WorkbenchError):
        await cache.run("evidence", call)
    assert await cache.run("evidence", call) == {"verdict": "misaligned"}
    assert call.await_count == 2


async def test_uncertain_alignment_stops_without_claiming_confirmed_mismatch(
    monkeypatch,
):
    d = simulation.ConversationDriver(replay_case(), {}, None, AsyncMock(), "chat")
    monkeypatch.setattr(
        d,
        "decide",
        AsyncMock(
            return_value={
                "verdict": "uncertain",
                "reason": "missing",
                "actual_quote": "Hello?",
                "proposed_quote": "Yes",
            }
        ),
    )
    assert await d.next([{"role": "assistant", "text": "Hello?"}], 0) is None
    assert d.receipt()["status"] == "inconclusive"
    assert d.receipt()["stop_reason"] == "alignment_uncertain"
    assert d.checks[0]["delivered"] is False


async def test_accepted_read_only_review_survives_final_transport_ack_failure(
    monkeypatch,
):
    async def run(client, *_a, **_kw):
        await client.dispatcher.call("review", {"ok": True})
        raise WorkbenchError("Model connection interrupted.")

    call = AsyncMock(side_effect=run)
    monkeypatch.setattr(
        test_tasks.CodexClient, "run", lambda client, *a, **kw: call(client, *a, **kw)
    )
    value = await test_tasks.decision(
        "review", "rules", {"type": "object"}, {}, None, AsyncMock()
    )
    assert value == {"ok": True}
    assert call.await_count == 1


def test_tuning_request_rejects_ambiguous_scope():
    with pytest.raises(ValueError):
        TuneRequest(
            candidate_id="c", suite_id="last", test_job_id="all", idempotency_key="x"
        )
    assert (
        TuneRequest(candidate_id="c", test_job_id="all", idempotency_key="x").suite_id
        is None
    )


async def test_task_tuning_resolves_and_evaluates_every_batch(monkeypatch):
    suites = [
        {
            "id": f"s{i}",
            "fingerprint": f"f{i}",
            "payload": {
                "name": f"batch{i}",
                "cases": [
                    {"name": f"case{j}", "user_messages": ["hello"]} for j in range(n)
                ],
            },
        }
        for i, n in enumerate([2, 3, 1])
    ]
    source = {
        "id": "all",
        "status": "completed",
        "output": {
            "outcome": "test",
            "candidate_id": "c",
            "suite_ids": [s["id"] for s in suites],
            "summary": {"total": 6},
        },
    }

    async def get(org, sid, table, item):
        assert (org, sid) == (9, "session")
        return source if table == "jobs" else next(s for s in suites if s["id"] == item)

    monkeypatch.setattr(tuning.db_client, "wb_get", get)
    scope, frozen, _ = await tuning.resolve_scope(
        9, "session", {"candidate_id": "c", "test_job_id": "all"}
    )
    assert scope["suite_count"] == 3 and scope["case_count"] == 6

    async def evaluate(org, sid, job, cid, suite_id, config, **kwargs):
        suite = next(s for s in suites if s["id"] == suite_id)
        return {
            "id": "e" + suite_id,
            "suite_id": suite_id,
            "results": [
                {
                    "name": c["name"],
                    "status": "failed" if suite_id == "s0" else "passed",
                }
                for c in suite["payload"]["cases"]
            ],
        }

    call = AsyncMock(side_effect=evaluate)
    monkeypatch.setattr(tuning, "evaluate", call)
    outcome = await tuning.evaluate_scope(
        9, "session", {}, "c", frozen, SimpleNamespace(effective="runtime")
    )
    assert call.await_count == 3
    assert outcome["summary"] == {
        "passed": 4,
        "failed": 2,
        "error": 0,
        "inconclusive": 0,
        "total": 6,
    }
    assert outcome["suite_ids"] == ["s0", "s1", "s2"]
    assert len({id(c.kwargs["case_semaphore"]) for c in call.call_args_list}) == 1
    assert tuning.blocked_baseline(outcome, scope) is None
    outcome["results"][0].update(
        status="inconclusive", validity={"stop_reason": "alignment_uncertain"}
    )
    blocked = tuning.blocked_baseline(outcome, scope)
    assert blocked["tuning_scope"]["case_count"] == 6
    assert blocked["rounds"] == [] and blocked["improved"] is False
    assert blocked["blocked_cases"][0]["stop_reason"] == "alignment_uncertain"
    source["output"]["summary"]["total"] = 7
    with pytest.raises(WorkbenchError, match="数量"):
        await tuning.resolve_scope(
            9, "session", {"candidate_id": "c", "test_job_id": "all"}
        )


@pytest.mark.parametrize("regress_earlier_batch", [False, True])
async def test_tuning_round_checks_all_batches_with_frozen_inputs_and_rejects_regression(
    monkeypatch, regress_earlier_batch
):
    from api.services.agent_workbench import jobs

    suites = [
        {
            "id": f"s{i}",
            "fingerprint": f"f{i}",
            "payload": {"cases": [{"name": f"case{i}"}]},
        }
        for i in range(3)
    ]
    source = {
        "id": "all",
        "status": "completed",
        "output": {
            "outcome": "test",
            "candidate_id": "c",
            "suite_ids": [s["id"] for s in suites],
            "summary": {"total": 3},
        },
    }
    candidate_row = {
        "snapshot": {"workflow_json": {"nodes": []}},
        "fingerprint": "draft",
        "base_fingerprint": "draft",
    }

    async def get(org, sid, table, item):
        assert (org, sid) == (9, "session")
        if table == "jobs":
            return source
        if table == "candidates":
            return candidate_row
        return next(s for s in suites if s["id"] == item)

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(
        jobs.db_client,
        "wb_context",
        AsyncMock(
            return_value={
                "session": {},
                "target": candidate_row["snapshot"],
                "target_fingerprint": "draft",
            }
        ),
    )
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())
    builder = SimpleNamespace(
        llm=SimpleNamespace(provider="openai", api_key="test-only")
    )
    runtime = SimpleNamespace(effective=object(), snapshot={"role": "runtime"})
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(
            return_value=SimpleNamespace(
                effective=builder, snapshot={"role": "builder"}
            )
        ),
    )
    monkeypatch.setattr(jobs, "replay_models", AsyncMock(return_value=runtime))
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    build = AsyncMock(return_value={"candidate_id": "proposed"})
    monkeypatch.setattr(jobs, "_build", build)

    async def run(_org, _sid, _job, cid, suite_id, config, **kwargs):
        assert config is runtime.effective
        name = "case" + suite_id[-1]
        context = {
            "variables": {"recipient": "Frozen recipient"},
            "fingerprint": "frozen",
        }
        if cid == "proposed":
            assert kwargs["frozen_contexts"][(suite_id, name)] == context
        passed = (
            suite_id != "s2"
            if cid == "c"
            else not (regress_earlier_batch and suite_id == "s0")
        )
        return {
            "id": cid + suite_id,
            "suite_id": suite_id,
            "results": [
                {
                    "name": name,
                    "status": "passed" if passed else "failed",
                    "assertions": [
                        {
                            "kind": "contains",
                            "value": "Original requirement",
                            "passed": passed,
                        }
                    ],
                    "validity": {"status": "valid", "replay_context": context},
                }
            ],
        }

    evaluation = AsyncMock(side_effect=run)
    monkeypatch.setattr(tuning, "evaluate", evaluation)
    output = await jobs._perform(
        9,
        "session",
        {
            "id": "tune",
            "kind": "tune",
            "lease_token": "lease",
            "output": {},
            "input": {"candidate_id": "c", "test_job_id": "all", "max_rounds": 1},
        },
    )
    assert evaluation.await_count == 6
    for cid in ["c", "proposed"]:
        assert {c.args[4] for c in evaluation.call_args_list if c.args[3] == cid} == {
            "s0",
            "s1",
            "s2",
        }
    assert output["tuning_scope"]["case_count"] == 3
    assert output["rounds"][0]["accepted"] is not regress_earlier_batch
    assert output["improved"] is not regress_earlier_batch
    assert output["candidate_id"] == ("c" if regress_earlier_batch else "proposed")
    assert build.call_args.args[4] is builder
    assert build.call_args.kwargs["tuning"] is True


def test_timing_constraint_cannot_be_inferred_from_generated_policy():
    check = {
        "timing_scope": "same_turn",
        "timing_citation": {"id": "policy", "quote": "introduce"},
    }
    with pytest.raises(WorkbenchError, match="时序依据"):
        review.validate_check(
            check, {"policy": {"source": "policy", "text": "introduce"}}
        )


async def test_tuning_batch_failure_cancels_siblings(monkeypatch):
    started, cancelled = asyncio.Event(), asyncio.Event()
    suites = [
        {"id": "slow", "payload": {"cases": [{"name": "slow"}]}},
        {"id": "broken", "payload": {"cases": [{"name": "broken"}]}},
    ]

    async def run(_org, _sid, _job, _cid, suite_id, _config, **kwargs):
        if suite_id == "broken":
            await asyncio.wait_for(started.wait(), 1)
            raise WorkbenchError("lease lost")
        started.set()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), 2)
        finally:
            cancelled.set()

    monkeypatch.setattr(tuning, "evaluate", run)
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(
            tuning.evaluate_scope(
                1, "s", {}, "c", suites, SimpleNamespace(effective="runtime")
            ),
            3,
        )
    assert cancelled.is_set()


def test_proxy_credentials_are_redacted_for_all_supported_schemes():
    from api.services.agent_workbench.transport_diagnostics import safe_message

    for scheme in ["http", "https", "socks4", "socks4a", "socks5", "socks5h"]:
        message = safe_message(
            RuntimeError(
                f"{scheme}://operator:private-value@proxy.test:1080/path?token=private-query"
            )
        )
        assert message == "proxy.test"


async def test_frozen_replay_context_is_not_replanned_for_a_candidate(monkeypatch):
    d = simulation.ConversationDriver(
        replay_case(), candidate(), None, AsyncMock(), "chat"
    )
    frozen = {"variables": {"recipient": "Baseline"}, "fingerprint": "unchanged"}
    d.replay_context = deepcopy(frozen)
    decision = AsyncMock(side_effect=AssertionError("Must keep baseline context"))
    monkeypatch.setattr(d, "decide", decision)
    await d.prepare()
    assert d.replay_context == frozen
    decision.assert_not_awaited()


@pytest.mark.parametrize(
    "context",
    [
        {"status": "unresolved"},
        {"status": "bound", "verification": {"supported": False}},
    ],
)
async def test_restoring_uncertain_context_cannot_bypass_its_stop(monkeypatch, context):
    d = simulation.ConversationDriver(
        replay_case(), candidate(), None, AsyncMock(), "chat"
    )
    d.replay_context = deepcopy(context)
    decide = AsyncMock(side_effect=AssertionError("Frozen evidence must remain fixed"))
    monkeypatch.setattr(d, "decide", decide)
    await d.prepare()
    assert d.stop_reason == "replay_context_ambiguous"
    assert d.replay_context == context
    decide.assert_not_awaited()


@pytest.mark.parametrize("selector", ["rerun_test_job_id", "resume_test_job_id"])
async def test_missing_source_rows_remain_in_rerun_and_resume_coverage(
    monkeypatch, selector
):
    issue = {"source": {"row": 4}, "reason": "Customer column is empty"}
    get = AsyncMock(return_value={"output": {"import_issues": [issue]}})
    monkeypatch.setattr(test_tasks.db_client, "wb_get", get)
    retained = await test_tasks.retained_import_issues(
        1, "session", {selector: "original"}
    )
    get.assert_awaited_once_with(1, "session", "jobs", "original")
    assert retained == [issue]
    baseline = {
        "candidate_id": "c",
        "evaluation_ids": ["e"],
        "results": [],
        "summary": {"total": 0},
    }
    assert (
        tuning.blocked_baseline(baseline, {"import_issues": retained})["improved"]
        is False
    )
    retained[0]["reason"] = "changed"
    assert issue["reason"] == "Customer column is empty"
