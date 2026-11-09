"""Compact polling must identify changes to full evidence, not just verdicts."""

from copy import deepcopy

from api.db.models import WorkbenchEvaluationModel
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401


async def test_evidence_revision_changes_when_detail_changes_without_summary_change(
    db_session, wb, async_session
):
    org, _user, sid = wb
    context = await db_session.wb_context(org, sid)
    job = await db_session.wb_enqueue(
        org, sid, "build", {"mode": "build", "text": "evidence refresh"}, "refresh"
    )
    job = await db_session.wb_claim(org, sid, job["id"])
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        job["id"],
        job["lease_token"],
        context["target"],
        "code",
        "saved",
        context["target_fingerprint"],
    )
    suite = await db_session.wb_save_suite(
        org,
        sid,
        {
            "name": "frozen",
            "cases": [
                {
                    "name": "case",
                    "user_messages": ["source"],
                    "assertions": [{"kind": "completed", "value": True}],
                }
            ],
        },
    )
    row = WorkbenchEvaluationModel(
        session_id=sid,
        job_id=job["id"],
        candidate_id=candidate["id"],
        suite_id=suite["id"],
        status="running",
        summary={"passed": 1},
        results=[
            {"name": "case", "status": "passed", "transcript": [], "assertions": []}
        ],
    )
    async_session.add(row)
    await async_session.flush()

    async def read_index():
        page = await db_session.wb_test_result_page(org, sid, job["id"])
        full = await db_session.wb_get(org, sid, "evaluations", row.id)
        compact = page["evaluations"][0]
        assert compact["detail_revision"] == full["detail_revision"]
        return compact

    before = await read_index()
    row.results = deepcopy(row.results)
    row.results[0]["transcript"] = [{"role": "assistant", "text": "Saved reply"}]
    await async_session.flush()
    after = await read_index()
    assert after["summary"] == before["summary"]
    assert after["results"] == before["results"]  # same compact content
    assert after["detail_revision"] != before["detail_revision"]
    assert (await read_index())["detail_revision"] == after["detail_revision"]
    row.results = deepcopy(row.results)
    row.results[0]["assertions"] = [{"passed": True, "reason": "Verified evidence"}]
    await async_session.flush()
    reviewed = await read_index()
    assert reviewed["detail_revision"] != after["detail_revision"]
    row.status = "completed"
    await async_session.flush()
    assert (await read_index())["detail_revision"] != reviewed["detail_revision"]
