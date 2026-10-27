"""Coverage must be grounded and must gate completion independently of test counts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest

from api.services.agent_workbench import jobs
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.requirements import (
    RequirementReviewer,
    preserves_test_coverage,
)


@pytest.fixture(autouse=True)
def checkpoint_storage(monkeypatch):
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())


def evidence():
    return {
        "requests": {
            "current": "Improve the entire agent, not only the example.",
            "prior": {
                "messages": [{"role": "assistant", "text": "Invented requirement"}]
            },
        },
        "candidate": {"workflow_json": {"nodes": [{"data": {"name": "Stage"}}]}},
        "suite": {"cases": [{"name": "Scenario"}]},
    }


def item(status="covered"):
    return {
        "requirement": "Whole-agent improvement",
        "source_quote": "entire agent",
        "scope": "agent",
        "status": status,
        "nodes": ["Stage"],
        "cases": ["Scenario"],
        "evidence": "Observed evidence",
        "correction": "Cover all applicable contexts" if status != "covered" else "",
    }


async def test_review_rejects_invented_sources_and_missing_evidence():
    reviewer = RequirementReviewer(SimpleNamespace(), evidence())
    for invalid in [
        {**item(), "source_quote": "Invented requirement"},
        {**item(), "nodes": ["Missing"]},
        {**item(), "cases": []},
        {**item("gap"), "correction": ""},
    ]:
        with pytest.raises((WorkbenchError, jsonschema.ValidationError)):
            await reviewer.call("submit_review", {"requirements": [invalid]})
    assert (await reviewer.call("submit_review", {"requirements": [item("gap")]}))[
        "status"
    ] == "needs_attention"
    with pytest.raises(WorkbenchError, match="read-only"):
        await reviewer.call("save_candidate", {})


async def test_review_cannot_drop_or_narrow_requirements_between_revisions():
    source = evidence()
    source["required_requirements"] = [item()]
    reviewer = RequirementReviewer(SimpleNamespace(), source)
    for changed in [
        {**item(), "scope": "local"},
        {**item(), "requirement": "Smaller scope"},
    ]:
        with pytest.raises(WorkbenchError, match="every original requirement"):
            await reviewer.call("submit_review", {"requirements": [changed]})
    assert (await reviewer.call("submit_review", {"requirements": [item()]}))[
        "status"
    ] == "passed"


def test_coverage_extensions_preserve_existing_scenarios_and_assertions():
    original = {
        "name": "Acceptance",
        "cases": [
            {
                "name": "Existing",
                "user_messages": ["hello"],
                "assertions": [{"kind": "contains", "value": "required"}],
            }
        ],
    }
    extended = {
        "name": "Extended",
        "cases": [
            {
                **original["cases"][0],
                "assertions": [
                    *original["cases"][0]["assertions"],
                    {"kind": "contains", "value": "new"},
                ],
            },
            {"name": "Additional", "assertions": []},
        ],
    }
    assert preserves_test_coverage(original, extended)
    for weakened in [
        {"cases": []},
        {"cases": [{**original["cases"][0], "assertions": []}]},
        {"cases": [{**original["cases"][0], "user_messages": ["different path"]}]},
    ]:
        assert not preserves_test_coverage(original, weakened)


async def test_scope_gap_triggers_local_revision_even_if_small_suite_would_pass(
    monkeypatch,
):
    review = AsyncMock(
        side_effect=[
            {"status": "needs_attention", "requirements": [item("gap")]},
            {"status": "passed", "requirements": [item()]},
            {"status": "passed", "requirements": [item()]},
        ]
    )
    monkeypatch.setattr(jobs, "review_requirements", review)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    revised = {"outcome": "build", "candidate_id": "revised", "suite_id": "expanded"}
    build = AsyncMock(return_value=revised)
    monkeypatch.setattr(jobs, "_build", build)
    monkeypatch.setattr(
        jobs.db_client,
        "wb_get",
        AsyncMock(
            return_value={"snapshot": {"name": "base"}, "payload": {"cases": []}}
        ),
    )
    verify = AsyncMock(
        return_value={
            **revised,
            "verification": {"status": "passed", "confirmed": True},
        }
    )
    monkeypatch.setattr(jobs, "_verify_build", verify)
    result = await jobs._complete_build(
        1,
        "s",
        {"id": "job", "lease_token": "lease", "input": {"text": "request"}},
        {"target": {}},
        None,
        {"candidate_id": "partial", "suite_id": "small"},
    )
    assert result["candidate_id"] == "revised"
    assert result["requirement_review"]["status"] == "passed"
    assert build.await_count == verify.await_count == 1
    assert build.call_args.args[3]["target"] == {"name": "base"}
    assert review.call_args.kwargs == {
        "observed": True,
        "required_requirements": [
            {k: item()[k] for k in ("requirement", "source_quote", "scope")}
        ],
    }


async def test_missing_information_never_manufactures_a_pass_or_business_defaults(
    monkeypatch,
):
    monkeypatch.setattr(
        jobs,
        "review_requirements",
        AsyncMock(
            return_value={
                "status": "needs_attention",
                "requirements": [
                    {
                        **item("needs_input"),
                        "input_request": {
                            "kind": "user_decision",
                            "question": "Which approved policy applies?",
                            "reason": "Two contradictory user policies",
                            "checked_sources": ["provided_context"],
                        },
                    }
                ],
            }
        ),
    )
    build = AsyncMock()
    monkeypatch.setattr(jobs, "_build", build)
    result = await jobs._complete_build(
        1, "s", {"id": "job", "lease_token": "lease"}, {}, None, {"candidate_id": "c", "suite_id": "s"}
    )
    assert result["verification"]["status"] == "inconclusive"
    assert result["verification"]["confirmed"] is False
    build.assert_not_awaited()
