import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import Instruction
from api.services.agent_workbench import jobs
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.recovery import resumable_build


@pytest.mark.parametrize("interrupted_stage", ["design", "verification", "result"])
async def test_resume_skips_completed_build_stages(monkeypatch, interrupted_stage):
    stored = {}

    async def persist(_org, _sid, _jid, _token, output, _progress):
        stored.update(copy.deepcopy(output))

    monkeypatch.setattr(jobs.db_client, "wb_job_progress", persist)
    build = AsyncMock(side_effect=AssertionError("must not regenerate candidate"))
    monkeypatch.setattr(jobs, "_build", build)
    passed = {"status": "passed", "requirements": []}
    failure = WorkbenchError("connection interrupted")
    review = AsyncMock(
        side_effect=[failure]
        if interrupted_stage == "design"
        else [copy.deepcopy(passed), failure]
        if interrupted_stage == "result"
        else [copy.deepcopy(passed)]
    )
    monkeypatch.setattr(jobs, "review_requirements", review)
    built = {"outcome": "build", "candidate_id": "saved", "suite_id": "frozen"}
    verified = {**built, "verification": {"status": "passed", "confirmed": True}}
    verify = (
        AsyncMock(side_effect=failure)
        if interrupted_stage == "verification"
        else AsyncMock(return_value=verified)
    )
    monkeypatch.setattr(jobs, "_verify_build", verify)
    job = {"id": "first", "lease_token": "lease", "input": {"text": "original"}}
    with pytest.raises(WorkbenchError):
        await jobs._complete_build(1, "s", job, {}, None, built)
    assert stored["build_checkpoint"]["stage"] == interrupted_stage
    assert stored["candidate_id"] == "saved"
    prior = copy.deepcopy(stored)
    review = AsyncMock(return_value=copy.deepcopy(passed))
    verify = AsyncMock(return_value=verified)
    monkeypatch.setattr(jobs, "review_requirements", review)
    monkeypatch.setattr(jobs, "_verify_build", verify)
    resumed = {**job, "id": "resumed", "output": copy.deepcopy(stored)}
    result = await jobs._complete_build(1, "s", resumed, {}, None, built)
    assert result["candidate_id"] == "saved" and result["verification"]["confirmed"]
    assert verify.await_count == (0 if interrupted_stage == "result" else 1)
    assert review.await_count == (2 if interrupted_stage == "design" else 1)
    assert resumed["output"]["build_checkpoint"]["stage"] == "complete"
    assert prior["build_checkpoint"]["stage"] == interrupted_stage
    build.assert_not_awaited()


def test_legacy_saved_candidate_can_resume_but_analysis_and_running_jobs_cannot():
    prior = {
        "kind": "build",
        "status": "failed",
        "output": {
            "outcome": "build",
            "candidate_id": "c",
            "suite_id": "s",
            "regression_suite_ids": ["s", "s2"],
        },
    }
    assert resumable_build(prior)["built"]["regression_suite_ids"] == ["s", "s2"]
    for invalid in (
        {**prior, "status": "running"},
        {**prior, "output": {"outcome": "analysis"}},
    ):
        with pytest.raises(WorkbenchError):
            resumable_build(invalid)


def test_resume_command_cannot_replace_candidate_or_mix_test_operations():
    payload = {
        "mode": "build",
        "text": "resume",
        "resume_build_job_id": "j",
        "idempotency_key": "k",
    }
    Instruction(**payload)
    for extra in (
        {"candidate_id": "other"},
        {"mode": "test"},
        {"asset_ids": ["new"]},
        {"rerun_test_job_id": "test"},
    ):
        with pytest.raises(ValueError):
            Instruction(**{**payload, **extra})


async def test_verification_resume_reuses_finished_batch_and_completed_cases(
    monkeypatch,
):
    built = {
        "outcome": "build",
        "candidate_id": "c",
        "suite_id": "s1",
        "regression_suite_ids": ["s2"],
    }
    job = {"id": "j", "lease_token": "l", "input": {"text": "original"}, "output": {}}
    snapshot = {"workflow_json": {"nodes": []}}
    rows = {}
    passed_case = {
        "name": "already finished",
        "status": "passed",
        "assertions": [],
        "transcript": [],
    }

    def result(eid, sid, cases, status="completed"):
        return {
            "id": eid,
            "candidate_id": "c",
            "suite_id": sid,
            "status": status,
            "results": cases,
            "summary": {
                "total": len(cases),
                "passed": sum(c["status"] == "passed" for c in cases),
                "failed": sum(c["status"] == "failed" for c in cases),
                "error": 0,
                "inconclusive": 0,
            },
        }

    async def get(_org, _sid, table, key):
        if table == "candidates":
            return {"snapshot": snapshot}
        if table == "suites":
            return {"payload": {"cases": [1]}}
        return copy.deepcopy(rows[key])

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())

    async def pin(*args, **kwargs):
        return copy.deepcopy(job["output"])

    monkeypatch.setattr(jobs.db_client, "wb_pin_models", pin)
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(snapshot={}, effective=None)),
    )
    build = AsyncMock(side_effect=AssertionError("do not rebuild"))
    monkeypatch.setattr(jobs, "_build", build)

    async def interrupted(
        _org, _sid, _job, cid, sid, _config, *, on_progress, prior_results
    ):
        if sid == "s1":
            rows["first"] = result("first", sid, [passed_case])
            await on_progress("first", {}, 1)
            return rows["first"]
        rows["partial"] = result("partial", sid, [passed_case], "failed")
        await on_progress("partial", {}, 1)
        raise WorkbenchError("interrupted second batch")

    monkeypatch.setattr(jobs, "evaluate", interrupted)
    with pytest.raises(WorkbenchError):
        await jobs._verify_build(1, "s", job, {}, None, built)
    frozen = copy.deepcopy(rows)
    calls = []

    async def resumed(
        _org, _sid, _job, cid, sid, _config, *, on_progress, prior_results
    ):
        calls.append(sid)
        assert prior_results["already finished"]["original_evaluation_id"] == "partial"
        assert prior_results["already finished"]["status"] == "passed"
        rows["rest"] = result(
            "rest",
            sid,
            [passed_case, {"name": "remaining", "status": "failed", "assertions": []}],
        )
        await on_progress("rest", {}, 2)
        return rows["rest"]

    monkeypatch.setattr(jobs, "evaluate", resumed)
    report = await jobs._verify_build(1, "s", job, {}, None, built)
    assert calls == ["s2"]
    assert report["evaluation_ids"] == ["first", "rest"]
    assert rows["partial"] == frozen["partial"] and rows["first"] == frozen["first"]
    build.assert_not_awaited()
