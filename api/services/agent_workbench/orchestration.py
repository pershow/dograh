"""Reusable durable steps, editing checkpoints and human handoffs."""

import asyncio
import copy
import json
from types import SimpleNamespace

from api.db import db_client
from api.schemas.tool import CreateToolRequest, UpdateToolRequest
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.workflow.authoring.contracts import CallBrief


class ApprovalRequired(WorkbenchError):
    def __init__(self, approval):
        super().__init__(
            "等待确认需求。"
            if approval["kind"] == "requirements"
            else "等待批准工具变更。"
        )
        self.approval = approval


async def schedule_continuation(org, sid, jid):
    import uuid

    from loguru import logger

    from api.tasks.arq import get_arq_redis

    try:
        redis = await get_arq_redis()
        await redis.enqueue_job(
            "execute_workbench_job",
            org,
            sid,
            jid,
            _queue_name="arq:agent-workbench",
            _job_id=f"workbench:{jid}:{uuid.uuid4()}",
        )
    except Exception:  # noqa: BLE001 - the committed DB outbox owns retries
        logger.warning("Workbench continuation retained in the database outbox")


STATE_FIELDS = (
    "candidate_ids",
    "suite_ids",
    "regression_suite_ids",
    "base_code",
    "saved_snapshot",
    "last_submission",
    "saved_removals",
    "source",
    "base_mapping",
    "coverage",
    "edit_stats",
    "intent",
    "mode",
    "creation_direction",
    "repair_plan",
    "unchanged_repairs",
)
MUTATING_TOOLS = {"save_candidate", "save_suite", "save_scenarios", "reuse_test_suites"}


class Orchestration:
    def __init__(self, tools, namespace="authoring"):
        self.tools = tools
        self.namespace = namespace
        self.lock = asyncio.Lock()
        self.data = {"version": 1}

    @property
    def scope(self):
        t = self.tools
        return (t.org, t.session_id, t.job["id"], t.job["lease_token"])

    async def load(self):
        self.data = await db_client.wb_orchestration(*self.scope[:3])
        for key, value in (
            self.data.get("working_states", {}).get(self.namespace, {}).items()
        ):
            if key in STATE_FIELDS:
                setattr(self.tools, key, copy.deepcopy(value))
        # Artifact IDs are committed in the same transaction as their creation.
        # Recover the narrow crash window before the post-tool checkpoint.
        recovered_candidate = False
        for key, artifact in self.data.get("artifacts", {}).items():
            if not key.startswith(self.namespace + ":"):
                continue
            name = "candidate_ids" if artifact["kind"] == "candidate" else "suite_ids"
            ids = getattr(self.tools, name)
            if artifact["id"] not in ids:
                ids.append(artifact["id"])
                recovered_candidate |= name == "candidate_ids"
        if recovered_candidate:
            self.tools.last_submission = None
        if self.tools.candidate_ids:
            candidate = await db_client.wb_get(
                *self.scope[:2], "candidates", self.tools.candidate_ids[-1]
            )
            self.tools.saved_snapshot = candidate["snapshot"]
            if not self.tools.last_submission:
                from api.mcp_server.ts_bridge import generate_code
                from api.services.agent_workbench.sop import without_source
                from api.services.configuration.masking import mask_workflow_definition

                self.tools.base_code = await generate_code(
                    mask_workflow_definition(
                        without_source(candidate["snapshot"]["workflow_json"])
                    ),
                    workflow_name=candidate["name"],
                )
        await self.save("started", True)

    def state(self):
        return {key: copy.deepcopy(getattr(self.tools, key)) for key in STATE_FIELDS}

    def key(self, key):
        if key.startswith(("approved-tool:", self.namespace + ":")):
            return key
        return self.namespace + ":" + key

    async def save(self, key, value):
        self.data = await db_client.wb_checkpoint(
            *self.scope,
            self.key(key),
            value,
            state=self.state(),
            namespace=self.namespace,
        )

    async def step(self, key, operation):
        key = self.key(key)
        if key in self.data.get("steps", {}):
            return copy.deepcopy(self.data["steps"][key])
        result = await operation()
        await self.save(key, result)
        return result

    async def call(self, name, arguments, operation):
        async with self.lock:
            key = self.key(
                "tool:" + fingerprint({"name": name, "arguments": arguments})
            )
            if name in MUTATING_TOOLS and key in self.data.get("steps", {}):
                return copy.deepcopy(self.data["steps"][key])
            self.tools.operation_key = key
            try:
                result = await operation()
            except Exception:
                # Invalid SDK submissions are useful recoverable editing state too.
                await self.save("editing", None)
                raise
            finally:
                self.tools.operation_key = None
            await self.save(
                key if name in MUTATING_TOOLS else "editing",
                result if name in MUTATING_TOOLS else None,
            )
            return result

    async def request(self, kind, proposal):
        approval = await db_client.wb_request_approval(*self.scope, kind, proposal)
        self.data = await db_client.wb_orchestration(*self.scope[:3])
        if approval["status"] == "pending":
            raise ApprovalRequired(approval)
        return approval

    async def requirements(self, basis, direction, llm, event, protocol):
        from api.services.agent_workbench.basis import source_records
        from api.services.agent_workbench.review import ReviewSourceReader
        from api.services.agent_workbench.test_tasks import decision

        previous = sorted(
            [
                a
                for a in self.data.get("approvals", {}).values()
                if a["kind"] == "requirements"
            ],
            key=lambda a: a.get("sequence", 0),
        )
        if previous and previous[-1]["status"] == "approved":
            return previous[-1]["proposal"]["brief"]
        if previous and previous[-1]["status"] == "pending":
            raise ApprovalRequired(previous[-1])
        reader = ReviewSourceReader({"records": source_records(basis)})

        def validate(value):
            reader.require_read()
            brief = CallBrief.model_validate(value)
            if brief.direction != direction["direction"]:
                raise WorkbenchError("需求摘要的方向必须与原始资料一致。")
            if len(json.dumps(value, ensure_ascii=False)) > 24000:
                raise WorkbenchError("需求摘要过长，请保留清晰的业务要求。")

        async def prepare():
            return await decision(
                "prepare_requirements",
                "Read the original task basis and prepare a concise CallBrief "
                "for HUMAN approval. "
                "Keep source business rules, required wording and success criteria. "
                "Do not invent missing business facts. "
                "Separate optional assumptions from requirements. User corrections "
                "supersede the affected prior summary. "
                "This brief supplements the originals; it never replaces their full "
                "detail. Do not create an Agent.",
                CallBrief.model_json_schema(),
                {
                    **reader.packet,
                    "direction": direction,
                    "previous_proposals": previous,
                },
                llm,
                event,
                protocol,
                validate=validate,
                reader=reader,
            )

        brief = await self.step("requirements:" + basis["fingerprint"], prepare)
        approval = await self.request(
            "requirements",
            {
                "brief": CallBrief.model_validate(brief).model_dump(),
                "basis_fingerprint": basis["fingerprint"],
            },
        )
        return approval["proposal"]["brief"]

    async def apply_approved_tools(self):
        from api.services.tool_management import (
            ToolManagementError,
            create_tool_for_user,
            update_tool_for_user,
        )

        for approval in list(self.data.get("approvals", {}).values()):
            if approval["kind"] != "tool_change" or approval["status"] != "approved":
                continue
            key = "approved-tool:" + approval["id"]
            if key in self.data.get("steps", {}):
                continue
            proposal = approval["proposal"]
            actor = SimpleNamespace(
                id=approval["decided_by"],
                provider_id=approval["decided_provider_id"],
                selected_organization_id=self.tools.org,
            )
            # The trusted operation identity comes only from the stored human handoff.
            operation_id = "workbench:" + approval["id"]
            await db_client.wb_heartbeat(*self.scope)
            authorization = {
                "session_id": self.tools.session_id,
                "job_id": self.tools.job["id"],
                "token": self.tools.job["lease_token"],
                "revision": approval["revision"],
            }
            try:
                if proposal["operation"] == "create":
                    result = await create_tool_for_user(
                        CreateToolRequest.model_validate(proposal["request"]),
                        actor,
                        source="workbench",
                        operation_id=operation_id,
                        workbench_authorization=authorization,
                    )
                else:
                    result = await update_tool_for_user(
                        proposal["tool_uuid"],
                        UpdateToolRequest.model_validate(proposal["request"]),
                        actor,
                        source="workbench",
                        operation_id=operation_id,
                        workbench_authorization=authorization,
                        expected_revision=proposal["tool_revision"],
                    )
            except ToolManagementError as error:
                # Keep the approved version immutable. Let the model read current
                # state and submit a revised proposal instead of retrying stale writes.
                await self.save(
                    key,
                    {
                        "saved": False,
                        "error": error.message,
                        "error_code": error.error_code,
                    },
                )
                continue
            await self.save(
                key, {"tool_uuid": result.tool_uuid, "name": result.name, "saved": True}
            )

    def receipts(self):
        return {
            "approvals": list(self.data.get("approvals", {}).values()),
            "completed_tools": {
                k: v
                for k, v in self.data.get("steps", {}).items()
                if k.startswith("approved-tool:")
            },
            "candidate_ids": self.tools.candidate_ids,
            "suite_ids": self.tools.suite_ids,
        }
