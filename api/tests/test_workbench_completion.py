"""Completion means grounded coverage plus new runtime results, not a finished turn."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import jobs, requirements
from api.services.agent_workbench import tools as tool_module
from api.services.agent_workbench.errors import WorkbenchError


def test_reviewed_test_setup_can_be_corrected_without_weakening_expectations():
    original = {
        "name": "Regression",
        "cases": [
            {
                "name": "Setup",
                "user_messages": ["done"],
                "assertions": [
                    {"kind": "contains", "value": "required fact", "turn": 1}
                ],
            }
        ],
    }
    revised = deepcopy(original)
    revised["cases"][0]["user_messages"] = ["identify", "agree", "done"]
    revised["cases"][0]["assertions"][0]["turn"] = 3
    assert not requirements.preserves_test_coverage(original, revised)
    repair = [
        {
            "case": "Setup",
            "reason": "Must reach target first",
            "source_quote": "Wait for identification",
        }
    ]
    assert requirements.preserves_test_coverage(original, revised, test_repairs=repair)
    revised["cases"][0]["assertions"][0]["value"] = "easier phrase"
    assert not requirements.preserves_test_coverage(
        original, revised, test_repairs=repair
    )
    revised["cases"][0]["assertions"][0]["value"] = "required fact"
    for suite in (original, revised):
        suite["cases"][0]["source"] = {"asset_id": "uploaded"}
    assert not requirements.preserves_test_coverage(
        original, revised, test_repairs=repair
    )


async def test_design_test_repair_requires_actual_source_quote():
    source = review_evidence()
    source.update(
        {
            "phase": "design",
            "original": {
                "workflow_json": {
                    "nodes": [{"data": {"prompt": "Wait for identification"}}]
                }
            },
        }
    )
    reviewer = requirements.RequirementReviewer(SimpleNamespace(), source)
    args = {
        "requirements": [finding()],
        "test_repairs": [
            {"case": "Case", "reason": "Must reach target", "source_quote": "invented"}
        ],
    }
    with pytest.raises(WorkbenchError, match="exact original"):
        await reviewer.call("submit_review", args)
    args["test_repairs"][0]["source_quote"] = "Wait for identification"
    assert (await reviewer.call("submit_review", args))["test_repairs"]


def finding(status="gap", **extra):
    return {
        "requirement": "Preserve policy",
        "source_quote": "policy",
        "scope": "agent",
        "status": status,
        "nodes": ["Stage"],
        "cases": ["Case"],
        "evidence": "Original source",
        "correction": "Read saved evidence",
        **extra,
    }


def review_evidence():
    return {
        "requests": {"current": "Preserve policy", "prior": {}},
        "candidate": {"workflow_json": {"nodes": [{"data": {"name": "Stage"}}]}},
        "suite": {"cases": [{"name": "Case"}]},
    }


async def test_missing_system_evidence_is_repairable_not_a_user_question():
    reviewer = requirements.RequirementReviewer(SimpleNamespace(), review_evidence())
    result = await reviewer.call(
        "submit_review", {"requirements": [finding("needs_input")]}
    )
    assert result["requirements"][0]["status"] == "gap"


async def test_reviewer_can_read_scoped_results_but_cannot_mutate_or_claim_unread_sources():
    reader = SimpleNamespace(
        specs=lambda: [
            tool_module.tool(
                "read_evaluation",
                "read",
                {"evaluation_id": {"type": "string"}},
                ["evaluation_id"],
            ),
            tool_module.tool("save_candidate", "write"),
        ],
        read_evaluation=AsyncMock(
            return_value={"actual_run_id": 42, "text": "Actual reply"}
        ),
    )
    reviewer = requirements.RequirementReviewer(reader, review_evidence())
    assert all("type" not in spec for spec in reviewer.specs())
    question = {
        "kind": "user_decision",
        "question": "Which conflicting policy applies?",
        "reason": "Two confirmed policies conflict",
        "checked_sources": ["read_evaluation"],
    }
    with pytest.raises(WorkbenchError, match="actually used"):
        await reviewer.call(
            "submit_review",
            {"requirements": [finding("needs_input", input_request=question)]},
        )
    assert (await reviewer.call("read_evaluation", {"evaluation_id": "batch"}))[
        "actual_run_id"
    ] == 42
    result = await reviewer.call(
        "submit_review",
        {"requirements": [finding("needs_input", input_request=question)]},
    )
    assert result["requirements"][0]["input_request"] == question
    with pytest.raises(WorkbenchError, match="read-only"):
        await reviewer.call("save_candidate", {})


async def test_one_real_question_does_not_discard_independent_repairs(monkeypatch):
    question = {
        "kind": "user_decision",
        "question": "Which policy?",
        "reason": "Conflicting user sources",
        "checked_sources": ["provided_context"],
    }
    blocker = finding("needs_input", input_request=question)
    gap = finding("gap", requirement="Independent fix")
    monkeypatch.setattr(
        jobs,
        "review_requirements",
        AsyncMock(
            side_effect=[
                {"status": "needs_attention", "requirements": [blocker, gap]},
                {
                    "status": "needs_attention",
                    "requirements": [blocker, {**gap, "status": "covered"}],
                },
            ]
        ),
    )
    build = AsyncMock(
        return_value={
            "outcome": "build",
            "candidate_id": "fixed",
            "suite_id": "expanded",
        }
    )
    monkeypatch.setattr(jobs, "_build", build)
    monkeypatch.setattr(
        jobs.db_client,
        "wb_get",
        AsyncMock(return_value={"snapshot": {}, "payload": {"cases": []}}),
    )
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())
    result = await jobs._complete_build(
        1,
        "s",
        {"id": "j", "lease_token": "l"},
        {"target": {}},
        None,
        {"candidate_id": "base", "suite_id": "old", "regression_suite_ids": ["frozen"]},
    )
    assert build.await_count == 1
    assert "independent gaps" in build.call_args.args[5]
    assert result["candidate_id"] == "fixed"
    assert result["regression_suite_ids"] == ["frozen"]
    assert result["pending_questions"] == [question]
    assert jobs.task_result_status("build", result) == "awaiting_input"


@pytest.mark.parametrize(
    "output,status",
    [
        ({"outcome": "build", "candidate_id": "saved"}, "needs_attention"),
        (
            {"verification": {"status": "passed", "passed": 7, "total": 7}},
            "needs_attention",
        ),
        (
            {
                "verification": {"status": "passed", "confirmed": True},
                "requirement_review": {"status": "needs_attention"},
            },
            "needs_attention",
        ),
        (
            {
                "verification": {"status": "passed", "confirmed": True},
                "requirement_review": {"status": "passed"},
            },
            "completed",
        ),
        ({"outcome": "test", "summary": {"failed": 2}}, "completed"),
        ({"outcome": "needs_input"}, "awaiting_input"),
    ],
)
def test_process_completion_cannot_masquerade_as_tuning_success(output, status):
    assert jobs.task_result_status("build", output) == status


async def test_review_failure_keeps_saved_candidate_and_suite_checkpoint(monkeypatch):
    from api.services.agent_workbench import test_tasks

    built = {
        "outcome": "build",
        "candidate_id": "saved-candidate",
        "suite_id": "saved-suite",
        "regression_suite_ids": ["frozen-batch"],
    }
    monkeypatch.setattr(
        jobs.db_client,
        "wb_context",
        AsyncMock(
            return_value={"target": {}, "session": {"model_protocol": "responses"}}
        ),
    )
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    progress = AsyncMock()
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", progress)
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(
            return_value=SimpleNamespace(
                effective=SimpleNamespace(
                    llm=SimpleNamespace(api_key="test", provider="openai")
                ),
                snapshot={},
            )
        ),
    )
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    monkeypatch.setattr(test_tasks, "test_intent", AsyncMock(return_value=False))
    monkeypatch.setattr(jobs, "_build", AsyncMock(return_value=built))

    async def interrupted_review(*args):
        progress.assert_awaited_once()
        raise WorkbenchError("Review connection interrupted")

    monkeypatch.setattr(jobs, "_complete_build", interrupted_review)
    with pytest.raises(WorkbenchError, match="interrupted"):
        await jobs._perform(
            1,
            "session",
            {
                "id": "job",
                "kind": "build",
                "lease_token": "lease",
                "input": {"text": "Resume tuning"},
            },
        )
    assert progress.call_args.args[4] == built


async def test_reuse_attachment_batches_retains_all_cases_without_reusing_runs(
    monkeypatch,
):
    data = {
        ("jobs", "source"): {
            "output": {
                "suite_ids": ["a", "b"],
                "suite_id": "b",
                "evaluation_ids": ["old-a", "old-b"],
            }
        },
        ("suites", "a"): {"fingerprint": "a-fp", "payload": {"cases": [1, 2]}},
        ("suites", "b"): {"fingerprint": "b-fp", "payload": {"cases": [3]}},
    }

    async def get(org, sid, table, identity):
        assert (org, sid) == (7, "session")
        return deepcopy(data[table, identity])

    monkeypatch.setattr(tool_module.db_client, "wb_get", get)
    t = tool_module.WorkbenchTools(
        7,
        "session",
        {"input": {"mode": "build"}},
        {"target": {}, "target_fingerprint": "draft"},
    )
    receipt = await t.save_suite(source_job_id="source")
    assert t.regression_suite_ids == ["a", "b"]
    assert receipt["total"] == 3
    assert "old-a" not in str(receipt)
    with pytest.raises(WorkbenchError, match="exactly one"):
        await t.save_suite(suite={}, source_job_id="source")


async def test_verification_executes_every_frozen_batch_on_new_candidate(monkeypatch):
    async def get(org, sid, table, identity):
        return (
            {"snapshot": {"workflow_json": {"nodes": []}}}
            if table == "candidates"
            else {"payload": {"cases": [1]}}
        )

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())
    runtime = object()
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(snapshot={}, effective=runtime)),
    )
    calls = []

    async def run(
        org, sid, job, candidate_id, suite_id, config, *, prior_results, on_progress
    ):
        assert candidate_id == "new" and config is runtime
        assert prior_results is None
        calls.append(suite_id)
        result = {
            "id": f"run-{len(calls)}",
            "candidate_id": candidate_id,
            "suite_id": suite_id,
            "results": [{"name": suite_id, "status": "passed"}],
            "summary": {
                "total": 1,
                "passed": 1,
                "error": 0,
                "failed": 0,
                "inconclusive": 0,
            },
        }
        await on_progress(result["id"], result["summary"], 1)
        return result

    monkeypatch.setattr(jobs, "evaluate", run)
    result = await jobs._verify_build(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"text": "test"}},
        {},
        None,
        {
            "candidate_id": "new",
            "suite_id": "extra",
            "regression_suite_ids": ["a", "b"],
        },
    )
    assert calls == ["extra", "a", "b"] * 2
    assert result["evaluation_ids"] == ["run-4", "run-5", "run-6"]
    assert result["verification"]["total"] == 3 and result["verification"]["confirmed"]


async def test_review_receives_saved_receipts_actual_model_and_evidence_index(
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(requirements, "ensure_basis", AsyncMock(return_value={"sources": [], "fingerprint": "basis", "memory_revision": "m", "target_fingerprint": "c", "warnings": []}))
    candidate = {
        "id": "saved",
        "job_id": "j",
        "fingerprint": "new",
        "base_fingerprint": "draft",
        "snapshot": review_evidence()["candidate"],
    }
    monkeypatch.setattr(
        requirements.db_client,
        "wb_get",
        AsyncMock(
            side_effect=[
                candidate,
                {"id": "suite", "payload": review_evidence()["suite"]},
            ]
        ),
    )
    monkeypatch.setattr(requirements.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(
        requirements,
        "dependency_report",
        AsyncMock(return_value={"services": [{"model": "voice-model"}]}),
    )
    reader = SimpleNamespace(
        load_agent_memory=AsyncMock(),
        agent_memory_view={},
        read_conversation=AsyncMock(return_value={}),
        list_attachments=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(tool_module, "WorkbenchTools", lambda *a, **kw: reader)

    async def run(self, prompt, *args, **kwargs):
        captured.update(self.dispatcher.evidence)
        self.dispatcher.report = {
            "status": "passed",
            "requirements": [finding("covered")],
        }
        return {}

    from api.services.agent_workbench.codex_client import CodexClient

    monkeypatch.setattr(CodexClient, "run", run)
    await requirements.review_requirements(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"text": "Preserve policy"}},
        {
            "target": {},
            "target_fingerprint": "draft",
            "session": {"model_protocol": "chat"},
            "model_dependencies": {"services": [{"model": "analysis-model"}]},
            "evaluations": [{"id": "prior", "status": "completed"}],
        },
        SimpleNamespace(llm=None),
        {"candidate_id": "saved", "suite_id": "suite"},
    )
    assert captured["saved_receipts"]["candidate"]["fingerprint"] == "new"
    assert (
        captured["runtime_model_dependencies"]["services"][0]["model"] == "voice-model"
    )
    assert (
        captured["builder_model_dependencies"]["services"][0]["model"]
        == "analysis-model"
    )
    assert captured["prior_evaluations"][0]["id"] == "prior"


async def test_verification_resume_reuses_saved_candidate_without_duplicate_write(
    monkeypatch,
):
    snapshot = {"name": "Original", "workflow_json": {"nodes": [], "edges": []}}
    row = {"id": "selected", "fingerprint": "fp", "snapshot": snapshot}
    monkeypatch.setattr(tool_module.db_client, "wb_get", AsyncMock(return_value=row))
    save = AsyncMock()
    monkeypatch.setattr(tool_module.db_client, "wb_save_candidate", save)
    t = tool_module.WorkbenchTools(
        1,
        "s",
        {"input": {"mode": "build", "candidate_id": "selected"}},
        {"target": snapshot, "target_fingerprint": "draft"},
    )
    monkeypatch.setattr(t, "require_memory_read", AsyncMock())
    result = await t.save_candidate(
        summary="Resume validation", use_current_candidate=True
    )
    assert result["candidate_id"] == "selected" and result["reused"]
    save.assert_not_awaited()
    with pytest.raises(WorkbenchError):
        await t.save_candidate(use_current_candidate=True, node_updates=[{"name": "A"}])
    t.tuning = True
    with pytest.raises(WorkbenchError):
        await t.save_candidate(use_current_candidate=True)
