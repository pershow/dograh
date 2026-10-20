"""Transactional coverage for tenant boundaries, leases and draft adoption."""

import copy
import uuid

import pytest

from api.db.models import OrganizationModel, UserModel, WorkflowModel
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError


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
