"""Agent-scoped memory. Only authenticated human routes may confirm or replace it.

User requests and uploaded text remain source evidence, never synthesized facts.
There is deliberately no model-facing write method/tool.
"""

import hashlib

from sqlalchemy import func, select

from api.db.agent_workbench_client import record
from api.db.base_client import BaseDBClient
from api.db.models import (
    WorkbenchAssetModel,
    WorkbenchCandidateModel,
    WorkbenchJobModel,
    WorkbenchMemoryModel,
    WorkbenchSessionModel,
)
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint


def memory_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class WorkbenchMemoryClient(BaseDBClient):
    async def wb_basis_origins(self, org, session_id, job_id, target_fingerprint):
        """Recover human inputs along the tested version's ancestry, not chat recency.

        An imported/legacy graph with no saved ancestry has no inferred authority.
        Confirmed memories are loaded separately at the task's own revision.
        """
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            current = await self._wb_item(db, "jobs", session_id, job_id)
            rows = list(
                await db.scalars(
                    select(WorkbenchCandidateModel)
                    .join(
                        WorkbenchSessionModel,
                        WorkbenchCandidateModel.session_id == WorkbenchSessionModel.id,
                    )
                    .where(
                        WorkbenchSessionModel.organization_id == org,
                        WorkbenchSessionModel.workflow_id == session.workflow_id,
                        WorkbenchCandidateModel.created_at <= current.created_at,
                    )
                    .order_by(WorkbenchCandidateModel.created_at.desc())
                )
            )
            pending = [
                next((c for c in rows if c.fingerprint == target_fingerprint), None)
            ]
            sources, visited, jobs = {}, set(), set()
            while pending:
                candidate = pending.pop()
                if candidate is None or candidate.id in visited:
                    continue
                visited.add(candidate.id)
                origin = await self._wb_item(
                    db, "jobs", candidate.session_id, candidate.job_id
                )
                if origin.id not in jobs:
                    jobs.add(origin.id)
                    inherited = origin.output.get("_task_basis", {})
                    for item in inherited.get("sources", []):
                        if item["kind"] not in {"memory", "current_request"}:
                            sources[item["id"]] = item
                    # The current request of an earlier build becomes historical
                    # source evidence, never the new task's execution instruction.
                    if origin.output.get("intent") != "test" and origin.output.get(
                        "outcome"
                    ) not in {"analysis", "test", "review"}:
                        text = origin.input.get("text") or origin.input.get(
                            "instruction", ""
                        )
                        if text:
                            key = "request:" + origin.id
                            sources[key] = {
                                "id": key,
                                "kind": "user_request",
                                "title": "版本来源中的用户要求",
                                "text": text,
                                "session_id": origin.session_id,
                                "source_id": origin.id,
                                "created_at": origin.created_at.isoformat(),
                            }
                        for aid in origin.input.get("asset_ids", []):
                            asset = await self._wb_item(
                                db, "assets", origin.session_id, aid
                            )
                            key = "attachment:" + aid
                            sources[key] = {
                                "id": key,
                                "kind": "material",
                                "title": asset.name,
                                "session_id": origin.session_id,
                                "source_id": aid,
                                "created_at": asset.created_at.isoformat(),
                                "sha256": asset.sha256,
                            }
                parent_id = origin.input.get("candidate_id")
                parent = next(
                    (
                        c
                        for c in rows
                        if c.id == parent_id and c.created_at < candidate.created_at
                    ),
                    None,
                )
                if parent is None:
                    parent = next(
                        (
                            c
                            for c in rows
                            if c.id != candidate.id
                            and c.created_at < candidate.created_at
                            and c.fingerprint == candidate.base_fingerprint
                        ),
                        None,
                    )
                pending.append(parent)
            return sorted(
                sources.values(), key=lambda s: (s.get("created_at", ""), s["id"])
            )

    async def wb_task_basis(self, org, session_id, job_id):
        async with self.async_session() as db:
            await self._wb_session(db, org, session_id)
            job = await self._wb_item(db, "jobs", session_id, job_id)
            return job.output.get("_task_basis")

    async def wb_freeze_basis(self, org, session_id, job_id, token, basis, summary):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            job = await self._wb_fence(db, session_id, job_id, token)
            if job.output.get("_task_basis"):
                if job.output["_task_basis"]["fingerprint"] != basis["fingerprint"]:
                    raise WorkbenchError(
                        "本任务依据已冻结，请用新任务评估变更后的资料。", 409
                    )
                return job.output["_task_basis"]
            if job.output.get("memory_revision") != await self._wb_memory_revision(
                db, session
            ):
                raise WorkbenchError(
                    "Agent 需求在任务开始后发生变化，请重新发起任务。", 409
                )
            if basis["memory_revision"] != job.output.get("memory_revision"):
                raise WorkbenchError("已确认约束发生变化，不能沿用旧复核依据。", 409)
            job.output = {**job.output, "_task_basis": basis, "task_basis": summary}
            await self._wb_event(
                db, session, "basis.frozen", {"job_id": job_id, **summary}
            )
            return basis

    async def _wb_memories(self, db, session):
        return list(
            await db.scalars(
                select(WorkbenchMemoryModel)
                .where(
                    WorkbenchMemoryModel.organization_id == session.organization_id,
                    WorkbenchMemoryModel.workflow_id == session.workflow_id,
                )
                .order_by(WorkbenchMemoryModel.created_at, WorkbenchMemoryModel.id)
            )
        )

    async def _wb_memory_revision(self, db, session):
        rows = await db.execute(
            select(
                WorkbenchMemoryModel.id,
                WorkbenchMemoryModel.status,
                WorkbenchMemoryModel.content_hash,
            )
            .where(
                WorkbenchMemoryModel.organization_id == session.organization_id,
                WorkbenchMemoryModel.workflow_id == session.workflow_id,
                WorkbenchMemoryModel.status == "confirmed",
            )
            .order_by(WorkbenchMemoryModel.created_at, WorkbenchMemoryModel.id)
        )
        return fingerprint([list(row) for row in rows])

    async def wb_memory(self, org, session_id, *, include_history=False):
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            rows = await self._wb_memories(db, session)
            active = [r for r in rows if r.status == "confirmed"]
            return {
                "workflow_id": session.workflow_id,
                "revision": fingerprint(
                    [[r.id, r.status, r.content_hash] for r in active]
                ),
                "entries": [
                    record(r)
                    for r in rows
                    if include_history or r.status == "confirmed"
                ],
            }

    def _wb_sources_query(self, session, source_type):
        model = (
            WorkbenchAssetModel if source_type == "attachment" else WorkbenchJobModel
        )
        query = (
            select(model)
            .join(WorkbenchSessionModel, model.session_id == WorkbenchSessionModel.id)
            .where(
                WorkbenchSessionModel.organization_id == session.organization_id,
                WorkbenchSessionModel.workflow_id == session.workflow_id,
            )
        )
        if source_type == "attachment":
            query = query.where(model.status == "ready")
        else:
            query = query.where(model.kind.in_(["build", "tune"]))
        return model, query

    @staticmethod
    def _wb_source_record(row, source_type):
        if source_type == "attachment":
            content = row.parsed.get("text", "")
            title = row.name
        else:
            content = row.input.get("text") or row.input.get("instruction", "")
            title = content[:120]
        return {
            "id": row.id,
            "source_type": source_type,
            "session_id": row.session_id,
            "title": title,
            "content": content,
            "content_hash": memory_hash(content),
            "created_at": row.created_at.isoformat(),
            "authority": "user_original_not_confirmed_fact",
            "truncated_by_parser": bool(row.parsed.get("truncated"))
            if source_type == "attachment"
            else False,
        }

    async def wb_memory_sources(
        self, org, session_id, *, source_type="request", query="", offset=0, limit=40
    ):
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            model, stmt = self._wb_sources_query(session, source_type)
            if query:
                field = (
                    model.parsed["text"].as_string()
                    if source_type == "attachment"
                    else func.coalesce(
                        model.input["text"].as_string(),
                        model.input["instruction"].as_string(),
                    )
                )
                stmt = stmt.where(field.icontains(query, autoescape=True))
            total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
            rows = await db.scalars(
                stmt.order_by(model.created_at.desc(), model.id.desc())
                .offset(offset)
                .limit(limit)
            )
            items = []
            for row in rows:
                item = self._wb_source_record(row, source_type)
                item["characters"] = len(item.pop("content"))
                items.append(item)
            return {
                "items": items,
                "total": total,
                "next_offset": offset + len(items)
                if offset + len(items) < total
                else None,
            }

    async def _wb_memory_source(self, db, session, source_type, source_id):
        model, stmt = self._wb_sources_query(session, source_type)
        row = await db.scalar(stmt.where(model.id == source_id))
        if row is None:
            raise WorkbenchError("Memory source not found for this Agent", 404)
        return self._wb_source_record(row, source_type)

    async def wb_memory_source(self, org, session_id, source_type, source_id):
        async with self.async_session() as db:
            session = await self._wb_session(db, org, session_id)
            return await self._wb_memory_source(db, session, source_type, source_id)

    async def wb_confirm_memory(
        self,
        org,
        session_id,
        user_id,
        *,
        kind,
        content,
        expected_revision,
        replaces_id=None,
        source_type="request",
        source_id=None,
        source_quote=None,
    ):
        if kind not in {"requirement", "constraint", "sop"} or not content.strip():
            raise WorkbenchError("Memory requires a kind and nonempty original content")
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            await self._wb_target(db, session.workflow_id, org, lock=True)
            revision = await self._wb_memory_revision(db, session)
            if expected_revision != revision:
                raise WorkbenchError(
                    "长期记忆已更新，请刷新后确认，避免覆盖其他会话的修改。", 409
                )
            source = {"type": "human_authored", "user_id": user_id}
            if source_id:
                original = await self._wb_memory_source(
                    db, session, source_type, source_id
                )
                if not source_quote or source_quote not in original["content"]:
                    raise WorkbenchError(
                        "引用必须来自该 Agent 的用户原文，不能引用模型推测。"
                    )
                source = {
                    "type": source_type,
                    "id": source_id,
                    "session_id": original["session_id"],
                    "quote": source_quote,
                    "content_hash": original["content_hash"],
                }
            if replaces_id:
                old = next(
                    (
                        r
                        for r in await self._wb_memories(db, session)
                        if r.id == replaces_id and r.status == "confirmed"
                    ),
                    None,
                )
                if old is None:
                    raise WorkbenchError("Active memory to replace not found", 404)
                old.status = "superseded"
            row = WorkbenchMemoryModel(
                organization_id=org,
                workflow_id=session.workflow_id,
                session_id=session_id,
                confirmed_by=user_id,
                kind=kind,
                content=content,
                content_hash=memory_hash(content),
                source=source,
                replaces_id=replaces_id,
                status="confirmed",
            )
            db.add(row)
            await db.flush()
            await self._wb_event(
                db,
                session,
                "memory.confirmed",
                {
                    "memory_id": row.id,
                    "replaces_id": replaces_id,
                    "confirmed_by": user_id,
                },
            )
            return record(row)

    async def wb_revoke_memory(
        self, org, session_id, user_id, memory_id, expected_revision
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, session_id, lock=True)
            await self._wb_target(db, session.workflow_id, org, lock=True)
            if expected_revision != await self._wb_memory_revision(db, session):
                raise WorkbenchError("长期记忆已更新，请刷新后再操作。", 409)
            row = next(
                (
                    r
                    for r in await self._wb_memories(db, session)
                    if r.id == memory_id and r.status == "confirmed"
                ),
                None,
            )
            if row is None:
                raise WorkbenchError("Active memory not found", 404)
            row.status = "revoked"
            await self._wb_event(
                db, session, "memory.revoked", {"memory_id": row.id, "user_id": user_id}
            )
            return {"id": row.id, "status": row.status}
