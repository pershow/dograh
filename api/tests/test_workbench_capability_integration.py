from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import tuning
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint


async def test_task_wide_tuning_rejects_any_held_out_batch(monkeypatch):
    async def get(org, sid, table, identity):
        return {
            "id": identity,
            "payload": {
                "usage": "acceptance" if identity == "heldout" else "development",
                "cases": [{"name": identity}],
            },
        }

    monkeypatch.setattr(tuning.db_client, "wb_get", get)
    with pytest.raises(WorkbenchError, match="验收"):
        await tuning.resolve_scope(
            1, "session", {"candidate_id": "c", "suite_ids": ["dev", "heldout"]}
        )


async def test_tuning_rechecks_resource_manifest_before_reusing_fixture_results(
    monkeypatch,
):
    suite = {
        "id": "suite",
        "fingerprint": "suite-hash",
        "payload": {"cases": [{"name": "case", "fixtures": [{"tool_uuid": "tool"}]}]},
    }
    prior = {
        "id": "evaluation",
        "suite_id": "suite",
        "candidate_id": "c",
        "status": "completed",
        "summary": {
            "evaluation_version": tuning.EVALUATION_VERSION,
            "model_configuration_fingerprint": fingerprint({}),
            "suite_fingerprint": "suite-hash",
        },
        "results": [
            {"name": "case", "status": "passed", "resource_versions": {"tool": "old"}}
        ],
    }
    monkeypatch.setattr(tuning.db_client, "wb_get", AsyncMock(return_value=prior))
    run = AsyncMock(return_value=prior)
    monkeypatch.setattr(tuning, "evaluate", run)
    await tuning.evaluate_scope(
        1,
        "session",
        {},
        "c",
        [suite],
        SimpleNamespace(effective=None, snapshot={}),
        source={"output": {"candidate_id": "c", "evaluation_ids": ["evaluation"]}},
    )
    assert run.await_count == 1
    assert run.await_args.kwargs["prior_results"]["case"]["resource_versions"] == {
        "tool": "old"
    }
