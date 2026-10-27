"""Contextual routing preserves test commands without reviving unrelated work."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import jobs
from api.services.agent_workbench import test_tasks as tasks
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.recovery import continued_test_input


def failed_request(**overrides):
    return {
        "id": "source",
        "kind": "build",
        "status": "failed",
        "input": {
            "mode": "auto",
            "text": "Replay all customer turns in the revised dialogue column.",
            "asset_ids": ["original-file"],
            "single_step_turns": [],
        },
        "output": {},
        **overrides,
    }


def current_request(**overrides):
    return {
        "id": "current",
        "kind": "build",
        "lease_token": "lease",
        "input": {"mode": "auto", "text": "继续", "asset_ids": []},
        "output": {},
        **overrides,
    }


def test_prepare_only_continuation_preserves_full_scope_and_checkpoint_without_evaluation():
    prior = failed_request(
        status="cancelled",
        input={
            "mode": "test",
            "text": "prepare all",
            "prepare_only": True,
            "test_mode": "dynamic",
            "test_suite_ids": ["first", "last"],
            "candidate_id": "fixed",
        },
        output={"preparation_receipts": [{"name": "done", "suite_id": "saved"}]},
    )
    restored = continued_test_input(prior, {"text": "继续"})
    assert restored["resume_test_job_id"] == "source"
    assert restored["prepare_only"] is True
    assert restored["test_suite_ids"] == ["first", "last"]
    assert restored["candidate_id"] == "fixed"
    assert restored["text"] == "prepare all"


@pytest.mark.parametrize(
    "text", ["继续", "从刚才中断的地方接着跑", "Resume the unfinished replay."]
)
async def test_early_failure_restores_original_request_through_analysis_detour(
    monkeypatch, text
):
    prior = failed_request()
    detour = failed_request(
        id="explanation",
        status="completed",
        input={"text": "继续"},
        output={"outcome": "analysis", "messages": "No simulation tool was exposed."},
    )
    job = current_request(input={"mode": "auto", "text": text, "asset_ids": []})
    classify = AsyncMock(
        side_effect=[
            {
                "intent": "test",
                "source_job_id": "source",
                "reason": "continue original replay",
            },
            {"intent": "test", "reason": "original request asks for tests"},
        ]
    )
    monkeypatch.setattr(tasks, "decision", classify)
    get = AsyncMock(return_value=prior)
    restore = AsyncMock(
        return_value={
            "input": continued_test_input(prior, job["input"], classified_test=True),
            "output": {"intent": "test", "test_source_job_id": "source"},
        }
    )
    monkeypatch.setattr(tasks.db_client, "wb_get", get)
    monkeypatch.setattr(tasks.db_client, "wb_resume_test_request", restore)
    context = {"jobs": [job, detour, prior]}
    assert await tasks.route_test_request(
        7, "session", job, context, None, AsyncMock(), "responses"
    )
    evidence = classify.call_args_list[0].args[3]
    assert evidence["current_request"] == text
    assert [p["job_id"] for p in evidence["recent_requests"]] == [
        "explanation",
        "source",
    ]
    assert evidence["recent_requests"][1]["execution"]["evaluation_ids"] == []
    assert "No simulation tool" not in str(evidence)
    assert classify.call_args_list[1].args[3] == {
        "current_request": prior["input"]["text"]
    }
    get.assert_awaited_once_with(7, "session", "jobs", "source")
    restore.assert_awaited_once_with(
        7, "session", "current", "lease", "source", classified_test=True
    )
    assert job["input"]["text"] == prior["input"]["text"]
    assert job["input"]["asset_ids"] == ["original-file"]
    assert job["input"]["test_continuation_text"] == text
    assert "resume_test_job_id" not in job["input"]


@pytest.mark.parametrize(
    "text,intent",
    [
        ("解释刚才为什么失败", "other"),
        ("停止之前的任务", "other"),
        ("修改开场后再测试", "other"),
        ("只运行新附件", "test"),
        ("重新执行所有测试，不复用结果", "test"),
    ],
)
async def test_standalone_request_keeps_its_own_inputs(monkeypatch, text, intent):
    job = current_request(
        input={"mode": "auto", "text": text, "asset_ids": ["new-file"]}
    )
    before = copy.deepcopy(job)
    monkeypatch.setattr(
        tasks,
        "decision",
        AsyncMock(
            return_value={
                "intent": intent,
                "source_job_id": None,
                "reason": "independent request",
            }
        ),
    )
    restore = AsyncMock()
    monkeypatch.setattr(tasks.db_client, "wb_resume_test_request", restore)
    assert await tasks.route_test_request(
        7, "s", job, {"jobs": [failed_request()]}, None, AsyncMock()
    ) == (intent == "test")
    assert job == before
    restore.assert_not_called()


@pytest.mark.parametrize("mode", ["build", "analyze"])
async def test_explicit_non_test_mode_never_revives_history(monkeypatch, mode):
    classify = AsyncMock()
    monkeypatch.setattr(tasks, "decision", classify)
    job = current_request(input={"text": "继续", "mode": mode})
    assert not await tasks.route_test_request(
        7, "s", job, {"jobs": [failed_request()]}, None, AsyncMock()
    )
    classify.assert_not_called()


async def test_original_request_must_independently_qualify_as_test(monkeypatch):
    prior = failed_request(
        input={"text": "Rewrite the Agent and test it", "mode": "auto"}
    )
    monkeypatch.setattr(
        tasks,
        "decision",
        AsyncMock(
            side_effect=[
                {
                    "intent": "test",
                    "source_job_id": "source",
                    "reason": "mistaken reference",
                },
                {"intent": "other", "reason": "original request edits the Agent"},
            ]
        ),
    )
    monkeypatch.setattr(tasks.db_client, "wb_get", AsyncMock(return_value=prior))
    restore = AsyncMock()
    monkeypatch.setattr(tasks.db_client, "wb_resume_test_request", restore)
    with pytest.raises(WorkbenchError, match="可恢复"):
        await tasks.route_test_request(
            7, "s", current_request(), {"jobs": [prior]}, None, AsyncMock()
        )
    restore.assert_not_called()


def test_clarification_is_added_once_and_preserves_execution_configuration():
    pending = {
        "text": "test the attachment",
        "asset_ids": ["file"],
        "test_mode": "scripted",
        "single_step_turns": [{"role": "user", "content": "known fact"}],
        "candidate_id": "version",
        "test_target_session_id": "original-session",
        "test_suite_ids": ["suite"],
    }
    prior = failed_request(
        status="awaiting_input",
        output={
            "outcome": "needs_input",
            "task_mode": "test",
            "pending_request": pending,
        },
    )
    first = continued_test_input(prior, {"text": "Use the revised column"})
    second = continued_test_input(prior, first)
    assert first == second
    assert (
        second["text"] == "test the attachment\n本次补充回答：\nUse the revised column"
    )
    for key, value in pending.items():
        if key != "text":
            assert second[key] == value


async def test_explicit_early_continuation_is_queued_for_worker_validation(monkeypatch):
    from api.tasks import arq

    prior = failed_request()
    monkeypatch.setattr(jobs.db_client, "wb_get", AsyncMock(return_value=prior))
    monkeypatch.setattr(
        jobs.db_client,
        "wb_context",
        AsyncMock(return_value={"target": {"name": "changed"}}),
    )
    monkeypatch.setattr(
        jobs.db_client,
        "wb_requested_target",
        AsyncMock(return_value={"name": "original"}),
    )
    prepare = AsyncMock()
    monkeypatch.setattr(jobs, "prepare_models", prepare)
    enqueue = AsyncMock(return_value={"id": "new"})
    monkeypatch.setattr(jobs.db_client, "wb_enqueue", enqueue)
    monkeypatch.setattr(
        arq,
        "get_arq_redis",
        AsyncMock(return_value=SimpleNamespace(enqueue_job=AsyncMock())),
    )
    payload = {"text": "continue", "continue_test_job_id": "source"}
    await jobs.enqueue(7, "s", "build", payload, "key")
    prepare.assert_awaited_once_with(7, {"name": "original"}, "build")
    assert enqueue.call_args.args[3] == payload


async def test_auto_continuation_enters_executor_and_never_builder(monkeypatch):
    prior = failed_request(output={"intent": "test"})
    context = {
        "session": {"model_protocol": "responses"},
        "target": {},
        "jobs": [prior],
    }
    monkeypatch.setattr(jobs.db_client, "wb_context", AsyncMock(return_value=context))
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(jobs.db_client, "wb_get", AsyncMock(return_value=prior))
    monkeypatch.setattr(
        jobs.db_client,
        "wb_resume_test_request",
        AsyncMock(
            return_value={
                "input": continued_test_input(prior, {"text": "继续"}),
                "output": {"intent": "test", "test_source_job_id": "source"},
            }
        ),
    )
    config = SimpleNamespace(llm=SimpleNamespace(api_key="test", provider="openai"))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(effective=config, snapshot={})),
    )
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    monkeypatch.setattr(
        tasks,
        "decision",
        AsyncMock(
            return_value={
                "intent": "test",
                "source_job_id": "source",
                "reason": "continuation",
            }
        ),
    )
    run = AsyncMock(return_value={"outcome": "test", "summary": {"executed": 2}})
    build, repair = AsyncMock(), AsyncMock()
    monkeypatch.setattr(tasks, "run_test_task", run)
    monkeypatch.setattr(jobs, "_build", build)
    monkeypatch.setattr(jobs, "_complete_build", repair)
    assert await jobs._perform(7, "s", current_request()) == {
        "outcome": "test",
        "summary": {"executed": 2},
    }
    assert run.call_args.args[2]["input"]["asset_ids"] == ["original-file"]
    assert run.call_args.args[2]["input"]["text"] == prior["input"]["text"]
    build.assert_not_called()
    repair.assert_not_called()


async def test_import_mapping_survives_failure_before_first_evaluation(monkeypatch):
    selection = {
        "asset_id": "file",
        "sheet": "Cases",
        "dialogue_column": "revised",
        "id_column": "ID",
        "user_labels": ["Customer"],
        "assistant_labels": ["Agent"],
    }
    prior = failed_request(
        input={"text": "test revised", "asset_ids": ["file"]},
        output={
            "intent": "test",
            "source_manifest": [{**selection, "sha256": "hash", "rows": 2}],
        },
    )
    monkeypatch.setattr(tasks.db_client, "wb_get", AsyncMock(return_value=prior))
    payload = continued_test_input(prior, {"text": "continue"})
    tables = [{"asset_id": "file", "sheet": "Cases", "sha256": "hash"}]
    assert "resume_test_job_id" not in payload
    assert await tasks.reusable_source_mapping(7, "s", payload, tables) == {
        "needs_input": "",
        "tables": [selection],
    }
    tables[0]["sha256"] = "changed"
    with pytest.raises(WorkbenchError, match="内容已变化"):
        await tasks.reusable_source_mapping(7, "s", payload, tables)
