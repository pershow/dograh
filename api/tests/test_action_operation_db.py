import pytest

from api.db.models import WorkflowRunModel
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401


@pytest.fixture
async def action_run(db_session, wb, async_session):
    org, _, session_id = wb
    context = await db_session.wb_context(org, session_id)
    run = WorkflowRunModel(
        name="Action test",
        workflow_id=context["session"]["workflow_id"],
        mode="textchat",
        purpose="evaluation",
    )
    async_session.add(run)
    await async_session.flush()
    return org, run.id


def request(action_run, **patch):
    org, run_id = action_run
    return (
        dict(
            organization_id=org,
            run_id=run_id,
            operation_key="operation",
            tool_uuid="tool",
            arguments={"day": "Friday"},
            arguments_hash="a",
            turn=1,
            confirmed=False,
            confirmation_required=True,
            lease_seconds=60,
        )
        | patch
    )


async def test_confirmation_is_bound_to_arguments_and_later_turn(
    db_session, action_run
):
    pending = await db_session.claim_action(**request(action_run))
    assert not pending["execute"]
    same_turn = await db_session.claim_action(**request(action_run, confirmed=True))
    assert not same_turn["execute"]
    changed = await db_session.claim_action(
        **request(
            action_run,
            confirmed=True,
            turn=2,
            arguments={"day": "Thursday"},
            arguments_hash="b",
        )
    )
    assert not changed["execute"]
    still_unsaid = await db_session.claim_action(
        **request(
            action_run,
            confirmed=True,
            turn=3,
            arguments={"day": "Thursday"},
            arguments_hash="b",
        )
    )
    assert not still_unsaid["execute"]
    assert not await db_session.mark_action_proposed(
        organization_id=action_run[0],
        operation_id=changed["id"],
        arguments_hash="a",
        turn=2,
    )
    assert await db_session.mark_action_proposed(
        organization_id=action_run[0],
        operation_id=changed["id"],
        arguments_hash="b",
        turn=2,
    )
    accepted = await db_session.claim_action(
        **request(
            action_run,
            confirmed=True,
            turn=3,
            arguments={"day": "Thursday"},
            arguments_hash="b",
        )
    )
    assert accepted["execute"]
    duplicate = await db_session.claim_action(
        **request(
            action_run,
            confirmed=True,
            turn=3,
            arguments={"day": "Thursday"},
            arguments_hash="b",
        )
    )
    assert not duplicate["execute"] and duplicate["state"] == "executing"


async def test_unknown_survives_new_claim_and_does_not_retry(db_session, action_run):
    accepted = await db_session.claim_action(
        **request(action_run, confirmation_required=False)
    )
    org, run_id = action_run
    assert await db_session.finish_action(
        organization_id=org,
        operation_id=accepted["id"],
        owner_token=accepted["owner_token"],
        state="unknown",
        result={"reason": "lost response"},
    )
    repeated = await db_session.claim_action(
        **request(action_run, confirmation_required=False, turn=4)
    )
    assert repeated["id"] == accepted["id"]
    assert repeated["state"] == "unknown" and not repeated["execute"]
    fresh = await db_session.claim_action(
        **request(
            action_run, operation_key="new-task-operation", confirmation_required=False
        )
    )
    assert fresh["execute"] and fresh["id"] != accepted["id"]


async def test_other_organization_cannot_claim_or_complete(db_session, action_run):
    org, run_id = action_run
    with pytest.raises(ValueError, match="not found"):
        await db_session.claim_action(
            **request(action_run, organization_id=org + 99999)
        )
    row = await db_session.claim_action(
        **request(action_run, confirmation_required=False)
    )
    assert not await db_session.finish_action(
        organization_id=org + 99999,
        operation_id=row["id"],
        owner_token=row["owner_token"],
        state="succeeded",
        result={},
    )
    assert not await db_session.list_actions(organization_id=org + 99999, run_id=run_id)
    assert not await db_session.finish_action(
        organization_id=org,
        operation_id=row["id"],
        owner_token="wrong-owner",
        state="succeeded",
        result={},
    )


async def test_capability_candidate_preserves_draft_and_checks_stale_base(
    db_session, wb
):
    from api.services.agent_workbench.errors import WorkbenchError

    org, _, session_id = wb
    context = await db_session.wb_context(org, session_id)
    candidate = await db_session.wb_capability_candidate(
        org,
        session_id,
        expected_fingerprint=context["target_fingerprint"],
        patch={
            "agent_behavior": {
                "enabled": True,
                "goal_type": "service",
                "goal": "Resolve the request",
            }
        },
    )
    assert candidate["snapshot"]["workflow_configurations"]["agent_behavior"]["enabled"]
    assert (await db_session.wb_context(org, session_id))["target"] == context["target"]
    with pytest.raises(WorkbenchError):
        await db_session.wb_capability_candidate(
            org, session_id, expected_fingerprint="0" * 64, patch={}
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_capability_candidate(
            org + 99999,
            session_id,
            expected_fingerprint=context["target_fingerprint"],
            patch={},
        )
    adopted = await db_session.wb_adopt(org, session_id, candidate["id"])
    assert not adopted["unchanged"]
    assert (await db_session.wb_context(org, session_id))["target"][
        "workflow_configurations"
    ]["agent_behavior"]["enabled"]
