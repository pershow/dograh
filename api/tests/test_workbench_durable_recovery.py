"""Recovery keeps frozen evidence across tasks, leases and process lifetimes."""

# ruff: noqa: F811 -- imported transactional fixtures

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from api.db.models import WorkbenchJobModel
from api.services.agent_workbench import basis, jobs, test_tasks
from api.services.agent_workbench.errors import (
    LeaseLost,
    ModelTransportError,
    WorkbenchError,
    retryable_task_error,
)
from api.services.agent_workbench.evaluation import EVALUATION_VERSION
from api.services.agent_workbench.evaluation_progress import (
    result_counts,
    retained_result,
)
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401


async def test_cross_task_ready_cases_do_not_reconcile_confirmed_questions_again(
    monkeypatch,
):
    from api.services.agent_workbench import test_compilation as compiler
    from api.services.agent_workbench.fingerprint import fingerprint
    from api.services.agent_workbench.preparation_questions import reconcile_questions
    from api.tests.test_workbench_preparation_questions import answer, inputs

    case, plan, frozen, questions = inputs()
    frozen.update(target_fingerprint="target", memory_revision="memory")
    frozen["sources"][0].update(source_id="sop-file", session_id="s", sha256="bytes")
    frozen["fingerprint"] = fingerprint(frozen)

    async def decide(_name, _rules, _schema, evidence, **kwargs):
        value = answer(evidence)
        kwargs["validate"](value)
        return value

    reviewed = await reconcile_questions(questions, frozen, [case], decide)
    case["compiled"].update(status="ready", scenario=plan, question_review=reviewed)
    case["compiled"]["fingerprint"] = fingerprint(
        {k: v for k, v in case["compiled"].items() if k != "fingerprint"}
    )
    before = deepcopy(case)
    old = {"id": "old", "input": {"text": "confirmed"}}

    async def get(_org, _sid, table, _id):
        return old if table == "jobs" else {"sha256": "bytes"}

    monkeypatch.setattr(basis.db_client, "wb_get", get)
    monkeypatch.setattr(
        basis.db_client, "wb_task_basis", AsyncMock(return_value=frozen)
    )
    inherited = await basis.continuation_basis(
        1,
        "s",
        {
            "id": "new",
            "created_at": "later",
            "input": {"text": "confirmed", "resume_test_job_id": "old"},
        },
        "target",
        {"revision": "memory"},
        (),
    )
    no_model = AsyncMock(
        side_effect=AssertionError("Confirmed information must not be asked again")
    )
    checkpoint = AsyncMock()
    cases, _ = await compiler.compile_cases(
        [case],
        {"snapshot": {}},
        inherited,
        no_model,
        concurrency=1,
        progress=AsyncMock(),
        checkpoint=checkpoint,
    )
    assert cases == [before]
    no_model.assert_not_awaited()
    checkpoint.assert_not_awaited()


async def test_preparation_transport_failure_saves_stage_before_retry():
    from api.services.agent_workbench import test_compilation as compiler
    from api.tests.test_workbench_fixture_recovery import prepare_inputs

    case, _, frozen = prepare_inputs()
    saved = []

    async def save(case):
        saved.append(deepcopy(case))

    async def progress(_done, _total, case):
        await save(case)

    with pytest.raises(ExceptionGroup) as caught:
        await compiler.compile_cases(
            [case],
            {"snapshot": {}},
            frozen,
            AsyncMock(side_effect=ModelTransportError("TLS lost")),
            concurrency=1,
            progress=progress,
            checkpoint=save,
        )
    assert retryable_task_error(caught.value)
    assert saved[-1]["compiled"]["status"] == "preparation_error"
    assert saved[-1]["compiled"]["preparation_checkpoint"]["stage"] == "generation"
    assert saved[-1]["compiled"]["requirements"] == case["compiled"]["requirements"]


async def due(async_session, jid, *, expire=False):
    row = await async_session.get(WorkbenchJobModel, jid, populate_existing=True)
    past = datetime.now(UTC) - timedelta(seconds=1)
    if expire:
        row.lease_until = past
    else:
        row.output = {
            **row.output,
            "recovery": {**row.output["recovery"], "retry_at": past.isoformat()},
        }
    await async_session.flush()


async def test_cross_task_basis_keeps_citations_without_source_discovery(
    db_session, wb, monkeypatch
):
    org, _, sid = wb
    target = (await db_session.wb_context(org, sid))["target"]
    discover = AsyncMock(return_value=[])
    monkeypatch.setattr(basis, "discover_originals", discover)
    first = await db_session.wb_enqueue(
        org,
        sid,
        "build",
        {"mode": "test", "text": "Only customer statements define facts."},
        "first",
    )
    first = await db_session.wb_claim(org, sid, first["id"])
    frozen = await basis.ensure_basis(org, sid, first, target, llm=object())
    await db_session.wb_finish(org, sid, first["id"], first["lease_token"], "failed")
    second = await db_session.wb_enqueue(
        org, sid, "build", {"text": "continue"}, "second"
    )
    second = await db_session.wb_claim(org, sid, second["id"])
    second = await db_session.wb_resume_test_request(
        org, sid, second["id"], second["lease_token"], first["id"]
    )
    inherited = await basis.ensure_basis(org, sid, second, target, llm=object())
    assert first["id"] != second["id"]
    assert inherited == frozen
    assert basis.source_records(inherited) == basis.source_records(frozen)
    discover.assert_awaited_once()
    assert await db_session.wb_task_basis(org, sid, first["id"]) == frozen
    assert await db_session.wb_task_basis(org, sid, second["id"]) == frozen


@pytest.mark.parametrize(
    "changed", ["request", "memory", "target", "attachments", "bytes", "reference_role"]
)
async def test_changed_inputs_never_inherit_basis(monkeypatch, changed):
    prior = {"id": "old", "input": {"text": "original", "asset_ids": ["a"]}}
    current = {
        "id": "new",
        "input": {**deepcopy(prior["input"]), "resume_test_job_id": "old"},
    }
    frozen = {
        "target_fingerprint": "t",
        "memory_revision": "m",
        "sources": [
            {"kind": "material", "source_id": "a", "session_id": "s", "sha256": "bytes"}
        ],
    }
    memory, target, refs, digest = {"revision": "m"}, "t", (), "bytes"
    if changed == "request":
        current["input"]["text"] = "new fact"
    if changed == "memory":
        memory["revision"] = "new-memory"
    if changed == "target":
        target = "new-target"
    if changed == "attachments":
        current["input"]["asset_ids"] = ["b"]
    if changed == "bytes":
        digest = "changed-bytes"
    if changed == "reference_role":
        refs = ("a",)

    async def get(_org, _sid, table, _id):
        return prior if table == "jobs" else {"sha256": digest}

    monkeypatch.setattr(basis.db_client, "wb_get", get)
    monkeypatch.setattr(
        basis.db_client, "wb_task_basis", AsyncMock(return_value=frozen)
    )
    assert await basis.continuation_basis(1, "s", current, target, memory, refs) is None


async def test_waiting_retry_is_durable_bounded_fenced_and_cancellable(
    db_session, wb, async_session, monkeypatch
):
    monkeypatch.setenv("WORKBENCH_TASK_RECOVERY_ATTEMPTS", "2")
    org, _, sid = wb
    queued = await db_session.wb_enqueue(
        org,
        sid,
        "build",
        {"text": "prepare", "mode": "test", "prepare_only": True},
        "retry",
    )
    first = await db_session.wb_claim(org, sid, queued["id"])
    checkpoint = {
        "preparation_receipts": [{"name": "one", "suite_id": "saved"}],
        "compilation": {"ready": 1},
    }
    await db_session.wb_job_progress(
        org, sid, first["id"], first["lease_token"], checkpoint, {}
    )
    assert await db_session.wb_retry_test(
        org, sid, first["id"], first["lease_token"], "network"
    )
    waiting = await db_session.wb_get(org, sid, "jobs", first["id"])
    assert (
        waiting["status"] == "queued" and waiting["output"]["recovery"]["attempt"] == 1
    )
    assert (
        waiting["output"]["preparation_receipts"] == checkpoint["preparation_receipts"]
    )
    assert (org, sid, first["id"]) not in await db_session.wb_pending()
    assert await db_session.wb_claim(org, sid, first["id"]) is None
    with pytest.raises(LeaseLost):
        await db_session.wb_finish(
            org, sid, first["id"], first["lease_token"], "completed"
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_retry_test(
            org + 1, sid, first["id"], first["lease_token"], "cross-tenant"
        )
    await due(async_session, first["id"])
    assert (org, sid, first["id"]) in await db_session.wb_pending()
    second = await db_session.wb_claim(org, sid, first["id"])
    assert second["lease_token"] != first["lease_token"]
    assert await db_session.wb_claim(org, sid, first["id"]) is None
    assert await db_session.wb_retry_test(
        org, sid, second["id"], second["lease_token"], "again"
    )
    await due(async_session, first["id"])
    third = await db_session.wb_claim(org, sid, first["id"])
    assert not await db_session.wb_retry_test(
        org, sid, third["id"], third["lease_token"], "exhausted"
    )
    await db_session.wb_cancel(org, sid, first["id"])
    assert (org, sid, first["id"]) not in await db_session.wb_pending()
    assert await db_session.wb_claim(org, sid, first["id"]) is None


async def test_worker_loss_reschedules_only_active_test_and_cancel_stays_stopped(
    db_session, wb, async_session
):
    org, _, sid = wb
    queued = await db_session.wb_enqueue(
        org, sid, "build", {"mode": "test", "text": "execute"}, "expired-test"
    )
    first = await db_session.wb_claim(org, sid, queued["id"])
    await due(async_session, first["id"], expire=True)
    await db_session.wb_pending()
    waiting = await db_session.wb_get(org, sid, "jobs", first["id"])
    assert (
        waiting["status"] == "queued" and waiting["output"]["recovery"]["attempt"] == 1
    )
    await db_session.wb_pending()
    assert await db_session.wb_get(org, sid, "jobs", first["id"]) == waiting
    await db_session.wb_cancel(org, sid, first["id"])
    await due(async_session, first["id"])
    assert (org, sid, first["id"]) not in await db_session.wb_pending()
    assert await db_session.wb_claim(org, sid, first["id"]) is None
    with pytest.raises(LeaseLost):
        await db_session.wb_retry_test(
            org, sid, first["id"], first["lease_token"], "late failure"
        )


async def test_repeated_worker_loss_exhausts_budget_without_reviving_failed_job(
    db_session, wb, async_session, monkeypatch
):
    monkeypatch.setenv("WORKBENCH_TASK_RECOVERY_ATTEMPTS", "1")
    org, _, sid = wb
    queued = await db_session.wb_enqueue(
        org, sid, "build", {"mode": "test", "text": "execute"}, "restarts"
    )
    for attempt in (0, 1):
        claimed = await db_session.wb_claim(org, sid, queued["id"])
        assert claimed["output"]["recovery"]["attempt"] == attempt
        await due(async_session, queued["id"], expire=True)
        await db_session.wb_pending()
        if not attempt:
            await due(async_session, queued["id"])
    failed = await db_session.wb_get(org, sid, "jobs", queued["id"])
    assert failed["status"] == failed["output"]["recovery"]["state"] == "failed"
    assert "1 次上限" in failed["error"]
    assert (org, sid, queued["id"]) not in await db_session.wb_pending()
    assert await db_session.wb_claim(org, sid, queued["id"]) is None
    assert await db_session.wb_get(org, sid, "jobs", queued["id"]) == failed


@pytest.mark.parametrize(
    "error,eligible",
    [
        (ModelTransportError("disconnected"), True),
        (
            ExceptionGroup(
                "pool", [ModelTransportError("timeout"), ModelTransportError("TLS")]
            ),
            True,
        ),
        (
            ExceptionGroup(
                "pool", [ModelTransportError("timeout"), ValueError("bad data")]
            ),
            False,
        ),
        (WorkbenchError("connection interrupted, please retry", 403), False),
        (LeaseLost(), False),
    ],
)
def test_only_typed_transport_failures_allow_task_retry(error, eligible):
    assert retryable_task_error(error) is eligible


async def test_progress_starts_with_all_retained_batches_and_replaces_pending_review():
    events = []
    retained = result_counts(
        [
            retained_result(
                {
                    "status": "passed",
                    "run_id": 10,
                    "transcript": [{"role": "assistant", "text": "ok"}],
                }
            )
        ]
    )
    pending = {**retained, "passed": 0, "inconclusive": 1, "review_pending": 1}

    async def save(i):
        return {"id": str(i)}

    async def progress(_phase, _text, **fields):
        events.append(fields["summary"])

    async def run(suite, update, semaphore):
        async with semaphore:
            before = pending if suite["id"] == "1" else retained
            await update("e" + suite["id"], before, before["executed"])
            await asyncio.sleep(0)
        return {"id": "e" + suite["id"], "summary": retained}

    initial = {**dict.fromkeys(retained, 0), "total": 3}
    _, _, counts = await test_tasks.execute_batches(
        [0, 1, 2],
        save,
        run,
        progress,
        initial,
        1,
        initial_snapshots=[retained, pending, retained],
    )
    assert all(e["executed"] == e["reused"] == 3 for e in events)
    assert events[0]["review_pending"] == 1 and events[0]["passed"] == 2
    assert counts["review_pending"] == 0 and counts["passed"] == 3


async def test_full_test_job_recovers_own_receipts_without_reimport_or_replay(
    db_session, wb, async_session, monkeypatch
):
    from api.services.agent_workbench import dependencies

    org, _, sid = wb
    source = await db_session.wb_save_suite(
        org,
        sid,
        {
            "name": "source",
            "cases": [
                {
                    "name": name,
                    "user_messages": ["hello"],
                    "assertions": [{"kind": "contains", "value": "hi"}],
                }
                for name in ("first", "second", "third")
            ],
        },
    )
    queued = await db_session.wb_enqueue(
        org,
        sid,
        "build",
        {
            "text": "execute selected suite",
            "mode": "test",
            "test_suite_ids": [source["id"]],
            "test_mode": "scripted",
            "prepare_only": False,
        },
        "flow",
    )
    runtime = SimpleNamespace(
        snapshot={"version": 3, "services": {}}, effective=SimpleNamespace(llm=None)
    )
    monkeypatch.setattr(test_tasks, "prepare_models", AsyncMock(return_value=runtime))
    monkeypatch.setattr(dependencies, "replay_models", AsyncMock(return_value=runtime))
    load = AsyncMock(return_value=([], []))
    monkeypatch.setattr(test_tasks, "load_sources", load)
    prepare = Mock(wraps=test_tasks.prepare_test_cases)
    monkeypatch.setattr(test_tasks, "prepare_test_cases", prepare)
    decide = AsyncMock(side_effect=AssertionError("Inputs are already confirmed"))
    monkeypatch.setattr(test_tasks, "decision", decide)
    calls, replayed = 0, []

    async def evaluate(
        org,
        sid,
        job,
        candidate_id,
        suite_id,
        _config,
        *,
        prior_results,
        on_progress,
        **kwargs
    ):
        nonlocal calls
        calls += 1
        suite = await db_session.wb_get(org, sid, "suites", suite_id)
        ev = await db_session.wb_evaluation(
            org, sid, job["id"], job["lease_token"], candidate_id, suite_id
        )
        results = []
        for index, case in enumerate(suite["payload"]["cases"]):
            if case["name"] in prior_results:
                results.append(retained_result(prior_results[case["name"]]))
            else:
                replayed.append(case["name"])
                results.append(
                    {
                        "name": case["name"],
                        "status": "passed",
                        "run_id": index + 100,
                        "transcript": [{"role": "assistant", "text": "hi"}],
                        "runtime_responded": True,
                    }
                )
            summary = {
                **result_counts(results),
                "total": 3,
                "evaluation_version": EVALUATION_VERSION,
            }
            await db_session.wb_evaluation_result(
                org,
                sid,
                job["id"],
                job["lease_token"],
                ev["id"],
                results,
                "running",
                summary,
            )
            await on_progress(ev["id"], summary, summary["executed"])
            if calls == 1:
                raise ModelTransportError("fixture TLS failure", 503)
        return {"id": ev["id"], "results": results, "summary": summary}

    monkeypatch.setattr(test_tasks, "evaluate", evaluate)

    async def perform(org, sid, job):
        return await test_tasks.run_test_task(
            org, sid, job, SimpleNamespace(llm=None), "responses", AsyncMock()
        )

    monkeypatch.setattr(jobs, "_perform", perform)
    before = (await db_session.wb_context(org, sid))["target_fingerprint"]
    await jobs.execute_workbench_job({}, org, sid, queued["id"])
    waiting = await db_session.wb_get(org, sid, "jobs", queued["id"])
    assert (
        waiting["status"] == "queued" and waiting["output"]["summary"]["executed"] == 1
    )
    assert len(waiting["output"]["recovery_evaluation_ids"]) == 1
    frozen = await db_session.wb_task_basis(org, sid, queued["id"])
    await due(async_session, queued["id"])
    await jobs.execute_workbench_job({}, org, sid, queued["id"])
    final = await db_session.wb_get(org, sid, "jobs", queued["id"])
    assert final["status"] == "completed", final.get("error")
    assert final["output"]["summary"]["executed"] == 3
    assert final["output"]["summary"]["reused"] == 1
    assert replayed == ["first", "second", "third"]
    assert final["output"]["import_suite_ids"] == waiting["output"]["import_suite_ids"]
    assert await db_session.wb_task_basis(org, sid, queued["id"]) == frozen
    assert (await db_session.wb_context(org, sid))["target_fingerprint"] == before
    load.assert_awaited_once()
    prepare.assert_called_once()
    decide.assert_not_awaited()
