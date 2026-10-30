"""Execution authority is distinct from routing a request to test handling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import jobs
from api.services.agent_workbench import test_operations as operations
from api.services.agent_workbench import test_tasks as tasks
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.recovery import continued_test_input


def request(text, **fields):
    return {
        "id": "current",
        "kind": "build",
        "lease_token": "lease",
        "output": {},
        "input": {"mode": "auto", "text": text, "prepare_only": False, **fields},
    }


def prior(operation=None, **fields):
    return {
        "id": "previous",
        "kind": "build",
        "status": "cancelled",
        "input": {"text": "Prepare reusable scenarios", "mode": "test", **fields},
        "output": {
            "intent": "test",
            **({"test_operation": operation} if operation else {}),
            "preparation_receipts": [{"name": "saved", "suite_id": "receipt"}],
        },
    }


def recorder(monkeypatch, job):
    async def record(org, sid, jid, token, operation, reason):
        assert (org, sid, jid, token) == (7, "session", "current", "lease")
        return {
            "input": operations.operation_input(job["input"], operation),
            "output": {
                **job["output"],
                "test_operation": operation,
                "test_operation_reason": reason,
            },
        }

    save = AsyncMock(side_effect=record)
    monkeypatch.setattr(operations.db_client, "wb_set_test_operation", save)
    return save


@pytest.mark.parametrize("mode", ["auto", "test"])
@pytest.mark.parametrize(
    "text,operation",
    [
        ("把历史工单转成可复用场景，先不要跑", "prepare"),
        ("Generate simulation tests from the supplied dialogues", "prepare"),
        ("准备并执行这些场景", "execute"),
        ("Evaluate the existing agent with the selected suite", "execute"),
    ],
)
async def test_operation_is_semantic_and_separate_from_test_mode(
    monkeypatch, mode, text, operation
):
    job = request(text, mode=mode)
    classify = AsyncMock(
        return_value={"operation": operation, "reason": "current request"}
    )
    monkeypatch.setattr(tasks, "decision", classify)
    save = recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
        == operation
    )
    assert job["input"]["prepare_only"] is (operation == "prepare")
    assert job["output"]["test_operation"] == operation
    assert classify.call_args.args[3]["current_request"] == text
    schema = classify.call_args.args[2]
    assert schema["properties"]["operation"]["enum"] == [
        "prepare",
        "execute",
        "continue",
        "needs_input",
    ]
    save.assert_awaited_once()
    classify.reset_mock()
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
        == operation
    )
    classify.assert_not_called()


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        ("prepare", "continue", "prepare"),
        ("execute", "continue", "execute"),
        ("prepare", "execute", "execute"),
        ("execute", "prepare", "prepare"),
    ],
)
async def test_continuation_preserves_scope_unless_current_request_changes_operation(
    monkeypatch, previous, current, expected
):
    old = prior(
        previous,
        prepare_only=previous == "prepare",
        candidate_id="fixed",
        asset_ids=["original"],
    )
    job = request("Continue that task", continue_test_job_id="previous")
    job["input"] = continued_test_input(old, job["input"])
    job["output"]["test_source_job_id"] = "previous"
    monkeypatch.setattr(operations.db_client, "wb_get", AsyncMock(return_value=old))
    classify = AsyncMock(return_value={"operation": current, "reason": "current scope"})
    monkeypatch.setattr(tasks, "decision", classify)
    recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {"jobs": [old]}, None, AsyncMock()
        )
        == expected
    )
    assert job["input"]["prepare_only"] is (expected == "prepare")
    assert job["input"]["candidate_id"] == "fixed"
    assert job["input"]["asset_ids"] == ["original"]
    assert job["input"]["resume_test_job_id"] == "previous"
    assert classify.call_args.args[3]["current_request"] == "Continue that task"


async def test_legacy_false_is_not_execution_authority(monkeypatch):
    old = prior(prepare_only=False)
    job = request("Retry", continue_test_job_id="previous")
    monkeypatch.setattr(operations.db_client, "wb_get", AsyncMock(return_value=old))
    classify = AsyncMock(
        side_effect=[
            {"operation": "continue", "reason": "resume"},
            {"operation": "prepare", "reason": "original requests generation only"},
        ]
    )
    monkeypatch.setattr(tasks, "decision", classify)
    recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
        == "prepare"
    )
    assert classify.call_args_list[1].args[3] == {
        "current_request": old["input"]["text"]
    }
    assert job["input"]["prepare_only"] is True


@pytest.mark.parametrize("operation", ["continue", "needs_input"])
async def test_ambiguous_scope_cannot_default_to_execution(monkeypatch, operation):
    job = request("处理一下测试")
    monkeypatch.setattr(
        tasks,
        "decision",
        AsyncMock(return_value={"operation": operation, "reason": "unclear"}),
    )
    save = recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
        is None
    )
    save.assert_not_called()


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"prepare_only": True}, "prepare"),
        ({"prepared_test_job_id": "prepared"}, "execute"),
        ({"rerun_test_job_id": "run"}, "execute"),
    ],
)
async def test_explicit_operations_do_not_need_model_permission(
    monkeypatch, fields, expected
):
    job = request("button command", mode="test", **fields)
    classify = AsyncMock()
    monkeypatch.setattr(tasks, "decision", classify)
    recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
        == expected
    )
    classify.assert_not_called()


@pytest.mark.parametrize("operation", ["prepare", "needs_input"])
async def test_preparation_routes_to_test_handling_but_never_builder_or_runtime(
    monkeypatch,
    operation,
):
    job = request("生成测试集，不运行")
    context = {"session": {"model_protocol": "responses"}, "target": {}, "jobs": []}
    monkeypatch.setattr(jobs.db_client, "wb_context", AsyncMock(return_value=context))
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    config = SimpleNamespace(llm=SimpleNamespace(api_key="local", provider="openai"))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(effective=config, snapshot={})),
    )
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    classify = AsyncMock(
        side_effect=[
            {"intent": "test", "reason": "generate cases"},
            {"operation": operation, "reason": "no execution requested"},
        ]
    )
    monkeypatch.setattr(tasks, "decision", classify)
    recorder(monkeypatch, job)

    async def run(org, sid, current, *args):
        assert current["input"]["prepare_only"] is True
        assert current["output"]["test_operation"] == "prepare"
        return {"outcome": "test_preparation"}

    runner = AsyncMock(side_effect=run)
    monkeypatch.setattr(tasks, "run_test_task", runner)
    build = AsyncMock(side_effect=AssertionError("Must not build/edit Agent"))
    monkeypatch.setattr(jobs, "_build", build)
    result = await jobs._perform(7, "session", job)
    if operation == "prepare":
        assert result == {"outcome": "test_preparation"}
        runner.assert_awaited_once()
    else:
        assert result["outcome"] == "needs_input"
        assert result["question"] == operations.QUESTION
        assert result["agent_modified"] is False
        runner.assert_not_called()
    assert [c.args[0] for c in classify.call_args_list] == [
        "classify_request",
        "classify_test_operation",
    ]
    build.assert_not_called()


async def test_receipt_mismatch_and_legacy_ambiguity_do_not_authorize_execution(
    monkeypatch,
):
    job = request("continue")
    job["output"]["test_operation"] = "prepare"
    with pytest.raises(WorkbenchError, match="不一致"):
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
    job["output"] = {}
    job["input"]["continue_test_job_id"] = "previous"
    old = prior(prepare_only=False)
    monkeypatch.setattr(operations.db_client, "wb_get", AsyncMock(return_value=old))
    monkeypatch.setattr(
        tasks,
        "decision",
        AsyncMock(
            side_effect=[
                {"operation": "continue", "reason": "retry"},
                {"operation": "needs_input", "reason": "original scope ambiguous"},
            ]
        ),
    )
    save = recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {}, None, AsyncMock()
        )
        is None
    )
    save.assert_not_called()


async def test_latest_pause_is_included_without_assistant_claims(monkeypatch):
    old = prior("execute")
    pause = {
        "id": "pause",
        "input": {"text": "暂停执行，只整理测试集"},
        "output": {"messages": "I will execute everything"},
    }
    job = request("继续", continue_test_job_id="previous")
    monkeypatch.setattr(operations.db_client, "wb_get", AsyncMock(return_value=old))
    classify = AsyncMock(
        return_value={
            "operation": "prepare",
            "reason": "latest scope restricts execution",
        }
    )
    monkeypatch.setattr(tasks, "decision", classify)
    recorder(monkeypatch, job)
    assert (
        await operations.resolve_test_operation(
            7, "session", job, {"jobs": [job, pause, old]}, None, AsyncMock()
        )
        == "prepare"
    )
    evidence = classify.call_args.args[3]
    assert evidence["recent_user_requests"][0] == pause["input"]["text"]
    assert "I will execute everything" not in str(evidence)
