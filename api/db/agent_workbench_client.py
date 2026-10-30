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
    WorkflowRunTextSessionModel,
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
    result = {
        column.name: (
            value.isoformat()
            if isinstance(value := getattr(row, column.name), datetime)
            else copy.deepcopy(value)
        )
        for column in row.__table__.columns
    }
    if isinstance(row, WorkbenchJobModel):
        result["output"].pop("_run_evidence", None)
        result["output"].pop("_requested_target", None)
        result["output"].pop("_task_basis", None)
        result["output"].pop("_submitted_input", None)
    return result


def evaluation_index(row):
    item = record(row)
    item["summary_only"] = True
    item["results"] = [
        {
            **{
                k: c.get(k)
                for k in (
                    "name",
                    "status",
                    "run_id",
                    "source",
                    "error",
                    "review_pending",
                )
            },
            "transcript": [],
            "assertions": [],
            "tool_calls": [],
        }
        for c in item.get("results", [])
    ]
    return item


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
        self,
        org,
        user_id,
        workflow_id=None,
        name="New Agent",
        model_protocol="chat",
        new_session=False,
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
                if existing and not new_session:
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

    async def wb_sessions(self, org, user_id, workflow_id):
        async with self.async_session() as db:
            await self._wb_target(db, workflow_id, org)
            # Only fetch a status, not each session's potentially large job history.
            active_status = (
                select(WorkbenchJobModel.status)
                .where(
                    WorkbenchJobModel.session_id == WorkbenchSessionModel.id,
                    WorkbenchJobModel.status.in_(["queued", "running"]),
                )
                .order_by(
                    WorkbenchJobModel.created_at.desc(), WorkbenchJobModel.id.desc()
                )
                .limit(1)
                .correlate(WorkbenchSessionModel)
                .scalar_subquery()
            )
            rows = (
                await db.execute(
                    select(
                        WorkbenchSessionModel, active_status.label("active_job_status")
                    )
                    .where(
                        WorkbenchSessionModel.organization_id == org,
                        WorkbenchSessionModel.user_id == user_id,
                        WorkbenchSessionModel.workflow_id == workflow_id,
                    )
                    .order_by(WorkbenchSessionModel.created_at.desc())
                )
            ).all()
            return [
                {**record(row), "active_job_status": status} for row, status in rows
            ]

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

    async def wb_context(
        self, org, session_id, *, if_revision=None, compact=False, session_only=False
    ):
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            if session_only:
                # Evaluation needs routing metadata, not every historical suite
                # and transcript. Keep this read scoped without loading archives.
                return {"session": record(session)}
            _, _, target = await self._wb_target(db, session.workflow_id, org)
            memory_revision = await self._wb_memory_revision(db, session)
            revision = fingerprint(
                {
                    "session": record(session),
                    "target": target,
                    "memory_revision": memory_revision,
                }
            )
            if if_revision == revision:
                return {"unchanged": True, "revision": revision}
            result = {
                "revision": revision,
                "session": record(session),
                "target": target,
                "target_fingerprint": fingerprint(target),
                "memory_revision": memory_revision,
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
                result[name] = [
                    evaluation_index(row)
                    if compact and name == "evaluations"
                    else record(row)
                    for row in rows
                ]
                if compact and name == "suites":
                    for item in result[name]:
                        item["summary_only"] = True
                        item["payload"]["cases"] = [
                            {"name": c["name"]} for c in item["payload"]["cases"]
                        ]
            return result

    async def wb_test_result_page(self, org, session_id, job_id, *, offset=0, limit=20):
        if offset < 0 or not 1 <= limit <= 50:
            raise WorkbenchError("Invalid test result page", 422)
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            await self._wb_item(db, "jobs", session_id, job_id)
            rows = (
                await db.scalars(
                    select(WorkbenchEvaluationModel)
                    .where(
                        WorkbenchEvaluationModel.session_id == session_id,
                        WorkbenchEvaluationModel.job_id == job_id,
                    )
                    .order_by(
                        WorkbenchEvaluationModel.created_at, WorkbenchEvaluationModel.id
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "evaluations": [evaluation_index(r) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
            }

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

    async def wb_requested_target(self, org, session_id, job_id):
        """Original build input stays private and immutable across resumptions."""
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            job = await self._wb_item(db, "jobs", session_id, job_id)
            target = job.output.get("_requested_target")
            if target is None:
                raise WorkbenchError("原构建缺少冻结输入，不能安全恢复。", 409)
            return copy.deepcopy(target)

    async def wb_resume_test_request(
        self, org, session_id, job_id, token, source_job_id, *, classified_test=False
    ):
        """Atomically restore a scoped command and snapshot; never touch the draft."""
        from api.services.agent_workbench.recovery import continued_test_input

        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            prior = await self._wb_item(db, "jobs", session_id, source_job_id)
            if prior.created_at >= job.created_at or job.kind != "build":
                raise WorkbenchError("只能接续当前会话中更早的测试任务。", 409)
            if job.output.get("test_source_job_id") == source_job_id:
                return record(job)
            if job.output.get("test_source_job_id"):
                if source_job_id in job.output.get("test_chain_job_ids", []):
                    return record(job)
                raise WorkbenchError("测试恢复来源已固定，不能切换。", 409)
            # The model or an old clarification button may reference an ancestor.
            # Resolve its latest scoped state under the session lock; retain all
            # compatible checkpoints, including legacy sibling continuations.
            from api.services.agent_workbench.test_lineage import test_chain

            rows = (
                await db.scalars(
                    select(WorkbenchJobModel)
                    .where(
                        WorkbenchJobModel.session_id == session_id,
                        WorkbenchJobModel.created_at < job.created_at,
                        WorkbenchJobModel.kind == "build",
                    )
                    .order_by(
                        WorkbenchJobModel.created_at.desc(), WorkbenchJobModel.id.desc()
                    )
                )
            ).all()
            chain = test_chain([record(row) for row in rows], source_job_id)
            if chain:
                prior = next(row for row in rows if row.id == chain[0]["id"])
                source_job_id = prior.id
            restored = continued_test_input(
                record(prior), job.input, classified_test=classified_test
            )
            if any(r["output"].get("preparation_receipts") for r in chain):
                restored["resume_test_job_id"] = source_job_id
            target = prior.output.get("_requested_target")
            if target is None:
                raise WorkbenchError("原测试缺少冻结版本，不能安全恢复。", 409)
            submitted = copy.deepcopy(job.input)
            job.input = restored
            job.output = {
                **job.output,
                "intent": "test",
                "_submitted_input": submitted,
                "test_source_job_id": source_job_id,
                "test_chain_job_ids": [r["id"] for r in chain],
                "_requested_target": copy.deepcopy(target),
                "requested_target_fingerprint": fingerprint(target),
                "test_target_fingerprint": fingerprint(target),
                **{
                    key: copy.deepcopy(prior.output[key])
                    for key in (
                        "evaluation_model_configuration_snapshot",
                        "evaluation_model_configuration_fingerprint",
                    )
                    if key in prior.output
                },
            }
            await self._wb_event(
                db,
                session,
                "test.progress",
                {
                    "job_id": job_id,
                    "phase": "restored",
                    "source_job_id": source_job_id,
                    "text": "已恢复原测试要求、附件、执行方式和固定版本；继续测试，不修改 Agent。",
                },
            )
            return record(job)

    async def wb_set_test_operation(
        self, org, session_id, job_id, token, operation, reason
    ):
        """Freeze preparation/execution before importing or running test cases."""
        from api.services.agent_workbench.test_operations import operation_input

        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            if job.kind != "build" or job.input.get("mode") in {"analyze", "build"}:
                raise WorkbenchError("该任务不是测试准备或执行任务。", 409)
            existing = job.output.get("test_operation")
            if existing and existing != operation:
                raise WorkbenchError("本次测试操作范围已固定，不能在恢复时切换。", 409)
            if existing:
                return record(job)
            submitted = copy.deepcopy(job.output.get("_submitted_input", job.input))
            job.input = operation_input(job.input, operation)
            job.output = {
                **job.output,
                "_submitted_input": submitted,
                "intent": "test",
                "test_operation": operation,
                "test_operation_reason": reason,
            }
            await self._wb_event(
                db,
                session,
                "test.progress",
                {
                    "job_id": job_id,
                    "phase": "test_operation",
                    "operation": operation,
                    "text": "本次仅准备并保存测试集，不执行 Agent 对话。"
                    if operation == "prepare"
                    else "已确认本次执行测试；先完成场景准备，再运行 Agent 对话。",
                },
            )
            return record(job)

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

    async def wb_event_page(
        self, org, session_id, *, after=None, before=None, limit=60
    ):
        """Open at the tail; independently page backward and consume new events forward."""
        if (after is not None and before is not None) or not 1 <= limit <= 200:
            raise WorkbenchError("Use one cursor and a page size between 1 and 200")
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            query = select(WorkbenchEventModel).where(
                WorkbenchEventModel.session_id == session_id
            )
            forward = after is not None
            if forward:
                query = query.where(WorkbenchEventModel.sequence > after)
            elif before is not None:
                query = query.where(WorkbenchEventModel.sequence < before)
            order = WorkbenchEventModel.sequence
            rows = (
                await db.scalars(
                    query.order_by(order if forward else order.desc()).limit(limit + 1)
                )
            ).all()
            page = rows[:limit]
            if not forward:
                page.reverse()
            return {
                "events": [record(row) for row in page],
                "before": page[0].sequence
                if page and not forward and len(rows) > limit
                else None,
                "after": page[-1].sequence
                if page
                else (after if forward else session.event_sequence),
                "has_more": forward and len(rows) > limit,
            }

    async def wb_emit(self, org, session_id, kind, payload, *, job_id=None, token=None):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            if job_id:
                await self._wb_fence(db, session_id, job_id, token)
            await self._wb_event(db, session, kind, payload)

    async def wb_codex_binding(self, org, session_id):
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            return await db.scalar(
                select(WorkbenchEventModel.payload)
                .where(
                    WorkbenchEventModel.session_id == session_id,
                    WorkbenchEventModel.type == "codex.bound",
                )
                .order_by(WorkbenchEventModel.sequence.desc())
                .limit(1)
            )

    async def wb_conversation(self, org, session_id, *, before=None, limit=40):
        """Backward pagination; a task's enqueue sequence is its history boundary."""
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            query = select(WorkbenchEventModel).where(
                WorkbenchEventModel.session_id == session_id,
                WorkbenchEventModel.type.in_(
                    [
                        "task.queued",
                        "assistant.message",
                        "voice.transcript",
                        "task.completed",
                        "task.needs_attention",
                        "task.awaiting_input",
                        "task.failed",
                        "task.cancelled",
                        "candidate.adopted",
                        "evidence.read",
                    ]
                ),
            )
            if before is not None:
                query = query.where(WorkbenchEventModel.sequence < before)
            rows = (
                await db.scalars(
                    query.order_by(WorkbenchEventModel.sequence.desc()).limit(limit + 1)
                )
            ).all()
            page = rows[:limit]
            return {
                "events": [record(row) for row in reversed(page)],
                "before": page[-1].sequence if len(rows) > limit else None,
            }

    async def wb_run_evidence(self, org, session_id, job_id):
        """Internal evidence memory, scoped to this session and earlier tasks."""
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            current = await self._wb_item(db, "jobs", session_id, job_id)
            outputs = (
                await db.scalars(
                    select(WorkbenchJobModel.output)
                    .where(
                        WorkbenchJobModel.session_id == session_id,
                        WorkbenchJobModel.output["history_before"].as_integer()
                        < current.output["history_before"],
                    )
                    .order_by(
                        WorkbenchJobModel.output["history_before"].as_integer().desc()
                    )
                    .limit(20)
                )
            ).all()
            pages = {}
            for output in outputs:
                for key, page in output.get("_run_evidence", {}).items():
                    pages.setdefault(key, copy.deepcopy(page))
            return pages

    async def wb_remember_run_evidence(self, org, session_id, job_id, token, key, page):
        async with self.async_session() as db, db.begin():
            await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            pages = {**job.output.get("_run_evidence", {}), key: copy.deepcopy(page)}
            job.output = {**job.output, "_run_evidence": pages}

    async def wb_agent_runs(self, org, session_id, *, run_id=None, limit=20):
        """Agent Runs only; simulation runs and other agents are never exposed."""
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            query = (
                select(WorkflowRunModel)
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(
                    WorkflowModel.organization_id == org,
                    WorkflowRunModel.workflow_id == session.workflow_id,
                    WorkflowRunModel.purpose == "normal",
                )
            )
            if run_id is not None:
                query = query.where(WorkflowRunModel.id == run_id)
            rows = (
                await db.scalars(
                    query.order_by(
                        WorkflowRunModel.created_at.desc(), WorkflowRunModel.id.desc()
                    ).limit(limit)
                )
            ).all()
            return [record(row) for row in rows]

    async def wb_run_definition(self, org, session_id, run_id):
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            definition = await db.scalar(
                select(WorkflowDefinitionModel)
                .join(
                    WorkflowRunModel,
                    WorkflowRunModel.definition_id == WorkflowDefinitionModel.id,
                )
                .join(WorkflowModel, WorkflowModel.id == WorkflowRunModel.workflow_id)
                .where(
                    WorkflowModel.organization_id == org,
                    WorkflowRunModel.workflow_id == session.workflow_id,
                    WorkflowDefinitionModel.workflow_id == session.workflow_id,
                    WorkflowRunModel.id == run_id,
                    WorkflowRunModel.purpose == "normal",
                )
            )
            return record(definition) if definition else None

    async def wb_diagnostic_run(self, org, session_id, run_id):
        """Read frozen runtime evidence across this Agent's sessions, never tenants/agents."""
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            run = await db.scalar(
                select(WorkflowRunModel)
                .join(WorkflowModel)
                .where(
                    WorkflowRunModel.id == run_id,
                    WorkflowRunModel.workflow_id == session.workflow_id,
                    WorkflowModel.organization_id == org,
                )
            )
            if run is None:
                raise WorkbenchError("Diagnostic run not found for this Agent", 404)
            definition = await db.scalar(
                select(WorkflowDefinitionModel).where(
                    WorkflowDefinitionModel.id == run.definition_id,
                    WorkflowDefinitionModel.workflow_id == session.workflow_id,
                )
            )
            case, source = None, None
            if run.purpose == "evaluation":
                # Run ownership has already been checked; also scope its source records.
                evaluations = (
                    await db.scalars(
                        select(WorkbenchEvaluationModel)
                        .join(WorkbenchSessionModel)
                        .where(
                            WorkbenchSessionModel.organization_id == org,
                            WorkbenchSessionModel.workflow_id == session.workflow_id,
                            WorkbenchEvaluationModel.job_id
                            == (run.extra or {}).get("workbench_job_id"),
                        )
                        .order_by(WorkbenchEvaluationModel.created_at)
                    )
                ).all()
                for evaluation in evaluations:
                    for index, result in enumerate(evaluation.results or []):
                        if result.get("run_id") == run_id:
                            case = copy.deepcopy(result)
                            source = {
                                "evaluation_id": evaluation.id,
                                "case_index": index,
                                "session_id": evaluation.session_id,
                            }
                            break
                    if case is not None:
                        break
            else:
                text_session = await db.scalar(
                    select(WorkflowRunTextSessionModel).where(
                        WorkflowRunTextSessionModel.workflow_run_id == run_id,
                    )
                )
                if text_session:
                    transcript, events = [], []
                    for index, turn in enumerate(
                        (text_session.session_data or {}).get("turns", [])
                    ):
                        for role, key in (
                            ("user", "user_message"),
                            ("assistant", "assistant_message"),
                        ):
                            if turn.get(key):
                                transcript.append(
                                    {
                                        "role": role,
                                        "turn": index,
                                        "text": turn[key].get("text", ""),
                                    }
                                )
                        events.extend(
                            {**e, "turn": index} for e in turn.get("events", [])
                        )
                    case = {"transcript": transcript, "events": events}
            return {
                "run": record(run),
                "definition": record(definition) if definition else None,
                "case": case,
                "source": source,
            }

    async def wb_diagnostic_probes(self, org, session_id, run_ids):
        """Reuse completed, attributable experiments after a task interruption."""
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            owned = set(
                (
                    await db.scalars(
                        select(WorkflowRunModel.id).where(
                            WorkflowRunModel.id.in_(run_ids),
                            WorkflowRunModel.workflow_id == session.workflow_id,
                        )
                    )
                ).all()
            )
            if owned != set(run_ids):
                raise WorkbenchError("Diagnostic runs not found for this Agent", 404)
            rows = (
                await db.scalars(
                    select(WorkbenchEventModel)
                    .where(
                        WorkbenchEventModel.session_id == session_id,
                        WorkbenchEventModel.type == "diagnosis.probe.completed",
                        WorkbenchEventModel.payload["run_id"].as_integer().in_(run_ids),
                    )
                    .order_by(WorkbenchEventModel.sequence.desc())
                    .limit(12)
                )
            ).all()
            return [copy.deepcopy(row.payload) for row in reversed(rows)]

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
                if (
                    old.kind != kind
                    or old.output.get("_submitted_input", old.input) != payload
                ):
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
            target_session_id = payload.get("test_target_session_id") or session_id
            if target_session_id != session_id:
                if (
                    kind != "build"
                    or payload.get("mode") != "test"
                    or not payload.get("candidate_id")
                ):
                    raise WorkbenchError("跨会话版本只能用于只读测试。", 422)
                source_session = await self._wb_session(db, org, target_session_id)
                if source_session.workflow_id != session.workflow_id:
                    raise WorkbenchError("测试版本必须属于同一个 Agent。", 409)
            for field, table in (
                ("candidate_id", "candidates"),
                ("suite_id", "suites"),
            ):
                if payload.get(field):
                    await self._wb_item(
                        db,
                        table,
                        target_session_id if field == "candidate_id" else session_id,
                        payload[field],
                    )
            _, _, target = await self._wb_target(db, session.workflow_id, org)
            if payload.get("candidate_id"):
                candidate = await self._wb_item(
                    db, "candidates", target_session_id, payload["candidate_id"]
                )
                definition = await db.get(
                    WorkflowDefinitionModel, candidate.definition_id
                )
                target = {
                    "name": candidate.name,
                    "workflow_json": definition.workflow_json,
                    "workflow_configurations": definition.workflow_configurations,
                    "template_context_variables": definition.template_context_variables,
                }
            row = WorkbenchJobModel(
                session_id=session_id,
                kind=kind,
                input=payload,
                idempotency_key=key,
                output={
                    "history_before": session.event_sequence + 1,
                    "memory_revision": await self._wb_memory_revision(db, session),
                    "_requested_target": copy.deepcopy(target),
                    "requested_target_fingerprint": fingerprint(target),
                    "model_protocol": session.model_protocol,
                },
            )
            if payload.get("resume_build_job_id"):
                from api.services.agent_workbench.recovery import resumable_build

                prior = await self._wb_item(
                    db, "jobs", session_id, payload["resume_build_job_id"]
                )
                checkpoint = resumable_build(record(prior))
                if kind != "build" or payload != {
                    **prior.input,
                    "resume_build_job_id": prior.id,
                }:
                    raise WorkbenchError("恢复任务必须保持原始要求和测试配置。", 409)
                await self._wb_item(
                    db, "candidates", session_id, checkpoint["built"]["candidate_id"]
                )
                for sid in {
                    checkpoint["built"]["suite_id"],
                    *checkpoint["built"].get("regression_suite_ids", []),
                }:
                    await self._wb_item(db, "suites", session_id, sid)
                # Copy private frozen source/basis as well as public receipts;
                # neither the original job nor its historical evidence is mutated.
                row.output = {
                    **copy.deepcopy(prior.output),
                    "build_checkpoint": checkpoint,
                    "resume_source_job_id": prior.id,
                    "model_protocol": prior.output.get(
                        "model_protocol", session.model_protocol
                    ),
                }
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
            row.results = [
                {
                    **result,
                    "status": "error",
                    "error": "测试任务已停止，已执行的对话保留；本条尚未完成。",
                    "validity": {
                        **result.get("validity", {}),
                        "status": "inconclusive",
                        "stop_reason": "interrupted",
                    },
                }
                if result.get("status") == "running"
                else result
                for result in row.results
            ]

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
                await self._wb_stop_evaluations(
                    db,
                    session_id,
                    job_id,
                    "cancelled"
                    if status in {"needs_attention", "awaiting_input"}
                    else status,
                )
            await self._wb_event(
                db,
                session,
                "task." + status,
                {"job_id": job.id, "output": record(job)["output"], "error": error},
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
            job = await self._wb_fence(db, session_id, job_id, token)
            if job.input.get("mode") == "test" or job.output.get("intent") == "test":
                raise WorkbenchError(
                    "只运行测试的任务不能修改 Agent 或生成候选版本。", 409
                )
            await self._wb_target(db, session.workflow_id, org, lock=True)
            if job.output.get("memory_revision") != await self._wb_memory_revision(
                db, session
            ):
                raise WorkbenchError("长期要求已变更，请基于最新记忆重新构建。", 409)
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

    async def wb_test_target(self, org, session_id, job_id, token):
        """Freeze an existing version for replay; never mutate the workflow/draft."""
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            target = job.output.get("_requested_target")
            if not target:
                _, _, target = await self._wb_target(db, session.workflow_id, org)
            digest = fingerprint(target)
            candidate = await db.scalar(
                select(WorkbenchCandidateModel)
                .where(
                    WorkbenchCandidateModel.session_id == session_id,
                    WorkbenchCandidateModel.fingerprint == digest,
                )
                .order_by(WorkbenchCandidateModel.created_at.desc())
                .limit(1)
            )
            if candidate is None:
                definition = WorkflowDefinitionModel(
                    workflow_id=session.workflow_id,
                    status="candidate",
                    is_current=False,
                    workflow_json=target["workflow_json"],
                    workflow_configurations=target["workflow_configurations"],
                    template_context_variables=target["template_context_variables"],
                )
                db.add(definition)
                await db.flush()
                candidate = WorkbenchCandidateModel(
                    session_id=session_id,
                    job_id=job_id,
                    definition_id=definition.id,
                    code="// Immutable test snapshot; no generated changes",
                    name=target["name"],
                    summary="当前版本的只读测试快照（未修改 Agent）",
                    fingerprint=digest,
                    base_fingerprint=digest,
                )
                db.add(candidate)
                await db.flush()
            job.output = {
                **job.output,
                "intent": "test",
                "test_candidate_id": candidate.id,
                "test_target_fingerprint": digest,
            }
            await self._wb_event(
                db,
                session,
                "test.progress",
                {
                    "job_id": job_id,
                    "phase": "target",
                    "text": "已固定原测试版本，只复核已有对话，不修改 Agent。"
                    if job.input.get("review_evaluation_ids")
                    else "已固定测试版本，仅准备测试集，不执行 Agent 对话。"
                    if job.input.get("prepare_only")
                    else "已固定测试版本，只执行测试，不修改 Agent。",
                    "candidate_id": candidate.id,
                },
            )
            return {**record(candidate), "snapshot": copy.deepcopy(target)}

    async def wb_job_progress(self, org, session_id, job_id, token, output, progress):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            job.output = {**job.output, **copy.deepcopy(output)}
            await self._wb_event(
                db, session, "test.progress", {"job_id": job_id, **progress}
            )

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

    async def wb_pin_models(
        self, org, session_id, job_id, token, snapshot, *, evaluation=False
    ):
        async with self.async_session() as db, db.begin():
            await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            prefix = "evaluation_" if evaluation else ""
            if job.output.get(prefix + "model_configuration_snapshot"):
                if job.output[prefix + "model_configuration_snapshot"] == snapshot:
                    return copy.deepcopy(job.output)
                raise WorkbenchError("Task model configuration is already frozen", 409)
            job.output = {
                **job.output,
                prefix + "model_configuration_snapshot": copy.deepcopy(snapshot),
                prefix + "model_configuration_fingerprint": fingerprint(snapshot),
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
                        "evaluation_model_configuration_fingerprint"
                    )
                    or job.output.get("model_configuration_fingerprint")
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
            snapshot = job.output.get(
                "evaluation_model_configuration_snapshot"
            ) or job.output.get("model_configuration_snapshot")
            if not isinstance(snapshot, dict) or snapshot.get("version") != 3:
                raise WorkbenchError(
                    "Freeze the task model configuration before evaluation", 409
                )
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
                        "evaluation_model_configuration_fingerprint"
                    )
                    or job.output.get("model_configuration_fingerprint"),
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
            job = await self._wb_item(db, "jobs", session_id, candidate.job_id)
            memory_revision = await self._wb_memory_revision(db, session)
            if job.output.get("memory_revision", fingerprint([])) != memory_revision:
                raise WorkbenchError(
                    "候选版本使用的长期要求已变更，请重新构建后再应用。", 409
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
