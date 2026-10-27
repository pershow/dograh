import pytest
from pydantic import ValidationError

from api.schemas.agent_workbench import Assertion, SimulationCase
from api.services.agent_workbench.assertions import check_tool_assertion
from api.services.agent_workbench.evaluation_policy import EvaluationPolicy


def test_wrong_attempt_cannot_be_hidden_by_correct_retry():
    assertion = Assertion(
        kind="tool_arguments", value={"name": "book", "arguments": {"day": "Friday"}}
    )
    result = check_tool_assertion(
        assertion,
        [
            {"name": "book", "arguments": {"day": "Thursday"}},
            {"name": "book", "arguments": {"day": "Friday"}},
        ],
        {},
    )
    assert not result["passed"]
    assert len(result["actual"]) == 2


@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_call_count_is_a_nonnegative_integer(value):
    with pytest.raises(ValidationError):
        Assertion(kind="tool_call_count", value={"name": "book", "count": value})


def test_missing_state_is_not_equal_to_explicit_null():
    assertion = Assertion(kind="business_state_equals", key="booking.id", value=None)
    assert not check_tool_assertion(assertion, [], {})["passed"]
    assert check_tool_assertion(assertion, [], {"booking": {"id": None}})["passed"]


def test_business_booleans_are_not_numeric_success_values():
    assertion = Assertion(
        kind="tool_result", value={"name": "book", "result": {"booked": True}}
    )
    assert not check_tool_assertion(
        assertion, [{"name": "book", "result": {"booked": 1}}], {}
    )["passed"]


async def test_unknown_response_can_commit_and_be_reconciled_without_repeating_write():
    case = SimulationCase(
        name="lost response",
        user_messages=["book Friday"],
        assertions=[
            Assertion(kind="tool_call_count", value={"name": "book", "count": 1})
        ],
        fixtures=[
            {
                "tool_uuid": "a",
                "name": "book",
                "steps": [
                    {
                        "outcome": "unknown",
                        "state_patch": {"booking": {"id": "123", "day": "Friday"}},
                    }
                ],
            },
            {"tool_uuid": "b", "name": "lookup", "state_result_key": "booking"},
        ],
    )
    policy = EvaluationPolicy(case, None)
    assert (await policy.invoke_async("book", {}))["status"] == "unknown"
    assert policy.invoke("lookup", {}) == {"id": "123", "day": "Friday"}
    assert check_tool_assertion(
        case.assertions[0], policy.calls, policy.business_state
    )["passed"]
    assert policy.invoke("book", {}) == {"error": "fixture_steps_exhausted"}
    assert not check_tool_assertion(
        case.assertions[0], policy.calls, policy.business_state
    )["passed"]


def test_rejected_arguments_are_still_observable_attempts():
    case = SimulationCase(
        name="bad arguments",
        user_messages=["hello"],
        assertions=[{"kind": "tool_not_called", "value": "book"}],
        fixtures=[{"name": "book", "tool_uuid": "a", "arguments": {"day": "Friday"}}],
    )
    policy = EvaluationPolicy(case, None)
    policy.invoke("book", {"day": "Thursday"})
    assert not check_tool_assertion(case.assertions[0], policy.calls, {})["passed"]


def test_sequence_rejects_unexpected_side_effect():
    assertion = Assertion(kind="tool_sequence", value=["lookup", "book"])
    assert not check_tool_assertion(
        assertion, [{"name": "lookup"}, {"name": "book"}, {"name": "book"}], {}
    )["passed"]
