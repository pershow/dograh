from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import SimulationCase
from api.services.agent_workbench import contract_review
from api.services.agent_workbench import test_compilation as compiler
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import apply_reference_review
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.simulation import ConversationDriver
from api.services.agent_workbench.tuning import actionable_partial, provisional_progress


def fixture():
    case = SimulationCase(
        name="customer fixture",
        mode="dynamic",
        user_messages=["我用手机，想了解费用"],
        reference_turns=[
            {"role": "assistant", "text": "您好"},
            {"role": "user", "text": "我用手机，想了解费用"},
        ],
        reference_dialogue="坐席：您好\n客户：我用手机，想了解费用",
    ).model_dump(mode="json")
    source = {
        "id": "basis:sop:0",
        "source": "basis",
        "source_kind": "material",
        "text": "必须说明费用；不得声称尚未发生的操作已完成。",
    }
    requirement = {
        "description": "说明费用",
        "applies_when": "客户询问费用",
        "expected_behavior": "依据业务资料说明费用",
        "severity": "blocking",
        "citations": [{"id": source["id"], "quote": "必须说明费用"}],
    }
    requirement = {"id": "req_" + fingerprint(requirement)[:20], **requirement}
    plan = {
        "facts": [
            {
                "id": "device",
                "statement": "使用手机",
                "provenance": "customer_quote",
                "quote": "我用手机",
                "reason": "客户自述",
                "applies_when": "从开始即成立，询问时告知",
            }
        ],
        "goals": [{"id": "cost", "description": "了解费用", "quote": "想了解费用"}],
        "behavior": "询问费用，按实际问题回复",
        "limitations": [],
        "initial_state": [{"key": "enabled", "value": "no"}],
        "actions": [
            {
                "id": "enable",
                "description": "收到开启要求后操作",
                "preconditions": [{"key": "enabled", "value": "no"}],
                "effects": [{"key": "enabled", "value": "yes"}],
                "result": "操作完成",
                "basis": "明确的合成测试环境",
            }
        ],
        "missing_information": [],
        "source_coverage": [
            {
                "turn": 1,
                "disposition": "fact",
                "scenario_ids": ["device", "cost"],
                "reason": "设备与诉求",
            }
        ],
    }
    contract = {
        "version": 1,
        "source_fingerprint": compiler.case_fingerprint(case),
        "requirements": [requirement],
        "sources": [source],
        "rubric_questions": [],
        "public_context": {},
        "scenario": plan,
        "status": "ready",
        "error": None,
    }
    contract["fingerprint"] = fingerprint(contract)
    case["compiled"] = contract
    return case


@pytest.mark.asyncio
async def test_preparation_checkpoints_survive_retry_chains_without_replacing_changed_sources(
    monkeypatch,
):
    from api.db import db_client
    from api.services.agent_workbench.recovery import restore_prepared_cases

    original = fixture()
    other = deepcopy(original)
    other["name"] = "changed source"
    cases = [deepcopy(original), deepcopy(other)]
    cases[0]["compiled"] = None
    cases[1]["compiled"] = None
    cases[1]["user_messages"] = ["新的客户事实"]
    rows = {
        ("jobs", "latest"): {
            "input": {"resume_test_job_id": "older"},
            "output": {"preparation_receipts": []},
        },
        ("jobs", "older"): {
            "input": {"resume_test_job_id": "latest"},
            "output": {
                "preparation_receipts": [
                    {"name": original["name"], "suite_id": "saved"},
                    {"name": other["name"], "suite_id": "saved"},
                ]
            },
        },
        ("suites", "saved"): {"payload": {"cases": [original, other]}},
    }

    async def get(org, sid, kind, id):
        assert (org, sid) == (7, "session")
        return rows[(kind, id)]

    monkeypatch.setattr(db_client, "wb_get", get)
    restored = await restore_prepared_cases(7, "session", "latest", cases)
    assert restored == {original["name"]}
    assert cases[0] == original
    assert cases[1]["compiled"] is None
    assert cases[1]["user_messages"] == ["新的客户事实"]
    cases[0]["compiled"]["status"] = "changed"
    assert original["compiled"]["status"] == "ready"


def test_prepared_case_detects_changed_source_and_changed_rubric():
    case = fixture()
    compiler.validate_compiled(case)
    altered = deepcopy(case)
    altered["user_messages"][0] = "我不想了解费用"
    with pytest.raises(WorkbenchError, match="原始用例"):
        compiler.validate_compiled(altered)
    altered = deepcopy(case)
    altered["compiled"]["requirements"][0]["expected_behavior"] = "不用说明费用"
    with pytest.raises(WorkbenchError, match="校验失败"):
        compiler.validate_compiled(altered)


@pytest.mark.asyncio
async def test_execute_prepared_task_uses_all_batches_without_client_selection(
    monkeypatch,
):
    from api.db import db_client
    from api.schemas.agent_workbench import Instruction
    from api.services.agent_workbench.test_tasks import prepared_test_input

    request = Instruction(
        mode="test",
        prepared_test_job_id="prepared",
        text="run all",
        idempotency_key="key",
    ).model_dump()
    ids = [f"batch-{i}" for i in range(117)]
    cases = {}
    for id in ids:
        case = fixture()
        case["name"] = id
        case["compiled"]["source_fingerprint"] = compiler.case_fingerprint(case)
        case["compiled"]["fingerprint"] = fingerprint(
            {k: v for k, v in case["compiled"].items() if k != "fingerprint"}
        )
        cases[id] = case
    prior = {
        "status": "completed",
        "output": {
            "outcome": "test_preparation",
            "candidate_id": "fixed",
            "suite_ids": ids,
            "compilation": {
                "total": len(ids),
                "rows": [
                    {"name": c["name"], "fingerprint": c["compiled"]["fingerprint"]}
                    for c in cases.values()
                ],
            },
        },
    }

    async def get(org, sid, kind, id):
        assert (org, sid) == (7, "session")
        if kind == "jobs":
            assert id == "prepared"
            return prior
        assert kind == "suites" and id in ids
        return {"payload": {"cases": [cases[id]]}}

    monkeypatch.setattr(db_client, "wb_get", get)
    resolved = await prepared_test_input(7, "session", "build", request)
    assert resolved["test_suite_ids"] == ids
    assert resolved["candidate_id"] == "fixed"
    assert request["test_suite_ids"] == []
    with pytest.raises(WorkbenchError, match="完整范围"):
        await prepared_test_input(
            7, "session", "build", {**request, "test_suite_ids": [ids[0]]}
        )
    prior["output"]["compilation"]["total"] += 1
    with pytest.raises(WorkbenchError, match="数量不一致"):
        await prepared_test_input(7, "session", "build", request)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_turn",
        "duplicate_turn",
        "unknown_fact",
        "invented_quote",
        "unknown_state",
    ],
)
def test_preparation_never_drops_source_or_invents_evidence(mutation):
    case = fixture()
    plan = case["compiled"]["scenario"]
    if mutation == "missing_turn":
        plan["source_coverage"] = []
    elif mutation == "duplicate_turn":
        plan["source_coverage"] *= 2
    elif mutation == "unknown_fact":
        plan["source_coverage"][0]["scenario_ids"] = ["invented"]
    elif mutation == "invented_quote":
        plan["facts"][0]["quote"] = "我用电脑"
    else:
        plan["actions"][0]["effects"][0]["key"] = "unobserved"
    with pytest.raises(WorkbenchError):
        compiler.validate_plan(plan, case["reference_turns"])


def test_rubric_rejects_reference_or_generated_policy_as_authority():
    case = fixture()
    r = {k: v for k, v in case["compiled"]["requirements"][0].items() if k != "id"}
    record = deepcopy(case["compiled"]["sources"][0])
    for source, kind in (("policy", "material"), ("basis", "reference")):
        record.update(source=source, source_kind=kind)
        with pytest.raises(WorkbenchError, match="原始业务"):
            compiler.validate_rubric(
                {"requirements": [r], "questions": []}, {record["id"]: record}
            )


def driver_for(case):
    return ConversationDriver(
        SimulationCase.model_validate(case),
        {"snapshot": {"template_context_variables": {"operator": "new-value"}}},
        SimpleNamespace(model="test"),
        AsyncMock(),
        "chat",
    )


@pytest.mark.asyncio
async def test_runtime_uses_prepared_fixture_without_replanning_or_leaking_private_facts():
    driver = driver_for(fixture())
    driver.decide = AsyncMock(side_effect=AssertionError("must not recompile"))
    await driver.prepare()
    assert driver.state == {"enabled": "no"}
    assert driver.replay_context["variables"] == {"operator": "new-value"}
    assert "device" not in driver.replay_context["variables"]
    assert driver.goal_states == {"cost": "unraised"}
    assert driver.next_state(
        {"action_ids": ["enable"], "simulated_action": "开启"}
    ) == {"enabled": "yes"}
    assert driver.state == {"enabled": "no"}  # proposal not delivered
    driver.state["enabled"] = "yes"
    with pytest.raises(WorkbenchError, match="前置条件"):
        driver.next_state({"action_ids": ["enable"], "simulated_action": "再次开启"})


@pytest.mark.asyncio
async def test_raising_concern_does_not_resolve_it():
    driver = driver_for(fixture())
    await driver.prepare()
    proposal = {
        "action": "reply",
        "text": "我想了解费用",
        "reason": "先问费用",
        "goal_ids": ["cost"],
        "fact_ids": [],
        "simulated_action": "",
        "action_ids": [],
        "responds_to": "您好",
        "goal_updates": [{"id": "cost", "state": "raised", "evidence": "我想了解费用"}],
    }

    async def decide(name, rules, schema, evidence, validate=None):
        result = (
            proposal
            if name == "respond_as_customer"
            else {
                "verdict": "aligned",
                "reason": "合理询问",
                "actual_quote": "您好",
                "proposed_quote": "我想了解费用",
                "state_claims": [],
                "omitted_transition_ids": [],
            }
        )
        if validate:
            validate(result)
        return result

    driver.decide = decide
    assert (
        await driver.next([{"role": "assistant", "text": "您好"}], 0) == "我想了解费用"
    )
    assert driver.goal_states == {"cost": "raised"}
    assert not driver.addressed
    assert driver.receipt()["pending_goals"] == ["cost"]


@pytest.mark.asyncio
async def test_state_claim_cannot_pass_alignment_without_its_frozen_transition():
    driver = driver_for(fixture())
    await driver.prepare()
    proposal = {
        "action": "reply",
        "text": "我已经开启了",
        "action_ids": [],
        "simulated_action": "",
    }
    check = {
        "verdict": "aligned",
        "reason": "claimed completion",
        "actual_quote": "请开启",
        "proposed_quote": "已经开启",
        "state_claims": [{"key": "enabled", "value": "yes", "quote": "已经开启"}],
        "omitted_transition_ids": [],
    }
    driver.decide = AsyncMock(return_value=check)
    result = await driver.check([{"role": "assistant", "text": "请开启"}], proposal)
    assert result["verdict"] == "misaligned"
    assert driver.state == {"enabled": "no"}
    proposal.update(action_ids=["enable"], simulated_action="开启")
    driver.decide = AsyncMock(
        return_value={**check, "verdict": "aligned", "reason": "declared transition"}
    )
    assert (await driver.check([{"role": "assistant", "text": "请开启"}], proposal))[
        "verdict"
    ] == "aligned"
    assert driver.state == {"enabled": "no"}
    with pytest.raises(WorkbenchError, match="未投递"):
        driver.next_state({**proposal, "action": "blocked"})


def assessment(status="failed", independent=True):
    case = fixture()
    requirement = case["compiled"]["requirements"][0]
    sources = case["compiled"]["sources"]
    evidence = {
        "records": sources
        + [
            {
                "id": "actual:0",
                "source": "actual",
                "role": "assistant",
                "text": "费用不用管，再见",
                "turn": 2,
            }
        ],
        "validity": {"status": "valid"},
        "candidate_fingerprint": "candidate",
    }
    report = {
        "checks": [
            {
                "criterion_id": requirement["id"],
                "status": status,
                "reason": "未说明费用就结束",
                "applicability": "已询问费用",
                "layer": "runtime",
                "independent_of_simulation": independent,
                "citations": [{"id": "actual:0", "quote": "费用不用管，再见"}],
            }
        ]
    }
    return case, report, evidence


def test_criterion_review_requires_every_frozen_id_and_actual_evidence():
    case, report, evidence = assessment()
    requirements = case["compiled"]["requirements"]
    records = {r["id"]: r for r in evidence["records"]}
    contract_review.validate_report(report, requirements, records, evidence["validity"])
    for checks in ([], report["checks"] * 2):
        with pytest.raises(WorkbenchError, match="全部冻结断言"):
            contract_review.validate_report(
                {"checks": checks}, requirements, records, evidence["validity"]
            )
    report["checks"][0]["citations"] = requirement_citations = requirements[0][
        "citations"
    ]
    assert requirement_citations
    with pytest.raises(WorkbenchError, match="实际坐席"):
        contract_review.validate_report(
            report, requirements, records, evidence["validity"]
        )


def test_independent_failure_survives_later_simulation_uncertainty():
    case, report, evidence = assessment()
    evidence["validity"] = {"status": "invalid"}
    reviewed = contract_review.finalize(
        report, report, case["compiled"]["requirements"], evidence, case["compiled"]
    )
    outcome = apply_reference_review({"validity": evidence["validity"]}, reviewed)
    assert outcome["status"] == "failed"
    assert (
        outcome["assertions"][0]["criterion_id"]
        == case["compiled"]["requirements"][0]["id"]
    )
    baseline = {"results": [{**outcome, "name": "case", "suite_id": "s"}]}
    assert actionable_partial(baseline)


def test_not_applicable_does_not_manufacture_a_pass():
    case, report, evidence = assessment("not_applicable")
    reviewed = contract_review.finalize(
        report, report, case["compiled"]["requirements"], evidence, case["compiled"]
    )
    assert reviewed["status"] == "inconclusive"
    assert reviewed["criterion_counts"] == {"not_applicable": 1}


def test_suite_coverage_keeps_unexercised_rules_and_missing_reviews_visible():
    cases = [fixture(), fixture()]
    cases[1]["name"] = "second"
    rid = cases[0]["compiled"]["requirements"][0]["id"]
    results = [
        {
            "name": c["name"],
            "reference_review": {
                "checks": [{"criterion_id": rid, "status": "not_applicable"}]
            },
        }
        for c in cases
    ]
    receipt = contract_review.coverage(cases, results)
    assert receipt["observed"] == 0 and receipt["criterion_count"] == 1
    assert receipt["rows"][0]["not_applicable"] == 2
    assert receipt["rows"][0]["passed"] == 0
    results.pop()
    receipt = contract_review.coverage(cases, results)
    assert receipt["rows"][0]["test_error"] == 1
    results[0]["reference_review"]["checks"][0]["status"] = "failed"
    receipt = contract_review.coverage(cases, results)
    assert receipt["observed"] == 1 and receipt["rows"][0]["failed"] == 1


def test_provisional_comparison_keeps_unknowns_and_rejects_lost_coverage():
    case, report, evidence = assessment()
    review = contract_review.finalize(
        report, report, case["compiled"]["requirements"], evidence, case["compiled"]
    )
    review["checks"].append({"criterion_id": "unknown", "status": "uncovered"})
    baseline = {
        "results": [{"name": "case", "suite_id": "s", "reference_review": review}]
    }
    candidate = deepcopy(baseline)
    candidate["results"][0]["reference_review"]["checks"][0]["status"] = "passed"
    assert provisional_progress(baseline, candidate)
    assert (
        candidate["results"][0]["reference_review"]["checks"][1]["status"]
        == "uncovered"
    )
    candidate["results"][0]["reference_review"]["checks"].pop()
    assert not provisional_progress(baseline, candidate)
    candidate = deepcopy(baseline)
    candidate["results"][0]["reference_review"]["checks"][0]["status"] = (
        "not_applicable"
    )
    assert not provisional_progress(baseline, candidate)


def test_tuning_index_counts_full_corpus_but_keeps_transcripts_in_paged_evidence():
    from api.services.agent_workbench.tuning import repair_index

    _, report, evidence = assessment()
    case = fixture()
    review = contract_review.finalize(
        report, report, case["compiled"]["requirements"], evidence, case["compiled"]
    )
    result = {
        "name": "sample",
        "status": "failed",
        "validity": {"status": "valid"},
        "assertions": [],
    }
    result = apply_reference_review(result, review)
    rows = [
        {
            **deepcopy(result),
            "name": f"case-{i}",
            "evaluation_id": f"batch-{i // 7}",
            "transcript": "large evidence" * 10000,
        }
        for i in range(29)
    ]
    unrelated = deepcopy(rows[0])
    unrelated["name"] = "invalid simulator"
    unrelated["validity"]["status"] = "invalid"
    unrelated["assertions"][0]["independent_of_mismatch"] = False
    rows.append(unrelated)
    indexed = repair_index({"results": rows})
    assert indexed["case_count"] == 30
    assert len(indexed["evaluation_ids"]) == 5
    assert len(indexed["failure_groups"]) == 1
    group = indexed["failure_groups"][0]
    assert group["occurrences"] == 29
    assert [x["case_index"] for x in group["examples"]] == [0, 1, 2]
    assert group["examples"][0]["evaluation_id"] == "batch-0"
    assert "large evidence" not in str(indexed)


@pytest.mark.asyncio
async def test_unrelated_authority_question_does_not_block_customer_fixture():
    case = fixture()
    frozen = case.pop("compiled")
    basis = {
        "sources": [
            {
                "id": "sop",
                "kind": "material",
                "title": "SOP",
                "text": frozen["sources"][0]["text"],
                "content_hash": "source",
            }
        ]
    }
    rubric = {
        "requirements": [
            {k: v for k, v in frozen["requirements"][0].items() if k != "id"}
        ],
        "questions": ["另一个设备分支的重试上限未说明"],
    }
    calls = []

    async def decide(name, rules, schema, evidence, **kwargs):
        calls.append((name, evidence))
        value = deepcopy(
            rubric
            if "rubric" in name
            else {"capabilities": []}
            if "environment" in name
            else {"verdict": "supported", "reason": "核对原文后支持", "patches": []}
            if name == "verify_customer_fixture"
            else frozen["scenario"]
        )
        kwargs["validate"](value)
        return value

    progress = AsyncMock()
    cases, receipt = await compiler.compile_cases(
        [case],
        {"snapshot": {"template_context_variables": {}}},
        basis,
        decide,
        concurrency=1,
        progress=progress,
    )
    assert receipt["ready"] == 1 and receipt["needs_input"] == 0
    assert cases[0]["compiled"]["rubric_questions"] == rubric["questions"]
    customer_packets = [e for name, e in calls if "fixture" in name]
    assert all(
        "requirements" not in e and "candidate" not in e for e in customer_packets
    )
    assert all(
        e["preparation_scope"]
        == {
            "unit": "one_source_conversation",
            "case_name": case["name"],
            "remaining_rows_owner": "orchestrator",
        }
        for e in customer_packets
    )
    assert progress.await_count == 1


def test_structured_fixture_corrections_cannot_escape_or_break_source_coverage():
    case = fixture()
    draft = case["compiled"]["scenario"]
    fixed = compiler.verified_plan(
        draft,
        {
            "verdict": "corrected",
            "reason": "具体说明",
            "patches": [
                {"path": ["behavior"], "value": "询问实际收费，未解释时继续保留顾虑"}
            ],
        },
        case["reference_turns"],
    )
    assert fixed["behavior"] != draft["behavior"]
    assert fixed["source_coverage"] == draft["source_coverage"]
    for path, value in (
        (["candidate", "prompt"], "bad"),
        (["source_coverage"], []),
        (["facts", "0", "quote"], "编造的原话"),
    ):
        with pytest.raises(WorkbenchError):
            compiler.verified_plan(
                draft,
                {
                    "verdict": "corrected",
                    "reason": "invalid",
                    "patches": [{"path": path, "value": value}],
                },
                case["reference_turns"],
            )


def test_compact_review_still_requires_all_criteria_and_checks_corrections():
    case, report, evidence = assessment()
    requirements = case["compiled"]["requirements"]
    records = {r["id"]: r for r in evidence["records"]}
    verified = {
        "reviews": [
            {
                "criterion_id": requirements[0]["id"],
                "verdict": "supported",
                "reason": "证据充分",
                "check": None,
            }
        ]
    }
    assert (
        contract_review.verified_report(
            report, verified, requirements, records, evidence["validity"]
        )
        == report
    )
    with pytest.raises(WorkbenchError, match="全部冻结断言"):
        contract_review.verified_report(
            report, {"reviews": []}, requirements, records, evidence["validity"]
        )


@pytest.mark.asyncio
async def test_environment_upgrade_reuses_frozen_rubric_and_customer_draft():
    case = fixture()
    old = deepcopy(case["compiled"])
    basis = {
        "sources": [
            {
                "id": "sop",
                "kind": "material",
                "title": "SOP",
                "text": old["sources"][0]["text"],
                "content_hash": "source",
            }
        ]
    }
    records = compiler.source_records(basis)
    case["compiled"]["business_sources_fingerprint"] = fingerprint(
        sorted(records, key=lambda r: r["id"])
    )
    case["compiled"]["fingerprint"] = fingerprint(
        {k: v for k, v in case["compiled"].items() if k != "fingerprint"}
    )
    # Discovery may add testing instructions or duplicate historical requests.
    # This must not regenerate or weaken the original frozen business contract.
    basis["sources"].append(
        {
            "id": "test-command",
            "kind": "user_request",
            "title": "Test request",
            "text": "Continue preparing all tests",
            "content_hash": "extra",
        }
    )
    calls = []

    async def decide(name, rules, schema, evidence, **kwargs):
        calls.append(name)
        if name in {"compile_test_environment", "verify_test_environment"}:
            value = {"capabilities": []}
        else:
            assert name == "verify_customer_fixture"
            assert evidence["draft"] == old["scenario"]
            assert "requirements" not in evidence
            value = {
                "verdict": "supported",
                "reason": "independently checked",
                "patches": [],
            }
        kwargs["validate"](value)
        return value

    prepared, receipt = await compiler.compile_cases(
        [case],
        {"snapshot": {}},
        basis,
        decide,
        concurrency=1,
        progress=AsyncMock(),
        retry_preparation=True,
    )
    assert receipt["ready"] == 1
    compiled = prepared[0]["compiled"]
    assert compiled["version"] == 2
    assert compiled["requirements"] == old["requirements"]
    assert compiled["scenario"] == old["scenario"]
    assert compiled["source_fingerprint"] == old["source_fingerprint"]
    assert calls == [
        "compile_test_environment",
        "verify_test_environment",
        "verify_customer_fixture",
    ]
    compiler.validate_compiled(prepared[0])
    repeated, _ = await compiler.compile_cases(
        prepared,
        {"snapshot": {}},
        basis,
        AsyncMock(side_effect=AssertionError("must reuse")),
        concurrency=1,
        progress=AsyncMock(),
        retry_preparation=True,
    )
    assert repeated == prepared


@pytest.mark.asyncio
async def test_customer_and_alignment_receive_only_delivered_operation_history():
    driver = driver_for(fixture())
    await driver.prepare()
    driver.checks = [
        {"turn": 1, "delivered": True, "proposal": {"action_ids": ["enable"]}},
        {"turn": 2, "delivered": False, "proposal": {"action_ids": ["enable"]}},
    ]
    driver.state["enabled"] = "yes"
    history = [{"role": "assistant", "text": "您还有什么问题？"}]
    packets = []

    async def decide(name, rules, schema, evidence, validate=None):
        packets.append(evidence)
        value = (
            {
                "action": "reply",
                "text": "我想了解费用",
                "reason": "ask",
                "fact_ids": [],
                "goal_ids": ["cost"],
                "action_ids": [],
                "simulated_action": "",
                "responds_to": "您还有什么问题",
                "goal_updates": [
                    {"id": "cost", "state": "raised", "evidence": "我想了解费用"}
                ],
            }
            if name == "respond_as_customer"
            else {
                "verdict": "aligned",
                "reason": "supported",
                "actual_quote": "您还有什么问题",
                "proposed_quote": "我想了解费用",
                "state_claims": [],
                "omitted_transition_ids": [],
            }
        )
        validate(value)
        return value

    driver.decide = decide
    assert await driver.next(history, 2) == "我想了解费用"
    assert len(packets) == 2
    assert all(
        p["delivered_actions"] == [{"turn": 1, "action_ids": ["enable"]}]
        for p in packets
    )
    assert driver.state["enabled"] == "yes"


@pytest.mark.asyncio
async def test_compiled_response_binds_actual_reply_without_model_copy_and_checks_state():
    import jsonschema

    from api.services.agent_workbench.simulation import COMPILED_REPLY

    case = fixture()
    case["compiled"]["scenario"]["actions"].append(
        {
            "id": "observe",
            "description": "observe existing state",
            "preconditions": [],
            "effects": [],
            "result": "read only",
            "basis": "existing state",
        }
    )
    case["compiled"]["fingerprint"] = fingerprint(
        {k: v for k, v in case["compiled"].items() if k != "fingerprint"}
    )
    driver = driver_for(case)
    await driver.prepare()
    assert "responds_to" not in COMPILED_REPLY["properties"]
    proposal = {
        "action": "reply",
        "text": "还没开启，我想了解费用",
        "reason": "ask",
        "fact_ids": [],
        "goal_ids": ["cost"],
        "action_ids": [],
        "simulated_action": "",
        "goal_updates": [{"id": "cost", "state": "raised", "evidence": "我想了解费用"}],
    }
    latest = "您已经开启了吗？"

    async def decide(name, rules, schema, evidence, validate=None):
        value = deepcopy(
            proposal
            if name == "respond_as_customer"
            else {
                "verdict": "aligned",
                "reason": "read existing state",
                "actual_quote": latest,
                "proposed_quote": "还没开启",
                "state_claims": [
                    {"key": "enabled", "value": "no", "quote": "还没开启"}
                ],
                "omitted_transition_ids": [],
            }
        )
        jsonschema.validate(value, schema)
        validate(value)
        return value

    driver.decide = decide
    assert (
        await driver.next([{"role": "assistant", "text": latest}], 0)
        == proposal["text"]
    )
    delivered = driver.checks[-1]
    assert delivered["proposal"]["responds_to"] == latest
    assert delivered["delivered"] and delivered["state_after"] == {"enabled": "no"}
    # An observation cannot satisfy a missing state-changing operation.
    driver.decide = AsyncMock(
        return_value={
            "verdict": "aligned",
            "reason": "claims completion",
            "actual_quote": latest,
            "proposed_quote": "已经开启",
            "state_claims": [{"key": "enabled", "value": "yes", "quote": "已经开启"}],
            "omitted_transition_ids": [],
        }
    )
    checked = await driver.check(
        [{"role": "assistant", "text": latest}],
        {
            **proposal,
            "text": "已经开启",
            "action_ids": ["observe"],
            "simulated_action": "observe",
        },
    )
    assert checked["verdict"] == "misaligned"
    assert driver.state == {"enabled": "no"}
