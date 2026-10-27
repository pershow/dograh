"""Freeze task-wide tuning coverage; partial or uncertain baselines never pass."""

import asyncio
from copy import deepcopy

from api.db import db_client
from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import EVALUATION_VERSION, evaluate
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.simulation import DecisionCache


async def resolve_scope(org, sid, payload):
    source = None
    if payload.get("test_job_id"):
        source = await db_client.wb_get(org, sid, "jobs", payload["test_job_id"])
        output = source.get("output") or {}
        if source["status"] in {"queued", "running"}:
            raise WorkbenchError("请等待所选测试任务完成后调优。", 409)
        if output.get("candidate_id") != payload["candidate_id"]:
            raise WorkbenchError("调优候选必须与所选测试任务的版本一致。", 409)
        ids = output.get("suite_ids") or []
        if not ids or output.get("outcome") != "test":
            raise WorkbenchError("所选任务没有完整的测试集回执。", 422)
    else:
        ids = payload.get("suite_ids") or (
            [payload["suite_id"]] if payload.get("suite_id") else []
        )
    if not ids or len(ids) != len(set(ids)):
        raise WorkbenchError("调优范围必须包含不重复的测试集。", 422)
    suites = [await db_client.wb_get(org, sid, "suites", i) for i in ids]
    for suite in suites:
        if suite["payload"].get("usage") == "acceptance":
            raise WorkbenchError(
                "验收测试集只能用于评估，不能用于自动调优。请选择开发测试集。", 409
            )
        names = [c["name"] for c in suite["payload"]["cases"]]
        if len(names) != len(set(names)):
            raise WorkbenchError(
                "同一测试集的用例名称重复，无法可靠关联回归结果。", 422
            )
    count = sum(len(s["payload"]["cases"]) for s in suites)
    issues = (source or {}).get("output", {}).get("import_issues", [])
    expected = (source or {}).get("output", {}).get("summary", {}).get("total")
    if expected is not None and expected != count + len(issues):
        raise WorkbenchError(
            "测试任务的回执数量与完整测试集不一致，不能静默调优部分用例。", 409
        )
    scope = {
        "source_job_id": (source or {}).get("id"),
        "suite_ids": ids,
        "suite_count": len(suites),
        "case_count": count,
        "import_issues": issues,
        "fingerprint": fingerprint(
            [{"id": s["id"], "payload": s["payload"]} for s in suites]
        ),
    }
    return scope, suites, source


async def evaluate_scope(
    org,
    sid,
    job,
    candidate_id,
    suites,
    runtime,
    *,
    source=None,
    decision_cache=None,
    frozen_contexts=None,
    frozen_scenarios=None,
):
    """Evaluate every frozen batch with one task-wide concurrency limit."""
    cache = decision_cache or DecisionCache()
    semaphore = asyncio.Semaphore(WorkbenchSettings.load().evaluation_concurrency)
    prior = {}
    if source and source.get("output", {}).get("candidate_id") == candidate_id:
        for eid in source["output"].get("evaluation_ids", []):
            e = await db_client.wb_get(org, sid, "evaluations", eid)
            if (
                e["candidate_id"] == candidate_id
                and e["status"] == "completed"
                and e.get("summary", {}).get("evaluation_version") == EVALUATION_VERSION
                and e.get("summary", {}).get("model_configuration_fingerprint")
                == fingerprint(runtime.snapshot)
            ):
                prior[e["suite_id"]] = e

    async def batch(suite):
        old = prior.get(suite["id"])
        names = [c["name"] for c in suite["payload"]["cases"]]
        if (
            old
            and old["summary"].get("suite_fingerprint") == suite["fingerprint"]
            and [c["name"] for c in old["results"]] == names
            and not any(
                c.get("fixtures") or c.get("audio") for c in suite["payload"]["cases"]
            )
        ):
            return old
        return await evaluate(
            org,
            sid,
            job,
            candidate_id,
            suite["id"],
            runtime.effective,
            case_semaphore=semaphore,
            decision_cache=cache,
            frozen_contexts=frozen_contexts,
            frozen_scenarios=frozen_scenarios,
            prior_results={r["name"]: r for r in old["results"]} if old else None,
        )

    # A lost lease or batch failure must stop siblings, not leave simulations
    # running after the parent task has already failed.
    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(batch(s)) for s in suites]
    batches = [task.result() for task in tasks]
    results = [
        {**deepcopy(r), "suite_id": e["suite_id"], "evaluation_id": e["id"]}
        for e in batches
        for r in e["results"]
    ]
    counts = {
        s: sum(r["status"] == s for r in results)
        for s in ("passed", "failed", "error", "inconclusive")
    }
    expected = sum(len(s["payload"]["cases"]) for s in suites)
    if len(results) != expected:
        raise WorkbenchError("调优基线缺少用例结果，不能继续修改候选。", 409)
    if any(r["status"] not in counts for r in results):
        raise WorkbenchError("调优基线仍有未结束用例，不能继续修改候选。", 409)
    return {
        "id": batches[-1]["id"],
        "evaluation_ids": [e["id"] for e in batches],
        "candidate_id": candidate_id,
        "suite_ids": [s["id"] for s in suites],
        "results": results,
        "summary": {**counts, "total": expected},
    }


def blocked_baseline(baseline, scope):
    """Report actionable blockers, keeping infrastructure and Agent outcomes apart."""
    blockers = [
        {
            "suite_id": r["suite_id"],
            "name": r["name"],
            "status": r["status"],
            "stop_reason": (r.get("validity") or {}).get("stop_reason"),
            "reason": r.get("error")
            or (r.get("reference_review") or {}).get("reason", "证据尚不完整"),
        }
        for r in baseline["results"]
        if r["status"] in {"error", "inconclusive"}
    ]
    if not blockers and not scope["import_issues"]:
        return None
    return {
        "candidate_id": baseline["candidate_id"],
        "tuning_scope": scope,
        "baseline_evaluation_ids": baseline["evaluation_ids"],
        "evaluation_ids": baseline["evaluation_ids"],
        "rounds": [],
        "improved": False,
        "blocked_cases": blockers,
        "blocked_reason": "baseline_incomplete",
        "verification_issue": "完整测试集仍有执行异常、证据不足或缺失样本；请先恢复测试或补齐上下文，再调优。",
        "verification": {
            **baseline["summary"],
            "status": "inconclusive",
            "confirmed": False,
        },
    }
