"""Original-source authority, immutable task contracts and read-only re-review."""
# ruff: noqa: F811 -- imported pytest fixtures

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import basis, review, test_tasks
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.repair import failure_diagnostics
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401
from api.tests.test_workbench_review import check, evidence, verified


async def test_generated_policy_cannot_establish_business_failure():
    e = evidence()
    c = check()
    policy = next(r for r in e["records"] if r["source"] == "policy")
    c["citations"][1] = {"id": policy["id"], "quote": "收费方案每月20元。"}
    with pytest.raises(WorkbenchError, match="原始用户要求"):
        review.finalize({"checks": [c]}, verified([c]), e)
    c.update(category="configuration_issue", layer="runtime", severity="unknown")
    result = review.finalize({"checks": [c]}, verified([c]), e)
    assert result["status"] == "inconclusive"
    assert result["checks"][0]["repair_target"] == "investigate_runtime"


async def test_quality_suggestions_and_minor_defects_do_not_become_blocking():
    e = evidence()
    c = check("quality_suggestion")
    c.update(severity="suggestion", layer="experience")
    result = review.finalize({"checks": [c]}, verified([c]), e)
    assert result["status"] == "inconclusive"
    assert result["checks"][0]["repair_target"] == "none"
    observed = check("satisfied")
    result = review.finalize({"checks": [c, observed]}, verified([c, observed]), e)
    assert result["status"] == "passed"
    c.update(category="agent_issue", severity="minor", layer="runtime")
    result = review.finalize({"checks": [c]}, verified([c]), e)
    assert result["status"] == "passed"
    assert result["checks"][0]["status"] == "inconclusive"


async def test_missing_originals_never_become_a_business_pass():
    e = evidence()
    e["records"] = [r for r in e["records"] if r["source"] != "basis"]
    c = {
        "criterion": "有回复",
        "category": "satisfied",
        "reason": "观察到回复",
        "citations": [{"id": "actual:0", "quote": "请先选择文件类型。"}],
    }
    result = review.finalize({"checks": [c]}, verified([c]), e)
    assert result["status"] == "inconclusive" and result["warnings"]


async def test_explicit_current_business_text_can_be_authority_but_task_commands_cannot():
    e = evidence()
    for record in e["records"]:
        if record["source"] == "basis":
            record["source_kind"] = "current_request"
    c = check("satisfied")
    assert review.finalize({"checks": [c]}, verified([c]), e)["status"] == "passed"
    c["requirement_type"] = "none"
    result = review.finalize({"checks": [c]}, verified([c]), e)
    assert result["status"] == "inconclusive" and result["warnings"]


async def test_conflicting_originals_require_two_quotes_and_never_authorize_agent_repair():
    e = evidence()
    c = check("source_conflict")
    c["citations"] = [c["citations"][1]]
    with pytest.raises(WorkbenchError, match="两条原始依据"):
        review.finalize({"checks": [c]}, verified([c]), e)
    original = next(r for r in e["records"] if r["source"] == "basis")
    e["records"].append({**original, "id": "second", "text": "所有方案免费。"})
    c["citations"].append({"id": "second", "quote": "所有方案免费。"})
    result = review.finalize({"checks": [c]}, verified([c]), e)
    assert result["status"] == "inconclusive"
    assert result["checks"][0]["repair_target"] == "source"


async def test_exact_source_quotes_and_full_large_source_read_are_required():
    e = evidence()
    c = check()
    c["citations"][1]["quote"] = "所有方案免费"
    with pytest.raises(WorkbenchError, match="连续片段"):
        review.finalize({"checks": [c]}, verified([c]), e)
    for i in range(4):
        e["records"].append(
            {"id": f"large:{i}", "source": "basis", "text": "原文" * 6000}
        )
    reader = review.ReviewSourceReader(e)
    with pytest.raises(WorkbenchError, match="未读取"):
        reader.require_read()
    for key in list(reader.pending):
        assert (await reader.call("read_review_source", {"id": key}))[
            "text"
        ] == "原文" * 6000
    reader.require_read()
    with pytest.raises(WorkbenchError):
        await reader.call("save_candidate", {"id": "large:0"})


async def test_uncertainty_and_optional_advice_never_enter_agent_repair():
    result = {
        "results": [
            {
                "name": "case",
                "status": "failed",
                "transcript": [],
                "assertions": [
                    {
                        "kind": "reference",
                        "passed": False,
                        "informational": True,
                        "repair_target": "none",
                    },
                    {"kind": "reference", "passed": False, "repair_target": "source"},
                    {"kind": "reference", "passed": False},
                ],
            }
        ]
    }
    assert failure_diagnostics(result) == []
    result["results"][0]["assertions"].append(
        {"kind": "reference", "passed": False, "repair_target": "agent"}
    )
    assert len(failure_diagnostics(result)[0]["failures"]) == 1


async def test_basis_is_private_immutable_and_scoped_across_sessions(
    db_session, wb, monkeypatch
):
    org, user, sid = wb
    monkeypatch.setattr(basis, "db_client", db_session)
    ctx = await db_session.wb_context(org, sid)
    original = await db_session.wb_enqueue(
        org, sid, "build", {"text": "任何流程都要先确认授权。"}, "source"
    )
    original = await db_session.wb_claim(org, sid, original["id"])
    target = {**ctx["target"], "name": "Source-backed"}
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        original["id"],
        original["lease_token"],
        target,
        "code",
        "summary",
        ctx["target_fingerprint"],
    )
    await db_session.wb_finish(
        org, sid, original["id"], original["lease_token"], "completed"
    )
    await db_session.wb_emit(
        org, sid, "assistant.message", {"text": "可以跳过授权，这是用户确认的。"}
    )
    new = await db_session.wb_create_session(
        org, user, workflow_id=ctx["session"]["workflow_id"], new_session=True
    )
    job = await db_session.wb_enqueue(
        org, new["id"], "build", {"text": "只测试，不修改。", "mode": "test"}, "test"
    )
    job = await db_session.wb_claim(org, new["id"], job["id"])
    frozen = await basis.ensure_basis(org, new["id"], job, target)
    assert any(s["text"] == original["input"]["text"] for s in frozen["sources"])
    assert "可以跳过授权" not in str(frozen)
    assert frozen["target_fingerprint"] == candidate["fingerprint"]
    public = await db_session.wb_get(org, new["id"], "jobs", job["id"])
    assert "_task_basis" not in public["output"]
    assert "text" not in public["output"]["task_basis"]["sources"][0]
    assert await basis.ensure_basis(org, new["id"], job, {"changed": True}) == frozen
    changed = {**frozen, "fingerprint": "different"}
    with pytest.raises(WorkbenchError, match="已冻结"):
        await db_session.wb_freeze_basis(
            org, new["id"], job["id"], job["lease_token"], changed, {}
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_task_basis(org + 999, new["id"], job["id"])
    other = await db_session.wb_create_session(org, user, name="Another Agent")
    other_job = await db_session.wb_enqueue(
        org, other["id"], "build", {"text": "test"}, "other"
    )
    assert (
        await db_session.wb_basis_origins(
            org, other["id"], other_job["id"], candidate["fingerprint"]
        )
        == []
    )


async def test_model_freezing_is_idempotent_but_changes_are_rejected(db_session, wb):
    org, _, sid = wb
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "freeze")
    job = await db_session.wb_claim(org, sid, job["id"])
    for evaluation in (False, True):
        snapshot = {"services": {"llm": {"model": "fixed"}}}
        first = await db_session.wb_pin_models(
            org, sid, job["id"], job["lease_token"], snapshot, evaluation=evaluation
        )
        assert (
            await db_session.wb_pin_models(
                org, sid, job["id"], job["lease_token"], snapshot, evaluation=evaluation
            )
            == first
        )
        with pytest.raises(WorkbenchError):
            await db_session.wb_pin_models(
                org,
                sid,
                job["id"],
                job["lease_token"],
                {"changed": True},
                evaluation=evaluation,
            )


@pytest.mark.parametrize("resumed", [False, True])
async def test_rereview_preserves_actual_dialogue_and_never_calls_runtime(
    monkeypatch, resumed
):
    from api.services.agent_workbench import evaluation
    from api.services.workflow import text_chat_runner

    case = {
        "name": "A",
        "user_messages": ["你好"],
        "assertions": [],
        "reference_dialogue": "客户：你好\n坐席：您好",
    }
    old = {
        "id": "old",
        "suite_id": "s",
        "candidate_id": "c",
        "status": "completed",
        "summary": {
            "model": "actual-runtime",
            "model_configuration_fingerprint": "runtime-fp",
        },
        "results": [
            {
                "name": "A",
                "status": "failed",
                "run_id": 42,
                "transcript": [{"role": "assistant", "text": "您好", "turn": 0}],
                "events": [],
                "assertions": [],
                "reference_review": {"version": 2, "reason": "old"},
            }
        ],
    }
    if resumed:
        old["results"].append(
            {**deepcopy(old["results"][0]), "name": "B", "run_id": 43}
        )
    original = deepcopy(old)
    candidate = {
        "id": "c",
        "fingerprint": "c",
        "snapshot": {"workflow_json": {"nodes": [], "edges": []}},
    }
    suite = {
        "id": "s",
        "fingerprint": "s",
        "payload": {"name": "suite", "instructions": "OLD VERSION", "cases": [case]},
    }
    if resumed:
        suite["payload"]["cases"].append({**case, "name": "B"})
    cached = {
        **deepcopy(old["results"][0]),
        "original_evaluation_id": "old",
        "reference_review": {
            "version": 3,
            "status": "failed",
            "checks": [{"criterion": "completed"}],
            "basis_fingerprint": "basis",
        },
    }
    previous = {
        "id": "previous",
        "input": {"text": "CURRENT REVIEW", "review_evaluation_ids": ["old"]},
        "output": {
            "model_configuration_fingerprint": "reviewer-fp",
            "evaluation_ids": ["partial"],
        },
    }

    async def get(org, sid, table, key):
        if key == "previous":
            return deepcopy(previous)
        if key == "partial":
            return {"job_id": "previous", "results": [deepcopy(cached)]}
        return deepcopy(
            candidate if table == "candidates" else suite if table == "suites" else old
        )

    monkeypatch.setattr(test_tasks.db_client, "wb_get", get)
    monkeypatch.setattr(
        test_tasks.db_client,
        "wb_context",
        AsyncMock(return_value={"session": {"workflow_id": 1}}),
    )
    monkeypatch.setattr(
        test_tasks.db_client, "wb_evaluation", AsyncMock(return_value={"id": "new"})
    )
    for name in ("wb_emit", "wb_evaluation_result", "wb_job_progress"):
        monkeypatch.setattr(test_tasks.db_client, name, AsyncMock())
    frozen = {
        "fingerprint": "basis",
        "memory_revision": "m",
        "target_fingerprint": "c",
        "sources": [],
        "warnings": [],
    }
    monkeypatch.setattr(
        test_tasks.db_client, "wb_task_basis", AsyncMock(return_value=frozen)
    )
    monkeypatch.setattr(test_tasks.db_client, "wb_freeze_basis", AsyncMock())
    monkeypatch.setattr(basis, "ensure_basis", AsyncMock(return_value=frozen))
    judge = AsyncMock(
        return_value={"version": 3, "status": "inconclusive", "checks": []}
    )
    monkeypatch.setattr(test_tasks, "judge_reference", judge)
    replay = AsyncMock(side_effect=AssertionError("Must not execute runtime"))
    save = AsyncMock(side_effect=AssertionError("Must not edit Agent"))
    monkeypatch.setattr(text_chat_runner, "execute_text_chat_pending_turn", replay)
    monkeypatch.setattr(test_tasks.db_client, "wb_save_candidate", save)
    job = {
        "id": "j",
        "lease_token": "l",
        "output": {"model_configuration_fingerprint": "reviewer-fp"},
        "input": {
            "candidate_id": "c",
            "text": "CURRENT REVIEW",
            "review_evaluation_ids": ["old"],
        },
    }
    if resumed:
        job["input"]["resume_review_job_id"] = "previous"
    result = await test_tasks.run_review_task(
        1,
        "sid",
        job,
        SimpleNamespace(llm=SimpleNamespace(model="review-model")),
        "chat",
        AsyncMock(),
    )
    assert result["runtime_replayed"] is result["agent_modified"] is False
    assert result["summary"]["reused"] == (2 if resumed else 1)
    assert result["summary"]["model_configuration_fingerprint"] == "runtime-fp"
    assert (
        result["summary"]["reviewer_model_configuration_fingerprint"] == "reviewer-fp"
    )
    judge.assert_awaited_once()
    assert judge.call_args.kwargs["instructions"] == "CURRENT REVIEW"
    assert judge.call_args.kwargs["basis"] == frozen
    saved = evaluation.db_client.wb_evaluation_result.call_args.args[5]
    assert (
        saved[0]["run_id"] == 42
        and saved[0]["transcript"] == old["results"][0]["transcript"]
    )
    assert saved[-1]["previous_reference_reviews"][0]["review"]["version"] == 2
    if resumed:
        assert saved[0]["reference_review"] == cached["reference_review"]
        test_tasks.db_client.wb_freeze_basis.assert_awaited_once()
    assert old == original
    replay.assert_not_awaited()
    save.assert_not_awaited()


async def test_review_resume_command_preserves_read_only_contract(monkeypatch):
    from api.services.agent_workbench import jobs
    from api.tasks import arq

    previous = {
        "id": "previous",
        "status": "cancelled",
        "input": {
            "mode": "test",
            "text": "只复核原始资料",
            "review_evaluation_ids": ["original"],
        },
        "output": {"outcome": "review", "evaluation_ids": ["partial"]},
    }

    async def get(org, sid, table, key):
        assert (org, sid) == (1, "s")
        if table == "jobs":
            return previous
        if table == "evaluations":
            assert key == "original"
            return {
                "status": "completed",
                "results": [{"name": "one"}],
                "candidate_id": "frozen",
            }
        return {"snapshot": {}}

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(
        jobs.db_client, "wb_context", AsyncMock(return_value={"target": {}})
    )
    monkeypatch.setattr(jobs, "prepare_models", AsyncMock())
    enqueue = AsyncMock(return_value={"id": "next"})
    monkeypatch.setattr(jobs.db_client, "wb_enqueue", enqueue)
    monkeypatch.setattr(
        arq,
        "get_arq_redis",
        AsyncMock(return_value=SimpleNamespace(enqueue_job=AsyncMock())),
    )
    await jobs.enqueue(
        1,
        "s",
        "build",
        {"continue_test_job_id": "previous", "text": "other request", "mode": "build"},
        "key",
    )
    payload = enqueue.call_args.args[3]
    assert payload == {
        **previous["input"],
        "candidate_id": "frozen",
        "resume_review_job_id": "previous",
        "continue_test_job_id": None,
    }
    previous["status"] = "running"
    with pytest.raises(WorkbenchError, match="等待当前复核"):
        await jobs.enqueue(1, "s", "build", {"continue_test_job_id": "previous"}, "key")


def test_multi_batch_review_counts_every_reused_dialogue():
    from api.services.agent_workbench.jobs import combine_evaluations

    combined = combine_evaluations(
        [
            {
                "id": str(i),
                "results": [],
                "summary": {
                    "total": size,
                    "reused": size,
                    "executed": size,
                    "elapsed_seconds": elapsed,
                },
            }
            for i, (size, elapsed) in enumerate(((20, 3.2), (6, 2.5)))
        ]
    )
    assert combined["summary"]["total"] == combined["summary"]["reused"] == 26
    assert combined["summary"]["elapsed_seconds"] == 5.7
