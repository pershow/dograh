"""Atomic, organization-scoped claims for business operations."""

import copy
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from api.db.base_client import BaseDBClient
from api.db.models import ActionOperationModel, WorkflowModel, WorkflowRunModel


def operation_data(row, *, execute=False):
    return {
        "id": row.id,
        "state": row.state,
        "arguments": copy.deepcopy(row.arguments),
        "result": copy.deepcopy(row.result),
        "owner_token": row.owner_token,
        "execute": execute,
    }


class ActionOperationClient(BaseDBClient):
    async def claim_action(
        self,
        *,
        organization_id,
        run_id,
        operation_key,
        tool_uuid,
        arguments,
        arguments_hash,
        turn,
        confirmed,
        confirmation_required,
        lease_seconds,
    ):
        async with self.async_session() as session:
            run = await session.scalar(
                select(WorkflowRunModel.id)
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(
                    WorkflowRunModel.id == run_id,
                    WorkflowModel.organization_id == organization_id,
                )
            )
            if run is None:
                raise ValueError("Action run not found")
            now = datetime.now(UTC)
            await session.execute(
                insert(ActionOperationModel)
                .values(
                    id=str(uuid.uuid4()),
                    organization_id=organization_id,
                    workflow_run_id=run_id,
                    operation_key=operation_key,
                    tool_uuid=tool_uuid,
                    arguments=arguments,
                    arguments_hash=arguments_hash,
                    state="awaiting_confirmation",
                    request_turn=turn,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "organization_id",
                        "workflow_run_id",
                        "operation_key",
                    ]
                )
            )
            row = await session.scalar(
                select(ActionOperationModel)
                .where(
                    ActionOperationModel.organization_id == organization_id,
                    ActionOperationModel.workflow_run_id == run_id,
                    ActionOperationModel.operation_key == operation_key,
                )
                .with_for_update()
            )
            if row.state == "executing" and row.lease_until and row.lease_until <= now:
                row.state = "unknown"
            if row.arguments_hash != arguments_hash:
                if row.state != "awaiting_confirmation":
                    await session.commit()
                    return {**operation_data(row), "state": "conflict"}
                row.arguments, row.arguments_hash, row.request_turn = (
                    arguments,
                    arguments_hash,
                    turn,
                )
                row.proposed_turn = None
            execute = row.state == "awaiting_confirmation" and (
                not confirmation_required
                or (
                    confirmed
                    and row.proposed_turn is not None
                    and turn > row.proposed_turn
                )
            )
            if execute:
                row.state = "executing"
                row.owner_token = str(uuid.uuid4())
                row.lease_until = now + timedelta(seconds=lease_seconds)
            row.updated_at = now
            result = operation_data(row, execute=execute)
            await session.commit()
            return result

    async def mark_action_proposed(
        self, *, organization_id, operation_id, arguments_hash, turn
    ):
        """Acknowledge an exact proposal only after it reaches conversation output."""
        async with self.async_session() as session:
            row = await session.scalar(
                select(ActionOperationModel)
                .where(
                    ActionOperationModel.id == operation_id,
                    ActionOperationModel.organization_id == organization_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.state != "awaiting_confirmation"
                or row.arguments_hash != arguments_hash
                or turn < row.request_turn
            ):
                return False
            row.proposed_turn = turn
            row.updated_at = datetime.now(UTC)
            await session.commit()
            return True

    async def release_unsubmitted_action(
        self, *, organization_id, operation_id, owner_token, turn
    ):
        """Release only a claim whose caller has not dispatched any request."""
        async with self.async_session() as session:
            row = await session.scalar(
                select(ActionOperationModel)
                .where(
                    ActionOperationModel.id == operation_id,
                    ActionOperationModel.organization_id == organization_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.owner_token != owner_token
                or row.state != "executing"
            ):
                return False
            row.state, row.request_turn, row.proposed_turn = (
                "awaiting_confirmation",
                turn,
                None,
            )
            row.owner_token, row.lease_until = None, None
            await session.commit()
            return True

    async def finish_action(
        self, *, organization_id, operation_id, owner_token, state, result
    ):
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("Invalid action completion state")
        async with self.async_session() as session:
            row = await session.scalar(
                select(ActionOperationModel)
                .where(
                    ActionOperationModel.id == operation_id,
                    ActionOperationModel.organization_id == organization_id,
                )
                .with_for_update()
            )
            if (
                row is None
                or row.owner_token != owner_token
                or row.state not in {"executing", "unknown"}
            ):
                return False
            row.state, row.result, row.updated_at = (
                state,
                copy.deepcopy(result),
                datetime.now(UTC),
            )
            await session.commit()
            return True

    async def list_actions(self, *, organization_id, run_id):
        async with self.async_session() as session:
            rows = await session.scalars(
                select(ActionOperationModel)
                .where(
                    ActionOperationModel.organization_id == organization_id,
                    ActionOperationModel.workflow_run_id == run_id,
                )
                .order_by(ActionOperationModel.created_at)
                .limit(200)
            )
            return [
                {
                    key: value
                    for key, value in operation_data(row).items()
                    if key not in {"owner_token", "execute"}
                }
                for row in rows
            ]
