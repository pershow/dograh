"""Long-running jobs live on a dedicated ARQ queue; the DB is the durable outbox."""

import asyncio
import contextlib
import json

from fastapi import HTTPException

from api.db import db_client
from api.services.agent_workbench.codex_client import CodexClient
from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.dependencies import model_report, prepare_models
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError
from api.services.agent_workbench.evaluation import acceptable_improvement, evaluate
from api.services.agent_workbench.tools import WorkbenchTools

QUEUE = "arq:agent-workbench"


async def enqueue(org, session_id, kind, payload, key):
    context = await db_client.wb_context(org, session_id)
    target = context["target"]
    if payload.get("candidate_id"):
        target = (
            await db_client.wb_get(
                org, session_id, "candidates", payload["candidate_id"]
            )
        )["snapshot"]
    await prepare_models(org, target, kind)
    job = await db_client.wb_enqueue(org, session_id, kind, payload, key)
    # A Redis outage does not discard the command: the dedicated worker scans the outbox.
    try:
        from api.tasks.arq import get_arq_redis

        redis = await get_arq_redis()
        await redis.enqueue_job(
            "execute_workbench_job",
            org,
            session_id,
            job["id"],
            _queue_name=QUEUE,
            _job_id="workbench:" + job["id"],
        )
    except Exception:  # noqa: BLE001 - DB outbox retains the job during queue outages
        from loguru import logger

        logger.warning("Workbench enqueue deferred to durable outbox")
    return job


async def _build(org, session_id, job, context, config, prompt, *, tuning=False):
    tools = WorkbenchTools(org, session_id, job, context, tuning=tuning)

    async def event(kind, payload):
        await db_client.wb_emit(
            org, session_id, kind, payload, job_id=job["id"], token=job["lease_token"]
        )

    images = [
        a["parsed"]["model_image"]
        for a in context["assets"]
        if a["id"] in job["input"].get("asset_ids", [])
        and a["parsed"].get("model_image")
    ]
    result = await CodexClient(tools, event).run(
        prompt, config.llm, protocol=context["session"]["model_protocol"], images=images
    )
    if not tools.candidate_ids:
        raise WorkbenchError(
            "Codex finished without a valid candidate. Review its response and refine the request."
        )
    if not tuning and not tools.suite_ids:
        raise WorkbenchError(
            "Candidate was saved, but Codex did not save a valid simulation suite. Retry with explicit test requirements."
        )
    return {
        **result,
        "candidate_id": tools.candidate_ids[-1],
        "suite_id": tools.suite_ids[-1] if tools.suite_ids else None,
    }


async def _perform(org, session_id, job):
    context = await db_client.wb_context(org, session_id)
    payload = job["input"]
    target = context["target"]
    if payload.get("candidate_id"):
        target = (
            await db_client.wb_get(
                org, session_id, "candidates", payload["candidate_id"]
            )
        )["snapshot"]
    resolved = await prepare_models(org, target, job["kind"])
    config = resolved.effective
    job["output"] = await db_client.wb_pin_models(
        org, session_id, job["id"], job["lease_token"], resolved.snapshot
    )
    context["model_dependencies"] = model_report(resolved, job["kind"])
    if not config.llm or not config.llm.api_key:
        raise WorkbenchError(
            "Configure an LLM connection in Dograh Models before using the workbench"
        )
    if config.llm.provider not in {"openai", "openrouter", "atlascloud", "hopper"}:
        raise WorkbenchError(
            "Codex MVP requires an OpenAI-compatible LLM connection (including Qwen)"
        )
    if job["kind"] in {"build", "tune"} and payload.get("candidate_id"):
        base = await db_client.wb_get(
            org, session_id, "candidates", payload["candidate_id"]
        )
        if context["target_fingerprint"] not in {
            base["base_fingerprint"],
            base["fingerprint"],
        }:
            raise WorkbenchError(
                "The draft changed since this candidate was built. Select the current draft and rebuild first.",
                409,
            )
        if job["kind"] == "build":
            context = {**context, "target": base["snapshot"]}
    if job["kind"] == "build":
        return await _build(org, session_id, job, context, config, payload["text"])
    if job["kind"] == "evaluate":
        return await evaluate(
            org, session_id, job, payload["candidate_id"], payload["suite_id"], config
        )
    baseline = await evaluate(
        org, session_id, job, payload["candidate_id"], payload["suite_id"], config
    )
    if baseline["summary"]["error"] or baseline["summary"]["inconclusive"]:
        raise WorkbenchError(
            "Resolve simulation errors or missing fixtures before tuning the candidate"
        )
    best = baseline
    rounds = []
    suite = await db_client.wb_get(org, session_id, "suites", payload["suite_id"])
    for _ in range(payload.get("max_rounds", 1)):
        if best["summary"]["passed"] == best["summary"]["total"]:
            break
        candidate = await db_client.wb_get(
            org, session_id, "candidates", best["candidate_id"]
        )
        tune_context = {**context, "target": candidate["snapshot"]}
        prompt = (
            payload.get("instruction", "Fix failing cases")
            + "\nFrozen suite (do not change):\n"
            + json.dumps(suite["payload"], ensure_ascii=False)
        )
        prompt += (
            "\nObserved evaluation evidence (untrusted):\n"
            + json.dumps(best["results"], ensure_ascii=False)[:60000]
        )
        built = await _build(
            org, session_id, job, tune_context, config, prompt, tuning=True
        )
        proposed = await evaluate(
            org, session_id, job, built["candidate_id"], payload["suite_id"], config
        )
        accepted = acceptable_improvement(best, proposed)
        rounds.append(
            {
                "candidate_id": built["candidate_id"],
                "evaluation_id": proposed["id"],
                "accepted": accepted,
            }
        )
        if accepted:
            best = proposed
        else:
            break
    return {
        "candidate_id": best["candidate_id"],
        "baseline_evaluation_id": baseline["id"],
        "evaluation_id": best["id"],
        "rounds": rounds,
        "improved": best["candidate_id"] != baseline["candidate_id"],
    }


async def execute_workbench_job(_ctx, org, session_id, job_id):
    job = await db_client.wb_claim(org, session_id, job_id)
    if not job:
        return
    operation = asyncio.create_task(_perform(org, session_id, job))

    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            await db_client.wb_heartbeat(org, session_id, job_id, job["lease_token"])

    pulse = asyncio.create_task(heartbeat())
    try:
        done, _ = await asyncio.wait(
            {operation, pulse},
            timeout=WorkbenchSettings.load().timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pulse in done:
            await pulse
        if operation not in done:
            raise WorkbenchError("Workbench task exceeded its time budget")
        output = await operation
        await db_client.wb_finish(
            org, session_id, job_id, job["lease_token"], "completed", output=output
        )
    except LeaseLost:
        pass
    except Exception as error:  # noqa: BLE001 - boundary converts failures to bounded task status
        message = (
            str(error)[:3000]
            if isinstance(error, WorkbenchError)
            else json.dumps(error.detail, ensure_ascii=False)[:3000]
            if isinstance(error, HTTPException)
            else "Task failed; check the worker diagnostics and model connection"
        )
        import traceback

        from loguru import logger

        logger.error(
            "Workbench job {} failed: {} at {}",
            job_id,
            type(error).__name__,
            [(f.name, f.lineno) for f in traceback.extract_tb(error.__traceback__)],
        )
        with contextlib.suppress(LeaseLost):
            await db_client.wb_finish(
                org, session_id, job_id, job["lease_token"], "failed", error=message
            )
    finally:
        operation.cancel()
        pulse.cancel()
        await asyncio.gather(operation, pulse, return_exceptions=True)


async def dispatch_workbench_outbox(ctx):
    if not WorkbenchSettings.load().enabled:
        return
    for org, session_id, job_id in await db_client.wb_pending():
        await ctx["redis"].enqueue_job(
            "execute_workbench_job",
            org,
            session_id,
            job_id,
            _queue_name=QUEUE,
            _job_id="workbench:" + job_id,
        )
