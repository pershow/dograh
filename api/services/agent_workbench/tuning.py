"""Freeze task-wide tuning coverage; partial or uncertain baselines never pass."""

import asyncio
from collections import Counter
from copy import deepcopy

from api.db import db_client
from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import EVALUATION_VERSION, evaluate
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.simulation import DecisionCache


def repair_index(baseline):
    """Index every proven defect; retrieve complete evidence with read_evaluation.

    Repeated full transcripts/fixtures do not belong in a builder's initial prompt.
    Examples are navigation links, never a replacement for the frozen full scope.
    """
    groups, offsets = {}, Counter()
    unknown = Counter()
    for case in baseline["results"]:
        eid = case.get("evaluation_id", baseline.get("id"))
        index = offsets[eid]
        offsets[eid] += 1
        review = case.get("reference_review") or {}
        checks = {c.get("criterion_id"): c for c in review.get("checks", [])}
        unknown.update(
            c["status"]
            for c in review.get("checks", [])
            if c["status"] in {"uncovered", "test_error", "needs_input"}
        )
        invalid = (case.get("validity") or {}).get("status") in {
            "invalid",
            "inconclusive",
        }
        for ai, assertion in enumerate(case.get("assertions", [])):
            if assertion.get("passed") or assertion.get("informational"):
                continue
            if (
                assertion.get("kind") == "reference"
                and assertion.get("repair_target") != "agent"
            ):
                continue
            if invalid and not (
                assertion.get("kind") == "reference"
                and assertion.get("independent_of_mismatch") is True
            ):
                continue
            key = assertion.get("criterion_id") or fingerprint(
                {k: assertion.get(k) for k in ("kind", "value", "key", "turn")}
            )
            check = checks.get(key) or {}
            group = groups.setdefault(
                key,
                {
                    "criterion_id": assertion.get("criterion_id"),
                    "criterion": check.get("criterion")
                    or {k: assertion.get(k) for k in ("kind", "value", "key", "turn")},
                    "expected_behavior": check.get("expected_behavior"),
                    "occurrences": 0,
                    "examples": [],
                },
            )
            group["occurrences"] += 1
            if len(group["examples"]) < 3:
                group["examples"].append(
                    {
                        "evaluation_id": eid,
                        "case_index": index,
                        "name": case["name"],
                        "assertion_index": ai,
                        "turn": assertion.get("turn"),
                    }
                )
    return {
        "case_count": len(baseline["results"]),
        "evaluation_ids": list(offsets),
        "failure_groups": list(groups.values()),
        "unresolved_criteria": dict(unknown),
        "guidance": "This is an index, not complete evidence. Read each relevant case's transcript, execution, review and simulation using read_evaluation; follow next_offset. Inspect distinct causes within each group. Only independently established Agent defects authorize a correction. Uncovered branches or unrelated source questions must not be repaired by guessing. Every frozen case will be evaluated after the edit; no samples or assertions may be dropped.",
    }


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
    batch_slots = asyncio.Semaphore(WorkbenchSettings.load().evaluation_concurrency)
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
        ):
            return old
        async with batch_slots:
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
    from api.services.agent_workbench.contract_review import coverage

    return {
        "id": batches[-1]["id"],
        "evaluation_ids": [e["id"] for e in batches],
        "candidate_id": candidate_id,
        "suite_ids": [s["id"] for s in suites],
        "results": results,
        "summary": {**counts, "total": expected},
        "criterion_coverage": coverage(
            [{**c, "suite_id": s["id"]} for s in suites for c in s["payload"]["cases"]],
            results,
        ),
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
            "criterion_ids": [
                c["criterion_id"]
                for c in (r.get("reference_review") or {}).get("checks", [])
                if c.get("criterion_id")
                and c["status"] in {"uncovered", "test_error", "needs_input"}
            ],
        }
        for r in baseline["results"]
        if r["status"] in {"error", "inconclusive"}
        or any(
            c.get("criterion_id")
            and c["status"] in {"uncovered", "test_error", "needs_input"}
            for c in (r.get("reference_review") or {}).get("checks", [])
        )
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


def actionable_partial(baseline):
    """Known frozen-criterion failures can inform a draft despite unrelated gaps."""
    return any(
        (r.get("reference_review") or {}).get("version") == 4
        and any(
            c["status"] == "failed"
            and c.get("repair_target") == "agent"
            and (
                r.get("validity", {}).get("status") == "valid"
                or c.get("independent_of_mismatch")
            )
            for c in r["reference_review"].get("checks", [])
        )
        for r in baseline["results"]
    )


def provisional_progress(baseline, proposed):
    """Track a draft improvement, NEVER certify unknown criteria as passing.

    Full case/criterion identities and evidence coverage must remain unchanged;
    every previously established outcome is protected, including failures that
    must not disappear into an unknown or inapplicable branch.
    """
    old, new = baseline["results"], proposed["results"]
    if len(old) != len(new):
        return False
    improved = False
    for a, b in zip(old, new):
        if (a.get("suite_id"), a["name"]) != (b.get("suite_id"), b["name"]):
            return False
        left, right = a.get("reference_review") or {}, b.get("reference_review") or {}
        if not left.get("checks"):
            # Still report/retain an unprepared case; never treat it as passed.
            ca = a.get("validity", {}).get("compilation") or {}
            cb = b.get("validity", {}).get("compilation") or {}
            if not ca.get("fingerprint") or ca.get("fingerprint") != cb.get(
                "fingerprint"
            ):
                return False
            continue
        if left.get("version") != 4 or right.get("version") != 4:
            return False
        if any(
            left.get(k) != right.get(k)
            for k in ("rubric_fingerprint", "compilation_fingerprint")
        ):
            return False
        prior = {c["criterion_id"]: c for c in left["checks"]}
        later = {c["criterion_id"]: c for c in right["checks"]}
        if prior.keys() != later.keys():
            return False
        for key, check in prior.items():
            status, result = check["status"], later[key]["status"]
            if status == "passed" and result != "passed":
                return False
            if status == "not_applicable" and result not in {
                "not_applicable",
                "passed",
            }:
                return False
            if status == "failed" and result not in {"passed", "failed"}:
                return False
            improved |= status == "failed" and result == "passed"
        # Explicit declarative assertions retain their exact original semantics.
        prior = [x for x in a.get("assertions", []) if not x.get("criterion_id")]
        later = [x for x in b.get("assertions", []) if not x.get("criterion_id")]
        if len(prior) != len(later):
            return False
        for x, y in zip(prior, later):
            if any(x.get(k) != y.get(k) for k in ("kind", "value", "key", "turn")) or (
                x.get("passed") and not y.get("passed")
            ):
                return False
    return improved
