"""Reject incomplete model submissions before saving or executing test cases."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest
from pydantic import ValidationError

from api.schemas.agent_workbench import SimulationSuite
from api.services.agent_workbench import basis, jobs
from api.services.agent_workbench import test_tasks as tasks
from api.services.agent_workbench.codex_client import CodexClient
from api.services.agent_workbench.errors import InvalidDecision, WorkbenchError


def valid_suite():
    return {
        "name": "Source grounded checks",
        "cases": [
            {
                "name": "Opening",
                "user_messages": ["private customer input"],
                "assertions": [
                    {
                        "kind": "contains",
                        "value": "required answer",
                        "key": None,
                        "turn": 1,
                    }
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    "case_update,expected",
    [
        ({"assertions": []}, "Tests need assertions"),
        (
            {
                "assertions": [
                    {"kind": "contains", "value": "required answer", "turn": 2}
                ]
            },
            "Assertion turn",
        ),
        ({"assertions": [{"kind": "contains", "value": False}]}, "nonempty string"),
        ({"mode": "dynamic"}, "structured source conversation"),
        ({"mode": "single_step"}, "original history"),
        (
            {
                "fixtures": [
                    {"tool_uuid": "a", "name": "same"},
                    {"tool_uuid": "b", "name": "same"},
                ]
            },
            "must be unique",
        ),
        (
            {
                "fixtures": [
                    {
                        "tool_uuid": "a",
                        "name": "remote",
                        "parameters": {"$ref": "https://private.example/schema"},
                    }
                ]
            },
            "cannot reference remote",
        ),
    ],
)
async def test_schema_valid_but_semantically_invalid_submission_is_not_accepted(
    case_update, expected
):
    invalid = valid_suite()
    invalid["cases"][0].update(case_update)
    schema = SimulationSuite.model_json_schema()
    jsonschema.validate(invalid, schema)
    dispatcher = tasks.StructuredDecision(
        "submit_test_suite",
        "Preserve the test requirements",
        schema,
        validate=SimulationSuite.model_validate,
    )
    events = AsyncMock()
    client = CodexClient(dispatcher, events)
    result = await client.handle_request(
        "item/tool/call", {"tool": "submit_test_suite", "arguments": invalid}
    )
    assert not result["success"]
    assert expected in str(result) and "$.cases[0]" in str(result)
    assert "private customer input" not in str(result)
    assert "https://private.example" not in str(result)
    assert dispatcher.report is None and not client.done.done()
    assert dispatcher.invalid_attempts == 1
    assert "tool.completed" not in [c.args[0] for c in events.call_args_list]
    # Correcting the malformed submission preserves the required answer check.
    result = await client.handle_request(
        "item/tool/call", {"tool": "submit_test_suite", "arguments": valid_suite()}
    )
    assert result["success"] and client.done.done()
    assert dispatcher.report == valid_suite()


async def test_repeated_pydantic_rejections_stop_with_the_exact_rule():
    dispatcher = tasks.StructuredDecision(
        "submit",
        "read only",
        SimulationSuite.model_json_schema(),
        validate=SimulationSuite.model_validate,
    )
    client = CodexClient(dispatcher, AsyncMock())
    for attempt in range(3):
        invalid = valid_suite()
        invalid["cases"][0]["assertions"][0]["turn"] = attempt + 2
        result = await client.handle_request(
            "item/tool/call", {"tool": "submit", "arguments": invalid}
        )
        assert not result["success"] and dispatcher.report is None
    assert dispatcher.invalid_attempts == 3 and client.done.done()
    with pytest.raises(InvalidDecision, match="Assertion turn"):
        client.done.result()


async def test_fresh_decision_attempt_retains_validation_feedback(monkeypatch):
    dispatchers = []

    class Client(CodexClient):
        async def run(self, *_args, **_kwargs):
            dispatchers.append(self.dispatcher)
            value = valid_suite()
            if len(dispatchers) == 1:
                value["cases"][0]["assertions"] = []
            result = await self.handle_request(
                "item/tool/call", {"tool": "submit", "arguments": value}
            )
            assert result["success"] == (len(dispatchers) == 2)

    monkeypatch.setattr(tasks, "CodexClient", Client)
    result = await tasks.decision(
        "submit",
        "read only",
        SimulationSuite.model_json_schema(),
        {},
        None,
        AsyncMock(),
        validate=SimulationSuite.model_validate,
    )
    assert result == valid_suite()
    assert "Tests need assertions" in dispatchers[1].instructions
    assert "private customer input" not in dispatchers[1].instructions


def test_preparation_keeps_assertions_and_reports_the_case_index():
    cases = valid_suite()["cases"]
    before = copy.deepcopy(cases)
    mode, prepared = tasks.prepare_test_cases(cases, "dynamic", automatic_method=True)
    assert mode == "scripted" and cases == before
    assert prepared[0]["assertions"] == before[0]["assertions"]
    with pytest.raises(WorkbenchError, match="固定断言"):
        tasks.prepare_test_cases(cases, "dynamic", automatic_method=False)
    cases.append(
        {"name": "Invalid second case", "user_messages": ["private customer input"]}
    )
    with pytest.raises(
        WorkbenchError, match=r"cases\[1\].*Tests need assertions"
    ) as caught:
        tasks.prepare_test_cases(cases, "scripted")
    assert "private customer input" not in str(caught.value)


def test_single_step_case_cannot_disappear_during_preparation():
    case = {
        "name": "Missing customer history",
        "user_messages": ["hello"],
        "reference_dialogue": "Assistant: welcome",
        "reference_turns": [{"role": "assistant", "text": "welcome"}],
    }
    with pytest.raises(WorkbenchError, match="不能静默跳过"):
        tasks.prepare_test_cases([case], "single_step")


@pytest.mark.parametrize("repair", [True, False])
async def test_generated_suite_is_validated_before_any_save_or_execution(
    monkeypatch, repair
):
    candidate = {"id": "frozen", "fingerprint": "original-fp", "snapshot": {}}
    monkeypatch.setattr(
        tasks.db_client, "wb_test_target", AsyncMock(return_value=candidate)
    )
    monkeypatch.setattr(
        tasks,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(snapshot={}, effective=object())),
    )
    monkeypatch.setattr(tasks.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(tasks.db_client, "wb_job_progress", AsyncMock())
    monkeypatch.setattr(tasks, "load_sources", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(basis, "ensure_basis", AsyncMock(return_value={}))
    save = AsyncMock(return_value={"id": "suite"})
    evaluate = AsyncMock(
        return_value={"id": "execution", "summary": {"passed": 1, "executed": 1}}
    )
    monkeypatch.setattr(tasks.db_client, "wb_save_suite", save)
    monkeypatch.setattr(tasks, "evaluate", evaluate)
    tool_results = []

    class Client(CodexClient):
        async def run(self, *_args, **_kwargs):
            tool = self.dispatcher.completion_tool
            if tool == "select_test_method":
                await self.handle_request(
                    "item/tool/call",
                    {
                        "tool": tool,
                        "arguments": {"mode": "dynamic", "reason": "select method"},
                    },
                )
                return
            assert tool == "submit_test_suite"
            value = valid_suite()
            value["cases"][0]["assertions"][0]["turn"] = 2
            for _ in range(1 if repair else 3):
                result = await self.handle_request(
                    "item/tool/call", {"tool": tool, "arguments": value}
                )
                tool_results.append(result)
                assert not result["success"] and "Assertion turn" in str(result)
                save.assert_not_awaited()
                evaluate.assert_not_awaited()
            if repair:
                result = await self.handle_request(
                    "item/tool/call", {"tool": tool, "arguments": valid_suite()}
                )
                assert result["success"]
            else:
                self.done.result()

    monkeypatch.setattr(tasks, "CodexClient", Client)
    job = {
        "id": "job",
        "lease_token": "lease",
        "input": {"mode": "test", "text": "Check the required answer"},
        "output": {},
    }
    if not repair:
        with pytest.raises(InvalidDecision, match="Assertion turn"):
            await tasks.run_test_task(
                1, "session", job, SimpleNamespace(llm=None), "responses", AsyncMock()
            )
        save.assert_not_awaited()
        evaluate.assert_not_awaited()
        return
    result = await tasks.run_test_task(
        1, "session", job, SimpleNamespace(llm=None), "responses", AsyncMock()
    )
    assert result["summary"]["executed"] == result["summary"]["total"] == 1
    assert result["test_mode"] == "scripted" and not result["agent_modified"]
    payload = save.call_args.args[2]
    assert payload["cases"][0]["assertions"] == valid_suite()["cases"][0]["assertions"]
    evaluate.assert_awaited_once()
    assert evaluate.call_args.args[3] == "frozen"


async def test_job_boundary_keeps_validation_rule_without_echoing_input(monkeypatch):
    invalid = valid_suite()
    invalid["cases"][0]["assertions"][0]["turn"] = 100
    with pytest.raises(ValidationError) as caught:
        SimulationSuite.model_validate(invalid)
    monkeypatch.setattr(
        jobs.db_client,
        "wb_claim",
        AsyncMock(return_value={"id": "job", "lease_token": "lease"}),
    )
    monkeypatch.setattr(jobs, "_perform", AsyncMock(side_effect=caught.value))
    finish = AsyncMock()
    monkeypatch.setattr(jobs.db_client, "wb_finish", finish)
    await jobs.execute_workbench_job({}, 1, "session", "job")
    assert finish.call_args.args[4] == "failed"
    message = finish.call_args.kwargs["error"]
    assert (
        "数据校验失败" in message
        and "$.cases[0]" in message
        and "Assertion turn" in message
    )
    assert "private customer input" not in message and "model connection" not in message
