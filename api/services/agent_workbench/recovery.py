"""Durable task boundaries; continuation retains the original inputs and version."""

import copy

from api.services.agent_workbench.errors import WorkbenchError


async def restore_prepared_cases(
    org, session_id, source_job_id, cases, *, related_job_ids=()
):
    """Restore matching checkpoints across retries without changing source rows."""
    from api.db import db_client
    from api.services.agent_workbench.test_compilation import case_fingerprint

    indexed = {c["name"]: c for c in cases}
    visited, restored = set(), set()
    pending = [source_job_id, *related_job_ids]
    anchor = None
    while pending:
        source_job_id = pending.pop(0)
        if not source_job_id or source_job_id in visited:
            continue
        visited.add(source_job_id)
        previous = await db_client.wb_get(org, session_id, "jobs", source_job_id)
        from api.services.agent_workbench.test_lineage import parent_ids, same_scope

        anchor = anchor or previous
        if not same_scope(anchor, previous):
            continue
        for receipt in reversed(
            previous.get("output", {}).get("preparation_receipts", [])
        ):
            name = receipt["name"]
            if name not in indexed or name in restored:
                continue
            saved = await db_client.wb_get(
                org, session_id, "suites", receipt["suite_id"]
            )
            source_case = next(
                (c for c in saved["payload"]["cases"] if c["name"] == name), None
            )
            if (
                source_case
                and source_case.get("compiled")
                and case_fingerprint(source_case) == case_fingerprint(indexed[name])
            ):
                indexed[name]["compiled"] = copy.deepcopy(source_case.get("compiled"))
                restored.add(name)
        pending.extend(sorted(parent_ids(previous)))
    return restored


def continued_test_input(prior, request, *, classified_test=False):
    """Restore a test command, including failures before the first evaluation.

    classified_test is internal evidence from classifying the original request;
    it is needed only for legacy auto tasks that failed before recording intent.
    """
    pending = prior["input"]
    output = prior.get("output", {})
    clarification = (
        output.get("outcome") == "needs_input" and output.get("task_mode") == "test"
    )
    known_test = pending.get("mode") == "test" or output.get("intent") == "test"
    preparation = output.get("outcome") == "test_preparation" or bool(
        output.get("preparation_receipts")
    )
    legacy_test = classified_test and prior["status"] in {"failed", "cancelled"}
    if (
        prior.get("kind", "build") != "build"
        or (
            prior["status"]
            not in {"failed", "cancelled", "completed", "awaiting_input"}
            and not (preparation and prior["status"] == "needs_attention")
        )
        or pending.get("mode") in {"build", "analyze"}
        or output.get("outcome") in {"build", "analysis", "review"}
        or not (known_test or clarification or legacy_test)
    ):
        raise WorkbenchError("该任务没有待补充或可恢复的测试。", 409)
    if clarification:
        pending = output["pending_request"]
    continuation_text = request.get("test_continuation_text", request["text"])
    restored = copy.deepcopy(pending)
    for key in (
        "continue_test_job_id",
        "resume_test_job_id",
        "rerun_test_job_id",
        "resume_build_job_id",
        "resume_review_job_id",
        "review_evaluation_ids",
        "test_source_job_id",
    ):
        restored.pop(key, None)
    restored.update(
        mode="test",
        continue_test_job_id=prior["id"],
        test_source_job_id=prior["id"],
        test_continuation_text=continuation_text,
        test_mode=pending.get("test_mode") or output.get("test_mode"),
        candidate_id=output.get("candidate_id")
        or output.get("test_candidate_id")
        or pending.get("candidate_id"),
        text=(
            pending["text"] + "\n本次补充回答：\n" + continuation_text
            if clarification
            or (preparation and output.get("compilation", {}).get("needs_input"))
            else pending["text"]
        ),
    )
    if output.get("evaluation_ids") or pending.get("resume_test_job_id") or preparation:
        restored["resume_test_job_id"] = prior["id"]
    return restored


BUILD_FIELDS = (
    "outcome",
    "candidate_id",
    "suite_id",
    "regression_suite_ids",
    "sop_coverage",
    "edit_stats",
    "model_requests",
    "tokens",
    "messages",
    "repair_plan",
    "evaluation_id",
    "evaluation_ids",
    "suite_ids",
    "baseline_evaluation_id",
    "rounds",
    "verification_checks",
    "verification",
    "generation_seconds",
)


def build_checkpoint(output):
    checkpoint = copy.deepcopy(output.get("build_checkpoint"))
    if checkpoint:
        if checkpoint.get("version") != 1 or checkpoint.get("stage") not in {
            "design",
            "verification",
            "result",
            "repair",
            "complete",
        }:
            raise WorkbenchError("不支持此调优断点版本，已保留原候选。", 409)
        return checkpoint
    if (
        output.get("outcome") != "build"
        or not output.get("candidate_id")
        or not output.get("suite_id")
    ):
        raise WorkbenchError(
            "该任务尚未保存完整候选和测试集，不能从需求审查继续。", 409
        )
    # Compatibility for candidates saved before checkpoint support existed.
    return {
        "version": 1,
        "stage": "design",
        "built": {
            key: copy.deepcopy(output[key]) for key in BUILD_FIELDS if key in output
        },
    }


def resumable_build(prior):
    if prior["kind"] != "build" or prior["status"] not in {"failed", "cancelled"}:
        raise WorkbenchError("只能恢复已失败或已停止的构建任务。", 409)
    checkpoint = build_checkpoint(prior["output"])
    return checkpoint


async def save_checkpoint(org, session_id, job, output, text):
    from api.db import db_client

    await db_client.wb_job_progress(
        org,
        session_id,
        job["id"],
        job["lease_token"],
        output,
        {"phase": "build_checkpoint", "text": text},
    )
    job["output"] = {**job.get("output", {}), **copy.deepcopy(output)}
