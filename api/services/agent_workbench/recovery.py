"""Durable task boundaries; continuation retains the original inputs and version."""

import copy

from api.services.agent_workbench.errors import WorkbenchError


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
    legacy_test = classified_test and prior["status"] in {"failed", "cancelled"}
    if (
        prior.get("kind", "build") != "build"
        or prior["status"] not in {"failed", "cancelled", "completed", "awaiting_input"}
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
            else pending["text"]
        ),
    )
    if output.get("evaluation_ids") or pending.get("resume_test_job_id"):
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
