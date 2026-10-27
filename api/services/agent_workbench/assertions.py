"""Deterministic checks of tool attempts and simulated business effects."""

from copy import deepcopy

TOOL_ASSERTIONS = {
    "tool_called",
    "tool_not_called",
    "tool_call_count",
    "tool_arguments",
    "tool_result",
    "tool_sequence",
    "business_state_equals",
}


def json_equal(actual, expected):
    """Compare JSON values without Python's True == 1 coercion."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            json_equal(actual[key], expected[key]) for key in actual
        )
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            json_equal(a, b) for a, b in zip(actual, expected, strict=True)
        )
    return actual == expected


def state_value(state, path):
    value = state
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def check_tool_assertion(assertion, calls, business_state):
    kind, expected = assertion.kind, assertion.value
    if kind == "business_state_equals":
        found, actual = state_value(business_state, assertion.key)
        passed = found and json_equal(actual, expected)
    elif kind == "tool_sequence":
        actual = [call.get("name") for call in calls]
        # Exact order also rejects unexpected or repeated side effects.
        passed = actual == expected
    elif kind in {"tool_called", "tool_not_called"}:
        actual = sum(call.get("name") == expected for call in calls)
        passed = actual > 0 if kind == "tool_called" else actual == 0
    else:
        matching = [call for call in calls if call.get("name") == expected["name"]]
        if kind == "tool_call_count":
            actual = len(matching)
            passed = actual == expected["count"]
        else:
            key = "arguments" if kind == "tool_arguments" else "result"
            actual = [call.get(key) for call in matching]
            # Every invocation must match; a later correct retry cannot hide an
            # earlier incorrect mutation. Use count/order checks when needed.
            passed = bool(matching) and all(
                key in call and json_equal(call[key], expected[key])
                for call in matching
            )
    return {**assertion.model_dump(), "passed": passed, "actual": deepcopy(actual)}
