"""Shared evidence stays lossless and bounded across storage and task recovery."""

# ruff: noqa: F811 -- imported pytest fixtures

from copy import deepcopy

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from api.db.models import WorkbenchCompilationArtifactModel, WorkbenchSuiteModel
from api.schemas.agent_workbench import SimulationCase, SimulationSuite
from api.services.agent_workbench import test_compilation as compiler
from api.services.agent_workbench import test_tasks as tasks
from api.services.agent_workbench.compiled_artifacts import (
    MAX_PAYLOAD_BYTES,
    REFERENCE_KEY,
    artifact_ids,
    hydrate_suite,
    payload_bytes,
    split_suite,
)
from api.services.agent_workbench.contract_review import same_review_contract
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import EVALUATION_VERSION
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.preparation_questions import question_id
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401
from api.tests.test_workbench_test_compilation import fixture


def seal(case):
    compiled = case["compiled"]
    compiled["source_fingerprint"] = compiler.case_fingerprint(case)
    compiled["fingerprint"] = fingerprint(
        {k: v for k, v in compiled.items() if k != "fingerprint"}
    )
    return case


def reviewed_case(name="case", *, large=False):
    case = fixture()
    case["name"] = name
    compiled = case["compiled"]
    question = "是否已确认测试输入？"
    text = "已确认使用所选客户回复。" + ("历史依据。" * 13000 if large else "")
    compiled["sources"][0]["text"] += "业务资料。" * (13000 if large else 1)
    compiled["rubric_questions"] = [question]
    compiled["question_review"] = {
        "version": 1,
        "input_fingerprint": fingerprint(text),
        "sources": [{"id": "basis:confirmation", "source": "basis", "text": text}],
        "resolutions": [
            {
                "question_id": question_id(question),
                "status": "resolved",
                "reason": "用户已确认",
                "citations": [
                    {"id": "basis:confirmation", "quote": "已确认使用所选客户回复。"}
                ],
            }
        ],
    }
    return seal(case)


@pytest.mark.parametrize("count", [1, 23])
def test_large_shared_evidence_is_lossless_and_every_case_survives_batching(count):
    cases = [reviewed_case(str(i), large=True) for i in range(count)]
    original = deepcopy(cases)
    assert payload_bytes(cases[0]) > MAX_PAYLOAD_BYTES
    batches = tasks.pack_suites(cases, "完整测试", "保留原始条件与断言")
    assert [c for b in batches for c in b["cases"]] == original
    assert len(batches) == (count + 19) // 20
    combined = {}
    for batch in batches:
        stored, artifacts = split_suite(batch)
        assert payload_bytes(stored) <= MAX_PAYLOAD_BYTES
        assert all(payload_bytes(a) <= MAX_PAYLOAD_BYTES for a in artifacts.values())
        assert len(artifacts) == 4  # Shared by all cases, not repeated per source row.
        combined.update(artifacts)
        assert hydrate_suite(stored, artifacts) == batch
        assert SimulationSuite.model_validate(batch).model_dump() == batch
    assert len(combined) == 4
    assert cases == original


def test_suite_limit_measures_normalized_utf8_storage_exactly():
    case = SimulationCase(
        name="边界", user_messages=["你好"], reference_dialogue="参考"
    ).model_dump()
    suite = {
        "usage": "development",
        "name": "测试 · 1",
        "instructions": "说明",
        "cases": [case],
    }
    case["initial_context"]["padding"] = ""
    case["initial_context"]["padding"] = "x" * (
        MAX_PAYLOAD_BYTES - payload_bytes(suite)
    )
    assert payload_bytes(suite) == MAX_PAYLOAD_BYTES
    assert tasks.pack_suites([case], "测试", "说明") == [suite]
    case["initial_context"]["padding"] += "x"
    with pytest.raises(ValidationError, match="exceeds 256 KB"):
        tasks.pack_suites([case], "测试", "说明")


def test_shared_artifacts_have_the_same_size_cap_and_do_not_bypass_assertions():
    case = reviewed_case()
    case["compiled"]["sources"][0]["text"] += "x" * MAX_PAYLOAD_BYTES
    seal(case)
    with pytest.raises(WorkbenchError, match="artifact sources exceeds 256 KB"):
        tasks.pack_suites([case], "test")
    case = reviewed_case(large=True)
    criterion = case["compiled"]["requirements"][0]
    criterion["citations"][0]["quote"] = "原文没有这个断言"
    criterion["id"] = (
        "req_" + fingerprint({k: v for k, v in criterion.items() if k != "id"})[:20]
    )
    seal(case)
    with pytest.raises(ValidationError):
        tasks.pack_suites([case], "test")


@pytest.mark.parametrize(
    "damage", ["missing", "tampered", "conflicting", "unknown_field"]
)
def test_storage_references_fail_closed(damage):
    stored, artifacts = split_suite({"name": "test", "cases": [reviewed_case()]})
    compiled = stored["cases"][0]["compiled"]
    digest = compiled[REFERENCE_KEY]["sources"]
    if damage == "missing":
        artifacts.pop(digest)
    elif damage == "tampered":
        artifacts[digest][0]["text"] = "replaced"
    elif damage == "conflicting":
        compiled["sources"] = []
    else:
        compiled[REFERENCE_KEY]["scenario"] = digest
    with pytest.raises(WorkbenchError):
        hydrate_suite(stored, artifacts)


async def test_database_deduplicates_and_hydrates_with_session_and_tenant_fences(
    db_session, async_session, wb
):
    org, user, sid = wb
    cases = [reviewed_case(str(i), large=True) for i in range(23)]
    batches = tasks.pack_suites(cases, "test")
    saved = [await db_session.wb_save_suite(org, sid, batch) for batch in batches]
    artifacts = (
        await async_session.scalars(
            select(WorkbenchCompilationArtifactModel).where(
                WorkbenchCompilationArtifactModel.session_id == sid
            )
        )
    ).all()
    assert len(artifacts) == 4
    for row, payload in zip(saved, batches):
        stored = await async_session.get(WorkbenchSuiteModel, row["id"])
        assert payload_bytes(stored.payload) <= MAX_PAYLOAD_BYTES
        assert artifact_ids(stored.payload) == {a.fingerprint for a in artifacts}
        assert row["payload"] == payload
        assert row["fingerprint"] == fingerprint(payload)
        assert await db_session.wb_get(org, sid, "suites", row["id"]) == row
    context = await db_session.wb_context(org, sid)
    assert {r["id"]: r for r in context["suites"]} == {r["id"]: r for r in saved}
    compact = await db_session.wb_context(org, sid, compact=True)
    assert all(
        set(c) == {"name"} for s in compact["suites"] for c in s["payload"]["cases"]
    )
    with pytest.raises(WorkbenchError):
        await db_session.wb_get(org + 999, sid, "suites", saved[0]["id"])
    other = await db_session.wb_create_session(org, user, name="Other session")
    with pytest.raises(WorkbenchError):
        await db_session.wb_get(org, other["id"], "suites", saved[0]["id"])
    forged = WorkbenchSuiteModel(
        session_id=other["id"],
        payload=deepcopy(stored.payload),
        fingerprint=stored.fingerprint,
    )
    async_session.add(forged)
    await async_session.flush()
    with pytest.raises(WorkbenchError, match="依据缺失"):
        await db_session.wb_get(org, other["id"], "suites", forged.id)
    artifacts[0].payload = {"tampered": True}
    await async_session.flush()
    with pytest.raises(WorkbenchError, match="完整性校验失败"):
        await db_session.wb_get(org, sid, "suites", saved[0]["id"])


async def test_legacy_inline_suite_remains_readable_and_migrates_only_on_new_save(
    db_session, async_session, wb
):
    org, _, sid = wb
    payload = {"name": "legacy", "cases": [reviewed_case(large=True)]}
    legacy = WorkbenchSuiteModel(
        session_id=sid, payload=deepcopy(payload), fingerprint=fingerprint(payload)
    )
    async_session.add(legacy)
    await async_session.flush()
    row = await db_session.wb_get(org, sid, "suites", legacy.id)
    assert row["payload"] == payload
    copied = await db_session.wb_save_suite(org, sid, row["payload"])
    assert copied["fingerprint"] == legacy.fingerprint
    assert (await db_session.wb_get(org, sid, "suites", copied["id"]))[
        "payload"
    ] == payload
    assert legacy.payload == payload


def changed_review(case):
    case = deepcopy(case)
    review = case["compiled"]["question_review"]
    review["input_fingerprint"] = "new task sources"
    review["sources"][0]["text"] += "\n新增的历史来源页" * 3000
    review["resolutions"][0]["reason"] = "接续任务再次确认既有信息"
    return seal(case)


def test_changed_audit_still_requires_valid_original_citations_and_integrity():
    original = reviewed_case()
    bad = changed_review(original)
    bad["compiled"]["question_review"]["resolutions"][0]["citations"][0][
        "quote"
    ] = "没有依据的确认"
    seal(bad)
    with pytest.raises(ValidationError):
        compiler.execution_fingerprint(bad)
    bad = changed_review(original)
    bad["compiled"]["question_review"]["sources"][0]["text"] += "unsealed change"
    with pytest.raises(ValidationError, match="冻结的测试准备结果校验失败"):
        compiler.execution_fingerprint(bad)


async def test_preparation_receipt_recovery_and_tuning_scope_keep_every_stored_case(
    db_session, wb
):
    from api.services.agent_workbench.recovery import restore_prepared_cases
    from api.services.agent_workbench.tuning import resolve_scope

    org, _, sid = wb
    request = {
        "text": "Prepare all imported customer scenarios",
        "mode": "test",
        "prepare_only": True,
        "test_mode": "dynamic",
    }
    queued = await db_session.wb_enqueue(org, sid, "build", request, "prepare")
    job = await db_session.wb_claim(org, sid, queued["id"])
    candidate = await db_session.wb_test_target(org, sid, job["id"], job["lease_token"])
    cases = [reviewed_case(str(i), large=True) for i in range(23)]
    suites = [
        await db_session.wb_save_suite(
            org, sid, batch, job_id=job["id"], token=job["lease_token"]
        )
        for batch in tasks.pack_suites(cases, "test", request["text"])
    ]
    summary = compiler.preparation_summary(cases)
    output = {
        "intent": "test",
        "outcome": "test_preparation",
        "candidate_id": candidate["id"],
        "suite_ids": [s["id"] for s in suites],
        "compilation": summary,
        "preparation_receipts": [
            {"name": c["name"], "suite_id": s["id"], "status": "ready"}
            for s in suites
            for c in s["payload"]["cases"]
        ],
    }
    await db_session.wb_finish(
        org, sid, job["id"], job["lease_token"], "completed", output=output
    )
    prepared = await tasks.prepared_test_input(
        org,
        sid,
        "build",
        {
            "mode": "test",
            "text": "Run prepared tests",
            "prepared_test_job_id": job["id"],
        },
    )
    assert prepared["test_suite_ids"] == output["suite_ids"]
    restored = deepcopy(cases)
    for c in restored:
        c["compiled"] = None
    assert await restore_prepared_cases(org, sid, job["id"], restored) == {
        c["name"] for c in cases
    }
    assert restored == cases
    # Resolving a tuning scope is read-only; no model or candidate edit is run.
    scope, loaded, _ = await resolve_scope(
        org, sid, {"suite_ids": prepared["test_suite_ids"]}
    )
    assert scope["case_count"] == len(cases)
    assert [c for s in loaded for c in s["payload"]["cases"]] == cases
    assert scope["fingerprint"] == fingerprint(
        [{"id": s["id"], "payload": s["payload"]} for s in suites]
    )


@pytest.mark.parametrize(
    "field",
    [
        "scenario",
        "requirements",
        "sources",
        "public_context",
        "environment",
        "resolution",
        "user_messages",
        "initial_business_state",
        "max_turns",
    ],
)
def test_execution_identity_ignores_only_valid_audit_changes(field):
    original = reviewed_case()
    changed = changed_review(original)
    assert changed["compiled"]["fingerprint"] != original["compiled"]["fingerprint"]
    assert compiler.execution_fingerprint(changed) == compiler.execution_fingerprint(
        original
    )
    compiled = changed["compiled"]
    if field == "scenario":
        compiled["scenario"]["behavior"] += "，客户会明确拒绝。"
    elif field == "requirements":
        item = compiled["requirements"][0]
        item["severity"] = "minor"
        item["id"] = (
            "req_" + fingerprint({k: v for k, v in item.items() if k != "id"})[:20]
        )
    elif field == "sources":
        compiled["sources"][0]["text"] += "新增业务规则。"
    elif field == "public_context":
        compiled["public_context"]["recipient"] = "someone else"
    elif field == "environment":
        compiled["version"] = 2
        environment = {"capabilities": [], "sources": []}
        compiled["environment"] = {
            **environment,
            "fingerprint": fingerprint(environment),
        }
    elif field == "resolution":
        compiled["question_review"]["resolutions"][0]["status"] = "unresolved"
    elif field == "user_messages":
        changed["user_messages"][0] += "，确认其他问题"
    elif field == "max_turns":
        changed["max_turns"] += 1
    else:
        changed["initial_business_state"] = {"confirmed": True}
    seal(changed)
    assert compiler.execution_fingerprint(changed) != compiler.execution_fingerprint(
        original
    )


async def test_resume_chain_prefers_completed_dialogue_over_newer_interruption(
    monkeypatch,
):
    original = reviewed_case()
    changed = changed_review(original)
    name = original["name"]
    result = {
        "name": name,
        "status": "passed",
        "run_id": 17,
        "transcript": [{"role": "assistant", "text": "费用已说明。"}],
        "reference_review": {
            "version": 4,
            "checks": [{"status": "passed"}],
            "rubric_fingerprint": fingerprint(original["compiled"]["requirements"]),
            "compilation_fingerprint": original["compiled"]["fingerprint"],
        },
    }
    output = {
        "test_target_fingerprint": "target",
        "evaluation_model_configuration_fingerprint": "model",
    }
    rows = {}
    for job_id, case, results in [
        ("old", original, [result]),
        (
            "new",
            changed,
            [
                {
                    **deepcopy(result),
                    "status": "error",
                    "run_id": 18,
                    "validity": {
                        "scenario": deepcopy(original["compiled"]["scenario"])
                    },
                }
            ],
        ),
    ]:
        rows["jobs", job_id] = {
            "id": job_id,
            "input": {
                "text": "request",
                **({"resume_test_job_id": "old"} if job_id == "new" else {}),
            },
            "output": {**output, "evaluation_ids": ["e-" + job_id]},
        }
        rows["evaluations", "e-" + job_id] = {
            "id": "e-" + job_id,
            "job_id": job_id,
            "candidate_id": "candidate",
            "suite_id": "s-" + job_id,
            "summary": {"evaluation_version": EVALUATION_VERSION},
            "results": results,
        }
        rows["suites", "s-" + job_id] = {"payload": {"cases": [case]}}
    before = deepcopy(rows)

    async def get(org, sid, table, key):
        assert (org, sid) == (1, "session")
        return rows[table, key]

    monkeypatch.setattr(tasks.db_client, "wb_get", get)
    args = (
        1,
        "session",
        "new",
        {"id": "candidate", "fingerprint": "target"},
        [changed],
        "request",
        "model",
    )
    reused = await tasks.reusable_test_results(*args)
    assert reused[name]["run_id"] == 17
    assert reused[name]["transcript"] == result["transcript"]
    assert not reused[name]["review_pending"]
    review = reused[name]["reference_review"]
    assert review["compilation_fingerprint"] == original["compiled"]["fingerprint"]
    assert same_review_contract(
        review,
        {**review, "compilation_fingerprint": changed["compiled"]["fingerprint"]},
    )
    assert not same_review_contract(
        review, {**review, "execution_fingerprint": "changed contract"}
    )
    assert rows == before
    # If the original review was interrupted, retain its dialogue and review only.
    rows["evaluations", "e-old"]["results"][0]["reference_review"]["checks"] = []
    reused = await tasks.reusable_test_results(*args)
    assert reused[name]["run_id"] == 17 and reused[name]["review_pending"]
    # With no completed dialogue, the newest saved scenario is still available.
    rows["evaluations", "e-old"]["results"] = []
    reused = await tasks.reusable_test_results(*args)
    assert reused[name]["run_id"] == 18 and reused[name]["replay_pending"]
    assert reused[name]["validity"]["scenario"] == original["compiled"]["scenario"]
    # Changed execution gates cannot reuse either kind of receipt.
    changed["compiled"]["question_review"]["resolutions"][0]["status"] = "unresolved"
    seal(changed)
    rows["suites", "s-new"]["payload"]["cases"] = [
        before["suites", "s-new"]["payload"]["cases"][0]
    ]
    assert await tasks.reusable_test_results(*args) == {}
