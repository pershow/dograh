"""A workbench session owns one native, durable Codex thread, not a history tail."""

import json
import uuid

from api.db import db_client
from api.services.agent_workbench.context import redact
from api.services.agent_workbench.errors import WorkbenchError


class DurableConversation:
    def __init__(self, tools, root):
        self.tools = tools
        # Values come from the scoped DB context, never client-supplied paths.
        org = int(tools.org)
        workflow = int(tools.context["session"]["workflow_id"])
        uuid.UUID(tools.session_id)
        self.project = f"dograh:{org}:{workflow}"
        self.workspace = root / "projects" / str(org) / str(workflow) / "workspace"
        self.root = self.workspace.parent
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    async def open(self, rpc, params):
        tools = self.tools
        binding = await db_client.wb_codex_binding(tools.org, tools.session_id)
        if binding:
            try:
                return await rpc.request(
                    "thread/resume",
                    {
                        **{k: v for k, v in params.items() if k != "dynamicTools"},
                        "threadId": binding["thread_id"],
                        "excludeTurns": True,
                    },
                )
            except Exception as error:
                # Starting blank here would silently erase the user's conversational memory.
                raise WorkbenchError(
                    "无法恢复此工作台的 Codex 会话。请检查持久存储或恢复备份；没有创建空白会话替代原历史。",
                    503,
                ) from error
        project = await rpc.request(
            "project/create",
            {
                "name": self.tools.context.get("target", {}).get("name")
                or self.project,
                "roots": [{"path": str(self.workspace)}],
                "idempotencyKey": self.project,
            },
        )
        opened = await rpc.request(
            "thread/start",
            {
                **params,
                "ephemeral": False,
                "projectId": project["project"]["id"],
            },
        )
        thread = opened["thread"]["id"]
        boundary = (
            tools.job.get("output", {}).get("history_before")
            or tools.context["session"]["event_sequence"] + 1
        )
        # Import once, losslessly and in chronological order. Bind only after the
        # entire import succeeds, so interrupted migrations cannot resume half a history.
        pages, before = [], boundary
        while True:
            page = await db_client.wb_conversation(
                tools.org, tools.session_id, before=before, limit=60
            )
            pages.append(page["events"])
            before = page["before"]
            if before is None:
                break
        for page in reversed(pages):
            for event in page:
                item = legacy_item(event)
                if item:
                    await rpc.request(
                        "thread/inject_items", {"threadId": thread, "items": [item]}
                    )
        await db_client.wb_emit(
            tools.org,
            tools.session_id,
            "codex.bound",
            {
                "thread_id": thread,
                "project_id": project["project"]["id"],
                "imported_before": boundary,
            },
            job_id=tools.job["id"],
            token=tools.job["lease_token"],
        )
        return opened


def legacy_item(event):
    """Original requests/replies retain roles; receipts are attributed evidence."""
    kind, payload = event["type"], event["payload"]
    role = "assistant"
    if kind == "task.queued":
        request = payload.get("input", {})
        role = "user"
        content = {"legacy_request": request, "kind": payload.get("kind")}
    elif kind in {"assistant.message", "voice.transcript"}:
        role = payload.get("role", "assistant")
        content = payload.get("text", "")
    elif kind in {
        "task.completed",
        "task.needs_attention",
        "task.awaiting_input",
        "task.failed",
        "task.cancelled",
        "candidate.adopted",
    }:
        output = payload.get("output", {})
        content = {
            "record_type": kind,
            "sequence": event["sequence"],
            "outcome": {
                key: output.get(key)
                for key in (
                    "outcome",
                    "candidate_id",
                    "suite_id",
                    "evaluation_id",
                    "verification",
                )
            },
            "error": payload.get("error"),
        }
    else:
        return None
    text = (
        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    )
    return {
        "type": "message",
        "role": role,
        "content": [
            {
                "type": "input_text" if role == "user" else "output_text",
                "text": redact(text),
            }
        ],
    }
