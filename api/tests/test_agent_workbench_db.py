"""Transactional coverage for tenant boundaries, leases and draft adoption."""

import copy
import uuid

import pytest

from api.db.models import OrganizationModel, UserModel, WorkflowModel
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError


async def test_resume_build_copies_frozen_inputs_and_keeps_source_and_draft(db_session, wb):
    org, user, sid = wb
    context = await db_session.wb_context(org, sid)
    queued = await db_session.wb_enqueue(org, sid, "build", {"text": "original", "mode": "build"}, "original")
    job = await db_session.wb_claim(org, sid, queued["id"])
    candidate = await db_session.wb_save_candidate(org, sid, job["id"], job["lease_token"],
        context["target"], "code", "saved", context["target_fingerprint"])
    suite = await db_session.wb_save_suite(org, sid, {"name": "frozen", "cases": []})
    await db_session.wb_finish(org, sid, job["id"], job["lease_token"], "failed", output={
        "outcome": "build", "candidate_id": candidate["id"], "suite_id": suite["id"],
        "model_configuration_snapshot": {"version": 3, "services": {}},
    }, error="connection interrupted")
    original = await db_session.wb_get(org, sid, "jobs", job["id"])
    payload = {**original["input"], "resume_build_job_id": job["id"]}
    other = await db_session.wb_create_session(org, user, name="Other")
    with pytest.raises(WorkbenchError):
        await db_session.wb_enqueue(org, other["id"], "build", payload, "cross-session")
    with pytest.raises(WorkbenchError):
        await db_session.wb_enqueue(org, sid, "build", {**payload, "text": "changed"}, "changed")
    resumed = await db_session.wb_enqueue(org, sid, "build", payload, "resume")
    assert resumed["id"] != job["id"]
    assert resumed["output"]["build_checkpoint"]["built"]["candidate_id"] == candidate["id"]
    assert resumed["output"]["model_configuration_snapshot"] == original["output"]["model_configuration_snapshot"]
    assert await db_session.wb_requested_target(org, sid, resumed["id"]) == context["target"]
    assert await db_session.wb_get(org, sid, "jobs", job["id"]) == original
    assert (await db_session.wb_context(org, sid))["target_fingerprint"] == context["target_fingerprint"]
    assert (await db_session.wb_enqueue(org, sid, "build", payload, "resume"))["id"] == resumed["id"]


@pytest.mark.parametrize("status", ["needs_attention", "awaiting_input"])
async def test_unfinished_outcomes_are_durable_terminal_receipts(
    db_session, wb, status
):
    org, _, sid = wb
    queued = await db_session.wb_enqueue(org, sid, "build", {"text": "repair"}, status)
    job = await db_session.wb_claim(org, sid, queued["id"])
    output = {
        "candidate_id": "saved",
        "verification": {"status": "inconclusive", "confirmed": False},
    }
    await db_session.wb_finish(
        org, sid, job["id"], job["lease_token"], status, output=output
    )
    stored = await db_session.wb_get(org, sid, "jobs", job["id"])
    assert stored["status"] == status and stored["lease_token"] is None
    page = await db_session.wb_conversation(org, sid)
    receipt = next(e for e in page["events"] if e["type"] == "task." + status)
    assert receipt["payload"]["output"]["candidate_id"] == "saved"
    assert not receipt["payload"]["output"]["verification"]["confirmed"]
    # A terminal unresolved task must not retain an active lock or block a continuation.
    assert (
        await db_session.wb_enqueue(org, sid, "build", {"text": "continue"}, "next")
    )["status"] == "queued"


async def test_run_evidence_is_scoped_to_session_agent_and_excludes_simulations(
    db_session, wb, async_session
):
    from api.db.models import WorkflowRunModel

    org, user, sid = wb
    context = await db_session.wb_context(org, sid)
    workflow_id = context["session"]["workflow_id"]
    other = await db_session.wb_create_session(org, user, name="Other agent")
    rows = [
        WorkflowRunModel(
            workflow_id=workflow_id, name="real", mode="smallwebrtc", purpose="normal"
        ),
        WorkflowRunModel(
            workflow_id=workflow_id,
            name="simulation",
            mode="smallwebrtc",
            purpose="evaluation",
        ),
        WorkflowRunModel(
            workflow_id=other["workflow_id"],
            name="other",
            mode="smallwebrtc",
            purpose="normal",
        ),
    ]
    async_session.add_all(rows)
    await async_session.flush()
    assert [r["id"] for r in await db_session.wb_agent_runs(org, sid)] == [rows[0].id]
    assert await db_session.wb_agent_runs(org, sid, run_id=rows[2].id) == []
    assert await db_session.wb_agent_runs(org, sid, run_id=rows[1].id) == []
    assert await db_session.wb_run_definition(org, sid, rows[2].id) is None
    with pytest.raises(WorkbenchError):
        await db_session.wb_agent_runs(org + 999, sid)


async def test_conversation_restores_both_roles_and_freezes_history_at_enqueue(
    db_session, wb
):
    org, _user, sid = wb
    await db_session.wb_emit(
        org, sid, "voice.transcript", {"role": "user", "text": "hello"}
    )
    await db_session.wb_emit(
        org, sid, "assistant.message", {"text": "remembered response"}
    )
    await db_session.wb_emit(org, sid, "tool.started", {"name": "not conversation"})
    job = await db_session.wb_enqueue(
        org, sid, "build", {"text": "follow-up"}, "memory"
    )
    before = job["output"]["history_before"]
    await db_session.wb_emit(
        org, sid, "voice.transcript", {"role": "user", "text": "future"}
    )
    page = await db_session.wb_conversation(org, sid, before=before, limit=1)
    assert page["events"][0]["payload"]["text"] == "remembered response"
    older = await db_session.wb_conversation(org, sid, before=page["before"], limit=1)
    assert older["events"][0]["payload"]["text"] == "hello"
    assert older["before"] is None
    with pytest.raises(WorkbenchError):
        await db_session.wb_conversation(org + 999, sid)


async def test_test_only_target_is_immutable_scoped_and_cannot_save_edits(
    db_session, wb
):
    org, _, sid = wb
    before = await db_session.wb_context(org, sid)
    queued = await db_session.wb_enqueue(
        org, sid, "build", {"mode": "test", "text": "test"}, "test-only"
    )
    assert "_requested_target" not in queued["output"]
    job = await db_session.wb_claim(org, sid, queued["id"])
    candidate = await db_session.wb_test_target(org, sid, job["id"], job["lease_token"])
    again = await db_session.wb_test_target(org, sid, job["id"], job["lease_token"])
    assert candidate["id"] == again["id"]
    assert candidate["fingerprint"] == before["target_fingerprint"]
    assert (await db_session.wb_context(org, sid))["target_fingerprint"] == before[
        "target_fingerprint"
    ]
    with pytest.raises(WorkbenchError, match="不能修改"):
        await db_session.wb_save_candidate(
            org,
            sid,
            job["id"],
            job["lease_token"],
            before["target"],
            "edit",
            "edit",
            before["target_fingerprint"],
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_test_target(org + 999, sid, job["id"], job["lease_token"])


@pytest.mark.parametrize("has_runtime_snapshot", [False, True])
async def test_early_test_resume_keeps_original_version_scope_and_idempotency(
    db_session, wb, async_session, has_runtime_snapshot
):
    org, user, sid = wb
    before = await db_session.wb_context(org, sid)
    original_input = {
        "text": "Replay every customer turn",
        "mode": "auto",
        "asset_ids": [],
    }
    source = await db_session.wb_enqueue(org, sid, "build", original_input, "source")
    source = await db_session.wb_claim(org, sid, source["id"])
    runtime = (
        {
            "evaluation_model_configuration_snapshot": {"version": 3, "services": {}},
            "evaluation_model_configuration_fingerprint": "runtime-fingerprint",
        }
        if has_runtime_snapshot
        else {}
    )
    await db_session.wb_finish(
        org,
        sid,
        source["id"],
        source["lease_token"],
        "failed",
        output=runtime,
        error="provider rejected request before any execution",
    )
    source = await db_session.wb_get(org, sid, "jobs", source["id"])
    workflow = await async_session.get(WorkflowModel, before["session"]["workflow_id"])
    workflow.name = "A newer draft"
    await async_session.flush()
    after_edit = await db_session.wb_context(org, sid)
    assert after_edit["target_fingerprint"] != before["target_fingerprint"]
    await db_session.wb_settings(org, sid, "responses")
    submitted = {"text": "continue", "mode": "auto", "asset_ids": []}
    current = await db_session.wb_enqueue(org, sid, "build", submitted, "resume")
    current = await db_session.wb_claim(org, sid, current["id"])
    args = (org, sid, current["id"], current["lease_token"], source["id"])
    with pytest.raises(WorkbenchError, match="可恢复"):
        await db_session.wb_resume_test_request(*args)
    with pytest.raises(LeaseLost):
        await db_session.wb_resume_test_request(
            org, sid, current["id"], "stale", source["id"], classified_test=True
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_resume_test_request(
            org + 999, *args[1:], classified_test=True
        )
    other = await db_session.wb_create_session(org, user, name="Other")
    other_job = await db_session.wb_enqueue(
        org, other["id"], "build", original_input, "other"
    )
    with pytest.raises(WorkbenchError):
        await db_session.wb_resume_test_request(
            *args[:4], other_job["id"], classified_test=True
        )
    with pytest.raises(WorkbenchError, match="更早"):
        await db_session.wb_resume_test_request(
            *args[:4], current["id"], classified_test=True
        )
    restored = await db_session.wb_resume_test_request(*args, classified_test=True)
    assert restored["input"]["text"] == original_input["text"]
    assert restored["input"]["mode"] == "test"
    assert "resume_test_job_id" not in restored["input"]
    assert "_submitted_input" not in restored["output"]
    assert restored["output"]["model_protocol"] == "responses"
    assert restored["output"]["test_target_fingerprint"] == before["target_fingerprint"]
    for key, value in runtime.items():
        assert restored["output"][key] == value
    assert (
        await db_session.wb_requested_target(org, sid, current["id"])
        == before["target"]
    )
    assert (
        await db_session.wb_resume_test_request(*args, classified_test=True) == restored
    )
    # Retry the original HTTP request after normalization: one durable job only.
    assert (await db_session.wb_enqueue(org, sid, "build", submitted, "resume"))[
        "id"
    ] == current["id"]
    with pytest.raises(WorkbenchError, match="different input"):
        await db_session.wb_enqueue(
            org, sid, "build", {**submitted, "text": "different"}, "resume"
        )
    frozen = await db_session.wb_test_target(*args[:4])
    assert frozen["fingerprint"] == before["target_fingerprint"]
    assert await db_session.wb_get(org, sid, "jobs", source["id"]) == source
    assert (await db_session.wb_context(org, sid))["target_fingerprint"] == after_edit[
        "target_fingerprint"
    ]
    with pytest.raises(WorkbenchError, match="不能修改"):
        await db_session.wb_save_candidate(
            *args[:4], before["target"], "edit", "edit", before["target_fingerprint"]
        )


async def test_event_tail_and_backward_pages_do_not_replay_the_archive(db_session, wb):
    org, _, sid = wb
    for i in range(145):
        await db_session.wb_emit(org, sid, "assistant.message", {"text": str(i)})
    tail = await db_session.wb_event_page(org, sid, limit=60)
    assert [e["sequence"] for e in tail["events"]] == list(range(86, 146))
    older = await db_session.wb_event_page(org, sid, before=tail["before"], limit=60)
    oldest = await db_session.wb_event_page(org, sid, before=older["before"], limit=60)
    assert [
        e["sequence"] for e in oldest["events"] + older["events"] + tail["events"]
    ] == list(range(1, 146))
    assert oldest["before"] is None
    await db_session.wb_emit(org, sid, "assistant.message", {"text": "new"})
    new = await db_session.wb_event_page(org, sid, after=tail["after"], limit=1)
    assert [e["sequence"] for e in new["events"]] == [146]
    assert not new["has_more"]
    assert not (await db_session.wb_event_page(org, sid, after=146))["events"]
    with pytest.raises(WorkbenchError):
        await db_session.wb_event_page(org + 999, sid)
    with pytest.raises(WorkbenchError):
        await db_session.wb_event_page(org, sid, after=1, before=3)


async def test_context_revision_skips_unchanged_archive_and_invalidates_changes(
    db_session, wb
):
    org, _, sid = wb
    original = await db_session.wb_context(org, sid)
    assert await db_session.wb_context(org, sid, if_revision=original["revision"]) == {
        "unchanged": True,
        "revision": original["revision"],
    }
    await db_session.wb_emit(org, sid, "assistant.message", {"text": "new"})
    assert "target" in await db_session.wb_context(
        org, sid, if_revision=original["revision"]
    )
    with pytest.raises(WorkbenchError):
        await db_session.wb_context(org + 999, sid, if_revision=original["revision"])


async def test_agent_sessions_share_only_the_agent_and_keep_memory_bindings_scoped(
    db_session, wb
):
    org, user, sid = wb
    workflow = (await db_session.wb_context(org, sid))["session"]["workflow_id"]
    assert (await db_session.wb_create_session(org, user, workflow_id=workflow))[
        "id"
    ] == sid
    fresh = await db_session.wb_create_session(
        org, user, workflow_id=workflow, new_session=True
    )
    assert fresh["id"] != sid
    await db_session.wb_emit(org, sid, "codex.bound", {"thread_id": "native-thread"})
    assert (await db_session.wb_codex_binding(org, sid))["thread_id"] == "native-thread"
    assert await db_session.wb_codex_binding(org, fresh["id"]) is None
    assert {s["id"] for s in await db_session.wb_sessions(org, user, workflow)} == {
        sid,
        fresh["id"],
    }
    with pytest.raises(WorkbenchError):
        await db_session.wb_codex_binding(org + 999, sid)
    with pytest.raises(WorkbenchError):
        await db_session.wb_sessions(org + 999, user, workflow)


async def test_session_list_tracks_only_active_jobs_for_its_agent_and_user(db_session, wb):
    org, user, sid = wb
    workflow = (await db_session.wb_context(org, sid))["session"]["workflow_id"]
    second = await db_session.wb_create_session(
        org, user, workflow_id=workflow, new_session=True
    )
    other = await db_session.wb_create_session(org, user, name="Other")
    await db_session.wb_enqueue(org, other["id"], "build", {"text": "other"}, "other")

    async def statuses():
        rows = await db_session.wb_sessions(org, user, workflow)
        assert all("jobs" not in row for row in rows)
        return {row["id"]: row["active_job_status"] for row in rows}

    assert await statuses() == {sid: None, second["id"]: None}
    first_job = await db_session.wb_enqueue(org, sid, "build", {"text": "first"}, "first")
    assert (await statuses())[sid] == "queued"
    claimed = await db_session.wb_claim(org, sid, first_job["id"])
    second_job = await db_session.wb_enqueue(
        org, second["id"], "build", {"text": "second"}, "second"
    )
    assert await statuses() == {sid: "running", second["id"]: "queued"}
    assert await db_session.wb_sessions(org, user + 999, workflow) == []
    await db_session.wb_finish(org, sid, claimed["id"], claimed["lease_token"], "completed")
    await db_session.wb_cancel(org, second["id"], second_job["id"])
    assert await statuses() == {sid: None, second["id"]: None}


async def test_evidence_memory_is_private_scoped_and_survives_task_completion(
    db_session, wb
):
    org, user, sid = wb
    first = await db_session.wb_enqueue(
        org, sid, "build", {"text": "Analyze"}, "evidence-first"
    )
    first = await db_session.wb_claim(org, sid, first["id"])
    page = {"text": "private transcript", "run": {"id": 42}}
    await db_session.wb_remember_run_evidence(
        org, sid, first["id"], first["lease_token"], "42:transcript", page
    )
    await db_session.wb_finish(org, sid, first["id"], first["lease_token"], "completed")
    second = await db_session.wb_enqueue(
        org, sid, "build", {"text": "Follow up"}, "evidence-second"
    )
    assert await db_session.wb_run_evidence(org, sid, second["id"]) == {
        "42:transcript": page
    }
    assert await db_session.wb_run_evidence(org, sid, first["id"]) == {}
    assert "private transcript" not in str(await db_session.wb_context(org, sid))
    assert "private transcript" not in str(await db_session.wb_events(org, sid))
    with pytest.raises(WorkbenchError):
        await db_session.wb_run_evidence(org + 999, sid, second["id"])
    other = await db_session.wb_create_session(org, user, name="Other")
    with pytest.raises(WorkbenchError):
        await db_session.wb_run_evidence(org, other["id"], second["id"])
    with pytest.raises(LeaseLost):
        await db_session.wb_remember_run_evidence(
            org, sid, first["id"], "expired", "42:transcript", page
        )


@pytest.fixture
async def db_session(async_session, db_connection):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from api.db import db_client

    original = db_client.async_session
    db_client.async_session = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield db_client
    finally:
        db_client.async_session = original


@pytest.fixture
async def wb(db_session, async_session):
    org = OrganizationModel(provider_id="workbench-test-" + str(uuid.uuid4()))
    async_session.add(org)
    await async_session.flush()
    user = UserModel(
        provider_id="workbench-user-" + str(uuid.uuid4()),
        selected_organization_id=org.id,
    )
    async_session.add(user)
    await async_session.flush()
    session = await db_session.wb_create_session(org.id, user.id, name="Test agent")
    return org.id, user.id, session["id"]


async def test_tenant_boundary_and_input_idempotency(db_session, wb):
    org, _user, sid = wb
    with pytest.raises(WorkbenchError) as error:
        await db_session.wb_context(org + 999, sid)
    assert error.value.status_code == 404
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "same")
    assert (await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "same"))[
        "id"
    ] == job["id"]
    with pytest.raises(WorkbenchError):
        await db_session.wb_enqueue(org, sid, "build", {"text": "different"}, "same")
    with pytest.raises(WorkbenchError):
        await db_session.wb_settings(org, sid, "responses")
    await db_session.wb_cancel(org, sid, job["id"])
    assert (await db_session.wb_settings(org, sid, "responses"))[
        "model_protocol"
    ] == "responses"


@pytest.mark.parametrize("status", ["queued", "running", "completed"])
async def test_reopening_workflow_restores_same_session_and_job(db_session, wb, status):
    org, user, sid = wb
    context = await db_session.wb_context(org, sid)
    await db_session.wb_settings(org, sid, "responses")
    suite = await db_session.wb_save_suite(
        org, sid, {"name": "Saved suite", "cases": []}
    )
    job = await db_session.wb_enqueue(
        org, sid, "build", {"text": "Build my agent"}, "original-build"
    )
    if status != "queued":
        job = await db_session.wb_claim(org, sid, job["id"])
    if status == "completed":
        await db_session.wb_finish(
            org, sid, job["id"], job["lease_token"], "completed", output={"saved": True}
        )
    events_before = await db_session.wb_events(org, sid)

    # This is the same request made by either the list or editor entry.
    reopened = await db_session.wb_create_session(
        org, user, workflow_id=context["session"]["workflow_id"], model_protocol="chat"
    )
    assert reopened["id"] == sid and reopened["model_protocol"] == "responses"
    restored = await db_session.wb_context(org, reopened["id"])
    assert len(restored["jobs"]) == 1
    assert restored["jobs"][0]["id"] == job["id"]
    assert restored["jobs"][0]["status"] == status
    assert restored["jobs"][0]["input"]["text"] == "Build my agent"
    assert restored["suites"][0]["id"] == suite["id"]
    assert await db_session.wb_events(org, sid) == events_before

    with pytest.raises(WorkbenchError) as error:
        await db_session.wb_create_session(
            org + 999, user, workflow_id=context["session"]["workflow_id"]
        )
    assert error.value.status_code == 404


async def test_cancellation_fences_late_worker_writes(db_session, wb):
    org, _user, sid = wb
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "new")
    job = await db_session.wb_claim(org, sid, job["id"])
    assert job and job["lease_token"]
    await db_session.wb_cancel(org, sid, job["id"])
    with pytest.raises(LeaseLost):
        await db_session.wb_finish(org, sid, job["id"], job["lease_token"], "completed")
    with pytest.raises(LeaseLost):
        await db_session.wb_emit(
            org,
            sid,
            "candidate.created",
            {},
            job_id=job["id"],
            token=job["lease_token"],
        )


async def test_cross_session_references_and_adoption_conflict(
    db_session, wb, async_session
):
    org, _user, sid = wb
    context = await db_session.wb_context(org, sid)
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "new")
    job = await db_session.wb_claim(org, sid, job["id"])
    snapshot = copy.deepcopy(context["target"])
    snapshot["name"] = "Built agent"
    snapshot["template_context_variables"] = {
        "company": "From the request",
        "customer": "",
    }
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        job["id"],
        job["lease_token"],
        snapshot,
        "code",
        "summary",
        context["target_fingerprint"],
    )
    await db_session.wb_finish(org, sid, job["id"], job["lease_token"], "completed")
    other = await db_session.wb_create_session(org, _user, name="Other")
    with pytest.raises(WorkbenchError):
        await db_session.wb_enqueue(
            org, other["id"], "evaluate", {"candidate_id": candidate["id"]}, "cross"
        )
    saved = await db_session.wb_adopt(org, sid, candidate["id"])
    assert not saved["unchanged"]
    workflow = await async_session.get(WorkflowModel, context["session"]["workflow_id"])
    assert workflow.released_definition_id is None
    assert workflow.template_context_variables == snapshot["template_context_variables"]
    assert (await db_session.wb_context(org, sid))["target"][
        "template_context_variables"
    ] == snapshot["template_context_variables"]
    assert (await db_session.wb_adopt(org, sid, candidate["id"]))["unchanged"]
    workflow.name = "Concurrent manual edit"
    await async_session.flush()
    with pytest.raises(WorkbenchError) as error:
        await db_session.wb_adopt(org, sid, candidate["id"])
    assert error.value.status_code == 409


async def test_evaluation_runs_are_hidden_from_normal_queries(db_session, wb):
    org, _user, sid = wb
    context = await db_session.wb_context(org, sid)
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "new")
    job = await db_session.wb_claim(org, sid, job["id"])
    await db_session.wb_pin_models(
        org,
        sid,
        job["id"],
        job["lease_token"],
        {"version": 3, "mode": "pipeline", "services": {}},
    )
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        job["id"],
        job["lease_token"],
        context["target"],
        "code",
        "summary",
        context["target_fingerprint"],
    )
    run = await db_session.wb_create_run(
        org, sid, job["id"], job["lease_token"], candidate["id"], {}
    )
    assert await db_session.get_workflow_run(run, organization_id=org) is None
    internal, _ = await db_session.get_workflow_run_with_context(run)
    assert (
        internal.purpose == "evaluation"
        and internal.definition_id == candidate["definition_id"]
    )


async def test_expired_job_stops_evaluation_and_emits_failure(
    db_session, wb, async_session
):
    from datetime import UTC, datetime, timedelta

    from api.db.models import WorkbenchJobModel

    org, _user, sid = wb
    context = await db_session.wb_context(org, sid)
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "build"}, "expire")
    job = await db_session.wb_claim(org, sid, job["id"])
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        job["id"],
        job["lease_token"],
        context["target"],
        "code",
        "summary",
        context["target_fingerprint"],
    )
    suite = await db_session.wb_save_suite(org, sid, {"name": "case", "cases": []})
    evaluation = await db_session.wb_evaluation(
        org, sid, job["id"], job["lease_token"], candidate["id"], suite["id"]
    )
    row = await async_session.get(WorkbenchJobModel, job["id"])
    row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    await async_session.flush()
    await db_session.wb_pending()
    result = await db_session.wb_get(org, sid, "evaluations", evaluation["id"])
    assert result["status"] == "failed"
    assert (await db_session.wb_events(org, sid))[-1]["type"] == "task.failed"
    with pytest.raises(LeaseLost):
        await db_session.wb_heartbeat(org, sid, job["id"], job["lease_token"])


async def test_job_model_pin_is_immutable_and_copied_into_every_run(db_session, wb):
    from api.services.agent_workbench.fingerprint import fingerprint

    org, _user, sid = wb
    context = await db_session.wb_context(org, sid)
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "model pin"}, "pin")
    job = await db_session.wb_claim(org, sid, job["id"])
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        job["id"],
        job["lease_token"],
        context["target"],
        "code",
        "summary",
        context["target_fingerprint"],
    )
    with pytest.raises(WorkbenchError, match="Freeze"):
        await db_session.wb_create_run(
            org, sid, job["id"], job["lease_token"], candidate["id"], {}
        )
    snapshot = {
        "version": 3,
        "mode": "pipeline",
        "services": {"llm": {"provider": "openai", "settings": {"model": "frozen"}}},
    }
    pinned = await db_session.wb_pin_models(
        org, sid, job["id"], job["lease_token"], snapshot
    )
    with pytest.raises(WorkbenchError, match="already frozen"):
        await db_session.wb_pin_models(
            org, sid, job["id"], job["lease_token"], {**snapshot, "mode": "realtime"}
        )
    for _ in range(2):
        run_id = await db_session.wb_create_run(
            org, sid, job["id"], job["lease_token"], candidate["id"], {}
        )
        run, _ = await db_session.get_workflow_run_with_context(run_id)
        assert run.model_configuration_snapshot == snapshot
        assert run.extra["model_configuration_fingerprint"] == fingerprint(snapshot)
    voice_snapshot = {
        **snapshot,
        "services": {
            "llm": {"provider": "openai", "settings": {"model": "actual-voice"}}
        },
    }
    await db_session.wb_pin_models(
        org, sid, job["id"], job["lease_token"], voice_snapshot, evaluation=True
    )
    with pytest.raises(WorkbenchError, match="already frozen"):
        await db_session.wb_pin_models(
            org, sid, job["id"], job["lease_token"], snapshot, evaluation=True
        )
    run_id = await db_session.wb_create_run(
        org, sid, job["id"], job["lease_token"], candidate["id"], {}
    )
    run, _ = await db_session.get_workflow_run_with_context(run_id)
    assert run.model_configuration_snapshot == voice_snapshot
    assert run.extra["model_configuration_fingerprint"] == fingerprint(voice_snapshot)
    await db_session.wb_finish(
        org,
        sid,
        job["id"],
        job["lease_token"],
        "completed",
        output={"candidate_id": candidate["id"]},
    )
    completed = await db_session.wb_enqueue(
        org, sid, "build", {"text": "model pin"}, "pin"
    )
    assert (
        completed["output"]["model_configuration_snapshot"]
        == pinned["model_configuration_snapshot"]
    )


async def test_blank_creation_can_start_without_publishing(db_session, wb):
    org, user, _sid = wb
    workflow = await db_session.create_workflow(
        "Blank", {"nodes": [], "edges": []}, user, org, draft_only=True
    )
    assert workflow.released_definition_id is None
    draft = await db_session.get_draft_version(workflow.id)
    assert draft is not None and draft.status == "draft"
    assert await db_session.get_workflow_version_summaries(workflow.id, org) == []


async def test_cross_session_test_target_keeps_same_agent_and_tenant_boundaries(
    db_session, wb
):
    org, user, sid = wb
    before = await db_session.wb_context(org, sid)
    original = await db_session.wb_enqueue(
        org, sid, "build", {"mode": "test", "text": "test"}, "source-test"
    )
    original = await db_session.wb_claim(org, sid, original["id"])
    candidate = await db_session.wb_test_target(
        org, sid, original["id"], original["lease_token"]
    )
    fresh = await db_session.wb_create_session(
        org, user, workflow_id=before["session"]["workflow_id"], new_session=True
    )
    payload = {
        "mode": "test",
        "text": "rerun",
        "candidate_id": candidate["id"],
        "test_target_session_id": sid,
    }
    job = await db_session.wb_enqueue(org, fresh["id"], "build", payload, "new-test")
    job = await db_session.wb_claim(org, fresh["id"], job["id"])
    frozen = await db_session.wb_test_target(
        org, fresh["id"], job["id"], job["lease_token"]
    )
    assert frozen["fingerprint"] == candidate["fingerprint"]
    assert frozen["id"] != candidate["id"]
    assert (await db_session.wb_context(org, sid))["target_fingerprint"] == before[
        "target_fingerprint"
    ]
    other = await db_session.wb_create_session(org, user, name="unrelated")
    with pytest.raises(WorkbenchError, match="同一个 Agent"):
        await db_session.wb_enqueue(org, other["id"], "build", payload, "wrong-agent")
    await db_session.wb_cancel(org, fresh["id"], job["id"])
    with pytest.raises(WorkbenchError, match="只读测试"):
        await db_session.wb_enqueue(
            org, fresh["id"], "build", {**payload, "mode": "build"}, "write"
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_enqueue(
            org + 999, fresh["id"], "build", payload, "wrong-org"
        )


async def test_diagnostic_runs_cross_sessions_but_never_agent_or_tenant_boundaries(db_session, wb, async_session):
    from api.db.models import WorkflowRunModel

    org, user, sid = wb
    context = await db_session.wb_context(org, sid)
    wid = context['session']['workflow_id']
    other = await db_session.wb_create_session(org, user, name='Unrelated')
    rows = [WorkflowRunModel(workflow_id=w, name='diagnostic', mode='smallwebrtc', purpose='evaluation')
            for w in [wid, other['workflow_id']]]
    async_session.add_all(rows)
    await async_session.flush()
    resumed = await db_session.wb_create_session(org, user, workflow_id=wid, new_session=True)
    packet = await db_session.wb_diagnostic_run(org, resumed['id'], rows[0].id)
    assert packet['run']['id'] == rows[0].id
    assert packet['definition'] is None  # Never substitute today's mutable draft.
    with pytest.raises(WorkbenchError):
        await db_session.wb_diagnostic_run(org, sid, rows[1].id)
    with pytest.raises(WorkbenchError):
        await db_session.wb_diagnostic_run(org + 999, sid, rows[0].id)


async def test_diagnostic_probe_receipts_survive_jobs_and_remain_session_scoped(db_session, wb, async_session):
    from api.db.models import WorkflowRunModel
    org, user, sid = wb
    context = await db_session.wb_context(org, sid)
    wid = context['session']['workflow_id']
    run = WorkflowRunModel(workflow_id=wid, name='receipt', mode='smallwebrtc', purpose='evaluation')
    async_session.add(run)
    await async_session.flush()
    receipt = {'evidence_id': 'probe:old', 'run_id': run.id, 'results': [{'text': 'Observed'}]}
    await db_session.wb_emit(org, sid, 'diagnosis.probe.completed', receipt)
    assert (await db_session.wb_diagnostic_probes(org, sid, [run.id]))[0]['evidence_id'] == 'probe:old'
    other = await db_session.wb_create_session(org, user, workflow_id=wid, new_session=True)
    assert await db_session.wb_diagnostic_probes(org, other['id'], [run.id]) == []
    with pytest.raises(WorkbenchError):
        await db_session.wb_diagnostic_probes(org + 999, sid, [run.id])


async def test_tuning_task_scope_is_tenant_scoped_complete_and_read_only(
    db_session, wb, monkeypatch
):
    from api.services.agent_workbench import tuning

    org, user, sid = wb
    monkeypatch.setattr(tuning, "db_client", db_session)
    before = await db_session.wb_context(org, sid)
    queued = await db_session.wb_enqueue(
        org, sid, "build", {"text": "test only"}, "scope-test"
    )
    job = await db_session.wb_claim(org, sid, queued["id"])
    suites = [
        await db_session.wb_save_suite(
            org,
            sid,
            {
                "name": f"batch {i}",
                "cases": [{"name": f"case {i}-{n}"} for n in range(size)],
            },
        )
        for i, size in enumerate([3, 1])
    ]
    await db_session.wb_finish(
        org,
        sid,
        job["id"],
        job["lease_token"],
        "completed",
        output={
            "outcome": "test",
            "candidate_id": "frozen",
            "suite_ids": [s["id"] for s in suites],
            "summary": {"total": 4},
        },
    )
    payload = {"candidate_id": "frozen", "test_job_id": job["id"]}
    scope, selected, _ = await tuning.resolve_scope(org, sid, payload)
    assert scope["case_count"] == 4 and scope["suite_count"] == 2
    assert [s["id"] for s in selected] == [s["id"] for s in suites]
    with pytest.raises(WorkbenchError):
        await tuning.resolve_scope(org + 999, sid, payload)
    other = await db_session.wb_create_session(org, user, name="Other task")
    with pytest.raises(WorkbenchError):
        await tuning.resolve_scope(org, other["id"], payload)
    assert (await db_session.wb_context(org, sid))["target_fingerprint"] == before[
        "target_fingerprint"
    ]
