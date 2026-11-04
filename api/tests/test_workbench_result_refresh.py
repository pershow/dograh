"""Compact polling must identify changes to full evidence, not just verdicts."""

from copy import deepcopy

from api.db.agent_workbench_client import evaluation_index, record
from api.db.models import WorkbenchEvaluationModel


def test_evidence_revision_changes_when_detail_changes_without_summary_change():
    row = WorkbenchEvaluationModel(
        status="running",
        summary={"passed": 1},
        results=[
            {"name": "case", "status": "passed", "transcript": [], "assertions": []}
        ],
    )
    before = evaluation_index(row)
    assert before["detail_revision"] == record(row)["detail_revision"]
    row.results = deepcopy(row.results)
    row.results[0]["transcript"] = [{"role": "assistant", "text": "Saved reply"}]
    after = evaluation_index(row)
    assert after["summary"] == before["summary"]
    assert after["results"] == before["results"]  # same compact content
    assert after["detail_revision"] != before["detail_revision"]
    assert after["detail_revision"] == record(row)["detail_revision"]
    assert evaluation_index(row)["detail_revision"] == after["detail_revision"]
    row.results[0]["assertions"] = [{"passed": True, "reason": "Verified evidence"}]
    reviewed = evaluation_index(row)
    assert reviewed["detail_revision"] != after["detail_revision"]
    row.status = "completed"
    assert evaluation_index(row)["detail_revision"] != reviewed["detail_revision"]
