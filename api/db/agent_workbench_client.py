"""Organization-scoped workbench persistence, leases and atomic draft adoption."""

import copy
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from api.db.base_client import BaseDBClient
from api.db.models import (
    OrganizationModel,
    WorkbenchAssetModel,
    WorkbenchCandidateModel,
    WorkbenchEvaluationModel,
    WorkbenchEventModel,
    WorkbenchJobModel,
    WorkbenchSessionModel,
    WorkbenchSuiteModel,
    WorkflowDefinitionModel,
    WorkflowModel,
    WorkflowRunModel,
)
from api.db.workflow_client import WorkflowClient
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.mps_policy import check_definition_models

TABLES = {
    "assets": WorkbenchAssetModel,
    "candidates": WorkbenchCandidateModel,
    "suites": WorkbenchSuiteModel,
    "evaluations": WorkbenchEvaluationModel,
    "jobs": WorkbenchJobModel,
}


def record(row):
    return {
        column.name: (
            value.isoformat()
            if isinstance(value := getattr(row, column.name), datetime)
            else copy.deepcopy(value)
        )
        for column in row.__table__.columns
    }


def definition_payload(workflow, definition):
    return {
        "name": workflow.name,
        "workflow_json": definition.workflow_json
        if definition
        else workflow.workflow_definition,
        "workflow_configurations": definition.workflow_configurations
        if definition
        else workflow.workflow_configurations,
        "template_context_variables": definition.template_context_variables
        if definition
        else workflow.template_context_variables,
    }


class AgentWorkbenchClient(BaseDBClient):
    async def _wb_session(self, db, org, session_id, *, lock=False):
        query = select(WorkbenchSessionModel).where(
            WorkbenchSessionModel.id == session_id,
            WorkbenchSessionModel.organization_id == org,
        )
        if lock:
            query = query.with_for_update()
        row = await db.scalar(query)
        if row is None:
            raise WorkbenchError("Workbench session not found", 404)
        return row

    async def _wb_item(self, db, table, session_id, item_id):
        model = TABLES[table]
        row = await db.scalar(
            select(model).where(model.id == item_id, model.session_id == session_id)
        )
        if row is None:
            raise WorkbenchError(f"Workbench {table} item not found", 404)
        return row

    async def _wb_fence(self, db, session_id, job_id, token):
        job = await self._wb_item(db, "jobs", session_id, job_id)
        if (
            job.status != "running"
            or job.lease_token != token
            or job.lease_until <= datetime.now(UTC)
        ):
            raise LeaseLost()
        return job

    async def _wb_event(self, db, session, kind, payload):
        session.event_sequence += 1
        db.add(
            WorkbenchEventModel(
                session_id=session.id,
                sequence=session.event_sequence,
                type=kind,
                payload=payload,
            )
        )

    async def _wb_target(self, db, workflow_id, org, *, lock=False):
        query = select(WorkflowModel).where(
            WorkflowModel.id == workflow_id, WorkflowModel.organization_id == org
        )
        if lock:
            query = query.with_for_update()
        workflow = await db.scalar(query)
        if not workflow:
            raise WorkbenchError("Agent not found", 404)
        draft = await db.scalar(
            select(WorkflowDefinitionModel).where(
                WorkflowDefinitionModel.workflow_id == workflow_id,
                WorkflowDefinitionModel.status == "draft",
            )
        )
        definition = draft or (
            await db.get(WorkflowDefinitionModel, workflow.released_definition_id)
            if workflow.released_definition_id
            else None
        )
        return workflow, draft, definition_payload(workflow, definition)

    async def wb_create_session(
        self, org, user_id, workflow_id=None, name="New Agent", model_protocol="chat"
    ):
        async with self.async_session() as db, db.begin():
            if workflow_id is None:
                workflow = WorkflowModel(
                    organization_id=org,
                    user_id=user_id,
                    name=name,
                    workflow_definition={"nodes": [], "edges": []},
                )
                db.add(workflow)
                await db.flush()
                workflow_id = workflow.id
                # Unlike create_workflow(), nothing is published until the user publishes.
                db.add(
                    WorkflowDefinitionModel(
                        workflow_id=workflow_id,
                        status="draft",
                        version_number=1,
                        workflow_json={"nodes": [], "edges": []},
                        workflow_configurations={},
                        template_context_variables={},
                    )
                )
            else:
                await self._wb_target(db, workflow_id, org)
                existing = await db.scalar(
                    select(WorkbenchSessionModel)
                    .where(
                        WorkbenchSessionModel.organization_id == org,
                        WorkbenchSessionModel.workflow_id == workflow_id,
                        WorkbenchSessionModel.user_id == user_id,
                    )
                    .order_by(WorkbenchSessionModel.created_at.desc())
                )
                if existing:
                    return record(existing)
            row = WorkbenchSessionModel(
                organization_id=org,
                user_id=user_id,
                workflow_id=workflow_id,
                model_protocol=model_protocol,
            )
            db.add(row)
            await db.flush()
            return record(row)

    async def wb_settings(self, org, session_id, model_protocol):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            active = await db.scalar(
                select(WorkbenchJobModel.id).where(
                    WorkbenchJobModel.session_id == session_id,
                    WorkbenchJobModel.status.in_(["queued", "running"]),
                )
            )
            if active:
                raise WorkbenchError(
                    "Cancel or finish the task before changing model protocol", 409
                )
            session.model_protocol = model_protocol
            return record(session)

    async def wb_context(self, org, session_id):
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            _, _, target = await self._wb_target(db, session.workflow_id, org)
            result = {
                "session": record(session),
                "target": target,
                "target_fingerprint": fingerprint(target),
            }
            for name, model in TABLES.items():
                rows = (
                    await db.scalars(
                        select(model)
                        .where(model.session_id == session_id)
                        .order_by(model.created_at.desc())
                        .limit(100)
                    )
                ).all()
                result[name] = [record(row) for row in rows]
            return result

    async def wb_get(self, org, session_id, table, item_id):
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            row = await self._wb_item(db, table, session_id, item_id)
            result = record(row)
            if table == "candidates":
                definition = await db.get(WorkflowDefinitionModel, row.definition_id)
                result["snapshot"] = {
                    "name": row.name,
                    "workflow_json": definition.workflow_json,
                    "workflow_configurations": definition.workflow_configurations,
                    "template_context_variables": definition.template_context_variables,
                }
            return result

    async def wb_events(self, org, session_id, after=0):
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            rows = (
                await db.scalars(
                    select(WorkbenchEventModel)
                    .where(
                        WorkbenchEventModel.session_id == session_id,
                        WorkbenchEventModel.sequence > after,
                    )
                    .order_by(WorkbenchEventModel.sequence)
                    .limit(200)
                )
            ).all()
            return [record(row) for row in rows]

    async def wb_emit(self, org, session_id, kind, payload, *, job_id=None, token=None):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            if job_id:
                await self._wb_fence(db, session_id, job_id, token)
            await self._wb_event(db, session, kind, payload)

    async def wb_enqueue(self, org, session_id, kind, payload, key):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            old = await db.scalar(
                select(WorkbenchJobModel).where(
                    WorkbenchJobModel.session_id == session_id,
                    WorkbenchJobModel.idempotency_key == key,
                )
            )
            if old:
                if old.kind != kind or old.input != payload:
                    raise WorkbenchError(
                        "Idempotency key was already used with different input", 409
                    )
                return record(old)
            active = await db.scalar(
                select(WorkbenchJobModel.id).where(
                    WorkbenchJobModel.session_id == session_id,
                    WorkbenchJobModel.status.in_(["queued", "running"]),
                )
            )
            if active:
                raise WorkbenchError(
                    "Wait for the current task, or cancel it first", 409
                )
            for asset_id in payload.get("asset_ids", []):
                asset = await self._wb_item(db, "assets", session_id, asset_id)
                if asset.status != "ready":
                    raise WorkbenchError("An attachment is not ready")
            for field, table in (
                ("candidate_id", "candidates"),
                ("suite_id", "suites"),
            ):
                if payload.get(field):
                    await self._wb_item(db, table, session_id, payload[field])
            row = WorkbenchJobModel(
                session_id=session_id, kind=kind, input=payload, idempotency_key=key
            )
            db.add(row)
            await db.flush()
            await self._wb_event(
                db,
                session,
                "task.queued",
                {"job_id": row.id, "kind": kind, "input": payload},
            )
            return record(row)

    async def wb_claim(self, org, session_id, job_id):
        async with self.async_session() as db, db.begin():
            await db.scalar(
                select(OrganizationModel)
                .where(OrganizationModel.id == org)
                .with_for_update()
            )
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_item(db, "jobs", session_id, job_id)
            if job.status != "queued":
                return None
            count = await db.scalar(
                select(func.count())
                .select_from(WorkbenchJobModel)
                .join(WorkbenchSessionModel)
                .where(
                    WorkbenchSessionModel.organization_id == org,
                    WorkbenchJobModel.status == "running",
                    WorkbenchJobModel.lease_until > datetime.now(UTC),
                )
            )
            if count >= 2:
                return None
            job.status = "running"
            job.lease_token = str(uuid.uuid4())
            job.lease_until = datetime.now(UTC) + timedelta(seconds=60)
            await self._wb_event(
                db, session, "task.started", {"job_id": job.id, "kind": job.kind}
            )
            return record(job)

    async def wb_heartbeat(self, org, session_id, job_id, token):
        async with self.async_session() as db, db.begin():
            await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            job.lease_until = datetime.now(UTC) + timedelta(seconds=60)

    async def _wb_stop_evaluations(self, db, session_id, job_id, status):
        rows = (
            await db.scalars(
                select(WorkbenchEvaluationModel).where(
                    WorkbenchEvaluationModel.session_id == session_id,
                    WorkbenchEvaluationModel.job_id == job_id,
                    WorkbenchEvaluationModel.status == "running",
                )
            )
        ).all()
        for row in rows:
            row.status = status

    async def wb_finish(
        self, org, session_id, job_id, token, status, output=None, error=None
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            job.status, job.output, job.error = (
                status,
                {**job.output, **(output or {})},
                error,
            )
            job.lease_token, job.lease_until = None, None
            if status != "completed":
                await self._wb_stop_evaluations(db, session_id, job_id, status)
            await self._wb_event(
                db,
                session,
                "task." + status,
                {"job_id": job.id, "output": job.output, "error": error},
            )

    async def wb_cancel(self, org, session_id, job_id):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_item(db, "jobs", session_id, job_id)
            if job.status in ("queued", "running"):
                job.status, job.lease_token, job.lease_until = "cancelled", None, None
                await self._wb_stop_evaluations(db, session_id, job_id, "cancelled")
                await self._wb_event(db, session, "task.cancelled", {"job_id": job.id})
            return record(job)

    async def wb_pending(self):
        """System-only outbox scan. Never exposed to a user or model tool."""
        async with self.async_session() as db, db.begin():
            expired = (
                await db.execute(
                    select(
                        WorkbenchSessionModel.organization_id,
                        WorkbenchJobModel.session_id,
                        WorkbenchJobModel.id,
                    )
                    .join(WorkbenchSessionModel)
                    .where(
                        WorkbenchJobModel.status == "running",
                        WorkbenchJobModel.lease_until < datetime.now(UTC),
                    )
                    .limit(100)
                )
            ).all()
            for org, session_id, job_id in expired:
                session = await self._wb_session(db, org, session_id, lock=True)
                job = await self._wb_item(db, "jobs", session_id, job_id)
                if job.status != "running" or job.lease_until >= datetime.now(UTC):
                    continue
                job.status, job.error, job.lease_token, job.lease_until = (
                    "failed",
                    "Worker stopped; retry the task",
                    None,
                    None,
                )
                await self._wb_stop_evaluations(db, session_id, job_id, "failed")
                await self._wb_event(
                    db, session, "task.failed", {"job_id": job_id, "error": job.error}
                )
            rows = (
                await db.execute(
                    select(
                        WorkbenchSessionModel.organization_id,
                        WorkbenchJobModel.session_id,
                        WorkbenchJobModel.id,
                    )
                    .join(WorkbenchSessionModel)
                    .where(WorkbenchJobModel.status == "queued")
                    .limit(100)
                )
            ).all()
            return [tuple(row) for row in rows]

    async def wb_asset(self, org, session_id, **fields):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            row = WorkbenchAssetModel(session_id=session_id, **fields)
            db.add(row)
            await db.flush()
            await self._wb_event(
                db, session, "asset.uploaded", {"asset_id": row.id, "name": row.name}
            )
            return record(row)

    async def wb_asset_result(self, org, session_id, asset_id, parsed=None, error=None):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            row = await self._wb_item(db, "assets", session_id, asset_id)
            row.status, row.parsed, row.error = (
                ("failed" if error else "ready"),
                parsed or {},
                error,
            )
            await self._wb_event(
                db, session, "asset." + row.status, {"asset_id": row.id, "error": error}
            )
            return record(row)

    async def wb_save_candidate(
        self, org, session_id, job_id, token, snapshot, code, summary, base_fingerprint
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            await self._wb_fence(db, session_id, job_id, token)
            definition = WorkflowDefinitionModel(
                workflow_id=session.workflow_id,
                status="candidate",
                is_current=False,
                workflow_json=snapshot["workflow_json"],
                workflow_configurations=snapshot["workflow_configurations"],
                template_context_variables=snapshot["template_context_variables"],
            )
            db.add(definition)
            await db.flush()
            row = WorkbenchCandidateModel(
                session_id=session_id,
                job_id=job_id,
                definition_id=definition.id,
                code=code,
                name=snapshot["name"],
                summary=summary,
                fingerprint=fingerprint(snapshot),
                base_fingerprint=base_fingerprint,
            )
            db.add(row)
            await db.flush()
            await self._wb_event(
                db,
                session,
                "candidate.created",
                {"candidate_id": row.id, "summary": summary},
            )
            return record(row)

    async def wb_save_suite(self, org, session_id, payload, *, job_id=None, token=None):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            if job_id:
                await self._wb_fence(db, session_id, job_id, token)
            row = WorkbenchSuiteModel(
                session_id=session_id, payload=payload, fingerprint=fingerprint(payload)
            )
            db.add(row)
            await db.flush()
            await self._wb_event(
                db,
                session,
                "suite.created",
                {"suite_id": row.id, "name": payload["name"]},
            )
            return record(row)

    async def wb_pin_models(self, org, session_id, job_id, token, snapshot):
        async with self.async_session() as db, db.begin():
            await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            if job.output.get("model_configuration_snapshot"):
                raise WorkbenchError("Task model configuration is already frozen", 409)
            job.output = {
                **job.output,
                "model_configuration_snapshot": copy.deepcopy(snapshot),
                "model_configuration_fingerprint": fingerprint(snapshot),
            }
            return copy.deepcopy(job.output)

    async def wb_evaluation(
        self, org, session_id, job_id, token, candidate_id, suite_id
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            await self._wb_item(db, "candidates", session_id, candidate_id)
            await self._wb_item(db, "suites", session_id, suite_id)
            row = WorkbenchEvaluationModel(
                session_id=session_id,
                job_id=job_id,
                candidate_id=candidate_id,
                suite_id=suite_id,
                summary={
                    "model_configuration_fingerprint": job.output.get(
                        "model_configuration_fingerprint"
                    )
                },
            )
            db.add(row)
            await db.flush()
            await self._wb_event(
                db, session, "evaluation.started", {"evaluation_id": row.id}
            )
            return record(row)

    async def wb_evaluation_result(
        self, org, session_id, job_id, token, evaluation_id, results, status, summary
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            await self._wb_fence(db, session_id, job_id, token)
            row = await self._wb_item(db, "evaluations", session_id, evaluation_id)
            row.results, row.status, row.summary = results, status, summary
            await self._wb_event(
                db,
                session,
                "evaluation.updated",
                {"evaluation_id": row.id, "status": status, "summary": summary},
            )

    async def wb_create_run(
        self, org, session_id, job_id, token, candidate_id, initial_context
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            snapshot = job.output.get("model_configuration_snapshot")
            if not isinstance(snapshot, dict) or snapshot.get("version") != 3:
                raise WorkbenchError("Freeze the task model configuration before evaluation", 409)
            candidate = await self._wb_item(db, "candidates", session_id, candidate_id)
            run = WorkflowRunModel(
                name="Workbench simulation",
                workflow_id=session.workflow_id,
                definition_id=candidate.definition_id,
                mode="textchat",
                purpose="evaluation",
                model_configuration_snapshot=copy.deepcopy(snapshot),
                initial_context=initial_context,
                gathered_context={},
                logs={},
                extra={
                    "workbench_session_id": session_id,
                    "workbench_job_id": job_id,
                    "model_configuration_fingerprint": job.output.get(
                        "model_configuration_fingerprint"
                    ),
                },
            )
            db.add(run)
            await db.flush()
            return run.id

    async def wb_adopt(self, org, session_id, candidate_id):
        async with self.async_session() as db, db.begin():
            # Same org catalog lock used by normal draft saves and publication.
            await db.scalar(
                select(OrganizationModel)
                .where(OrganizationModel.id == org)
                .with_for_update()
            )
            session = await self._wb_session(db, org, session_id, lock=True)
            candidate = await self._wb_item(db, "candidates", session_id, candidate_id)
            workflow, draft, target = await self._wb_target(
                db, session.workflow_id, org, lock=True
            )
            if fingerprint(target) == candidate.fingerprint:
                return {
                    "workflow_id": workflow.id,
                    "definition_id": draft.id if draft else None,
                    "unchanged": True,
                }
            if fingerprint(target) != candidate.base_fingerprint:
                raise WorkbenchError(
                    "The agent changed since this candidate was built. Rebuild against the current draft.",
                    409,
                )
            definition = await db.get(WorkflowDefinitionModel, candidate.definition_id)
            check_definition_models(
                definition.workflow_json, definition.workflow_configurations
            )
            await WorkflowClient._check_workflow_model_references(
                self, db, org, definition.workflow_configurations
            )
            if draft is None:
                draft = WorkflowDefinitionModel(
                    workflow_id=workflow.id,
                    status="draft",
                    is_current=False,
                    version_number=await WorkflowClient._next_version_number(
                        self, db, workflow.id
                    ),
                )
                db.add(draft)
            draft.workflow_json = definition.workflow_json
            draft.workflow_configurations = definition.workflow_configurations
            draft.template_context_variables = definition.template_context_variables
            workflow.name = candidate.name
            workflow.workflow_definition = draft.workflow_json
            workflow.workflow_configurations = draft.workflow_configurations
            workflow.template_context_variables = draft.template_context_variables
            await db.flush()
            await self._wb_event(
                db,
                session,
                "candidate.adopted",
                {"candidate_id": candidate.id, "definition_id": draft.id},
            )
            return {
                "workflow_id": workflow.id,
                "definition_id": draft.id,
                "unchanged": False,
            }
