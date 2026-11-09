"""Fenced workbench checkpoints and authenticated, revision-bound decisions.

Stored in the existing versioned job output so old jobs remain readable. Private
working state is excluded from both ORM and compact HTTP projections.
"""

import copy
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import UserModel, WorkbenchJobModel
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint


class WorkbenchOrchestrationClient(BaseDBClient):
    async def wb_orchestration(self, org, sid, jid):
        async with self.async_session() as db:
            await self._wb_session(db, org, sid)
            job = await self._wb_item(db, "jobs", sid, jid)
            state = copy.deepcopy(job.output.get("_orchestration", {"version": 1}))
            if state.get("version") != 1:
                raise WorkbenchError("Unsupported workbench checkpoint version", 409)
            return state

    async def wb_checkpoint(
        self, org, sid, jid, token, key, value, *, state=None, namespace="authoring"
    ):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, sid, lock=True)
            job = await self._wb_fence(db, sid, jid, token)
            saved = copy.deepcopy(job.output.get("_orchestration", {"version": 1}))
            saved.setdefault("steps", {})[key] = copy.deepcopy(value)
            if state is not None:
                saved.setdefault("working_states", {})[namespace] = copy.deepcopy(state)
            job.output = {
                **job.output,
                "_orchestration": saved,
                "orchestration": {"version": 1, "last_step": key},
            }
            await self._wb_event(
                db, session, "checkpoint.saved", {"job_id": jid, "step": key}
            )
            return saved

    async def wb_request_approval(self, org, sid, jid, token, kind, proposal):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, sid, lock=True)
            job = await self._wb_fence(db, sid, jid, token)
            saved = copy.deepcopy(job.output.get("_orchestration", {"version": 1}))
            revision = fingerprint({"kind": kind, "proposal": proposal})
            old = saved.get("approvals", {}).get(revision)
            if old:
                return old
            pending = job.output.get("pending_approval")
            if pending and pending["status"] == "pending":
                raise WorkbenchError("Resolve the pending approval first", 409)
            _, _, target = await self._wb_target(db, session.workflow_id, org)
            approval = {
                "id": str(uuid.uuid4()),
                "sequence": len(saved.get("approvals", {})) + 1,
                "revision": revision,
                "kind": kind,
                "proposal": copy.deepcopy(proposal),
                "status": "pending",
                "target_fingerprint": fingerprint(target),
                "memory_revision": await self._wb_memory_revision(db, session),
            }
            saved.setdefault("approvals", {})[revision] = approval
            job.output = {
                **job.output,
                "_orchestration": saved,
                "pending_approval": approval,
            }
            await self._wb_event(
                db, session, "approval.requested", {"job_id": jid, "approval": approval}
            )
            return approval

    async def wb_decide(self, org, sid, jid, user_id, decision):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, sid, lock=True)
            job = await self._wb_item(db, "jobs", sid, jid)
            saved = copy.deepcopy(job.output.get("_orchestration", {}))
            approval = saved.get("approvals", {}).get(decision.revision)
            if not approval or approval["id"] != decision.approval_id:
                raise WorkbenchError(
                    "Approval changed; reload the latest proposal", 409
                )
            expected = {"decision": decision.decision, "feedback": decision.feedback}
            if approval["status"] != "pending":
                if approval.get("response") == expected:
                    return {"job_id": jid, "status": job.status}
                raise WorkbenchError(
                    "This proposal already has a different decision", 409
                )
            if job.status != "awaiting_input":
                raise WorkbenchError("Wait for this task to pause before deciding", 409)
            if approval["kind"] == "requirements" and decision.decision == "deny":
                raise WorkbenchError("Approve the requirements or request changes", 422)
            active = await db.scalar(
                select(WorkbenchJobModel.id).where(
                    WorkbenchJobModel.session_id == sid,
                    WorkbenchJobModel.id != jid,
                    WorkbenchJobModel.status.in_(["queued", "running"]),
                )
            )
            if active:
                raise WorkbenchError("Wait for the current task or stop it first", 409)
            _, _, target = await self._wb_target(
                db, session.workflow_id, org, lock=True
            )
            if (
                fingerprint(target) != approval["target_fingerprint"]
                or await self._wb_memory_revision(db, session)
                != approval["memory_revision"]
            ):
                raise WorkbenchError(
                    "The draft or confirmed requirements changed; start a new task", 409
                )
            actor = await db.get(UserModel, user_id)
            if actor is None:
                raise WorkbenchError("Approving user not found", 404)
            approval.update(
                status="approved" if decision.decision == "approve" else "rejected",
                response=expected,
                decided_by=user_id,
                decided_provider_id=actor.provider_id,
                decided_at=datetime.now(UTC).isoformat(),
            )
            # Only an authenticated human decision promotes a summary to evidence.
            # user_request sources survive candidate ancestry into later tasks.
            confirmed_brief = (
                approval["kind"] == "requirements" and decision.decision == "approve"
            )
            if (decision.feedback or confirmed_brief) and saved.get("version") == 1:
                basis = copy.deepcopy(job.output.get("_task_basis"))
                if basis:
                    basis["parent_fingerprint"] = basis.pop("fingerprint")
                    text = (
                        "用户已确认以下需求摘要；摘要补充完整原始资料，不删除未提及的要求。\n"
                        + json.dumps(approval["proposal"]["brief"], ensure_ascii=False)
                        if confirmed_brief
                        else decision.feedback
                    )
                    basis["sources"].append(
                        {
                            "id": "decision:" + approval["id"],
                            "kind": "user_request",
                            "title": "用户已确认的需求"
                            if confirmed_brief
                            else "用户修订意见",
                            "text": text,
                            "source_id": approval["id"],
                            "session_id": sid,
                            "created_at": approval["decided_at"],
                            "available": True,
                            "characters": len(text),
                            "content_hash": fingerprint(text),
                        }
                    )
                    basis["fingerprint"] = fingerprint(basis)
                    from api.services.agent_workbench.basis import summary

                    job.output = {
                        **job.output,
                        "_task_basis": basis,
                        "task_basis": summary(basis),
                    }
            job.output = {
                **job.output,
                "_orchestration": saved,
                "pending_approval": approval,
            }
            job.status, job.error = "queued", None
            await self._wb_event(
                db,
                session,
                "approval.decided",
                {
                    "job_id": jid,
                    "approval": approval,
                    "role": "user",
                    "text": (
                        "已确认需求，继续构建。"
                        if approval["kind"] == "requirements"
                        else "已批准工具变更。"
                    )
                    if decision.decision == "approve"
                    else "已退回提案。" + decision.feedback,
                },
            )
            return {"job_id": jid, "status": job.status}

    async def wb_resume_orchestration(self, org, sid, jid):
        async with self.async_session() as db, db.begin():
            session = await self._wb_session(db, org, sid, lock=True)
            job = await self._wb_item(db, "jobs", sid, jid)
            if job.status == "queued":
                return {"job_id": jid, "status": job.status}
            if job.status not in {"failed", "cancelled"} or not job.output.get(
                "_orchestration"
            ):
                raise WorkbenchError(
                    "This task has no resumable orchestration checkpoint", 409
                )
            if await db.scalar(
                select(WorkbenchJobModel.id).where(
                    WorkbenchJobModel.session_id == sid,
                    WorkbenchJobModel.status.in_(["queued", "running"]),
                )
            ):
                raise WorkbenchError("Wait for the current task or stop it first", 409)
            if (job.output.get("pending_approval") or {}).get("status") == "pending":
                job.status = "awaiting_input"
            else:
                job.status = "queued"
            job.error = None
            job.output = {**job.output, "recovery": {"attempt": 0, "retry_at": None}}
            await self._wb_event(
                db, session, "checkpoint.resumed", {"job_id": jid, "status": job.status}
            )
            return {"job_id": jid, "status": job.status}
