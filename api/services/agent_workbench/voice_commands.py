"""The voice assistant and text UI share the same authenticated commands."""

from api.db import db_client
from api.schemas.agent_workbench import (
    EvaluateRequest,
    Instruction,
    TuneRequest,
    VoiceToolRequest,
)
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.jobs import enqueue

VOICE_FUNCTIONS = [
    {
        "name": "submit_instruction",
        "description": "Start constructing or changing the agent using the user's complete instruction and selected attachments.",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
    {
        "name": "get_status",
        "description": "Read actual task and test status. Never invent completion.",
        "properties": {},
        "required": [],
    },
    {
        "name": "run_tests",
        "description": "Run the latest candidate against the current frozen text suite.",
        "properties": {},
        "required": [],
    },
    {
        "name": "tune_agent",
        "description": "Test and tune the latest candidate against the same suite; do not publish.",
        "properties": {"instruction": {"type": "string"}},
        "required": ["instruction"],
    },
    {
        "name": "cancel_task",
        "description": "Cancel the active task only when explicitly requested. Interrupting speech is not task cancellation.",
        "properties": {},
        "required": [],
    },
]


async def voice_command(org, session_id, request: VoiceToolRequest):
    context = await db_client.wb_context(org, session_id)
    if request.name == "get_status":
        return {
            "jobs": [
                {
                    **{k: j[k] for k in ("id", "kind", "status", "error")},
                    "output": {
                        k: v
                        for k, v in j["output"].items()
                        if k
                        in {
                            "candidate_id",
                            "suite_id",
                            "evaluation_id",
                            "improved",
                            "summary",
                        }
                    },
                }
                for j in context["jobs"][:2]
            ],
            "evaluations": [
                {"id": e["id"], "summary": e["summary"], "status": e["status"]}
                for e in context["evaluations"][:2]
            ],
        }
    if request.name == "cancel_task":
        active = next(
            (j for j in context["jobs"] if j["status"] in {"queued", "running"}), None
        )
        return (
            await db_client.wb_cancel(org, session_id, active["id"])
            if active
            else {"status": "No task running"}
        )
    key = "voice:" + request.call_id
    if request.name == "submit_instruction":
        payload = Instruction(
            candidate_id=request.candidate_id,
            text=request.arguments.get("text", ""),
            asset_ids=request.asset_ids,
            idempotency_key=key,
        )
        return await enqueue(
            org,
            session_id,
            "build",
            payload.model_dump(exclude={"idempotency_key"}),
            key,
        )
    if not context["candidates"] or not context["suites"]:
        raise WorkbenchError("Build a candidate and a simulation suite first")
    base = {
        "candidate_id": request.candidate_id or context["candidates"][0]["id"],
        "suite_id": request.suite_id or context["suites"][0]["id"],
        "idempotency_key": key,
    }
    payload = (
        TuneRequest(
            **base,
            asset_ids=request.asset_ids,
            instruction=request.arguments.get("instruction", "Fix failures"),
        )
        if request.name == "tune_agent"
        else EvaluateRequest(**base)
    )
    return await enqueue(
        org,
        session_id,
        "tune" if request.name == "tune_agent" else "evaluate",
        payload.model_dump(exclude={"idempotency_key"}),
        key,
    )
