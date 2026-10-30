from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import test_tasks
from api.services.agent_workbench.errors import (
    InvalidDecision,
    LeaseLost,
    ModelTransportError,
    WorkbenchError,
    sdk_turn_error,
    transient_model_error,
)


@pytest.mark.parametrize(
    "info,message",
    [
        ({"responseStreamDisconnected": {"httpStatusCode": None}}, "disconnected"),
        ({"responseStreamConnectionFailed": {"httpStatusCode": 502}}, "bad gateway"),
        (
            "other",
            "stream disconnected before completion: Transport error: network error: error decoding response body",
        ),
        (
            "other",
            "stream disconnected before completion: stream closed before response.completed",
        ),
        (
            "other",
            "stream disconnected before completion: idle timeout waiting for SSE",
        ),
    ],
)
def test_sdk_transport_envelope_is_typed(info, message):
    error = sdk_turn_error(
        {"status": "failed", "error": {"codexErrorInfo": info, "message": message}}
    )
    assert isinstance(error, ModelTransportError)
    assert transient_model_error(error)
    assert error.code == "model_stream_interrupted"


@pytest.mark.parametrize(
    "status,info,message",
    [
        ("interrupted", "other", "connection interrupted"),
        ("failed", "unauthorized", "connection interrupted"),
        (
            "interrupted",
            "other",
            "stream disconnected before completion: Transport error: network error: reset",
        ),
        (
            "failed",
            {"responseStreamDisconnected": {"httpStatusCode": 403}},
            "access denied",
        ),
        ("failed", "unauthorized", "stream disconnected before completion"),
        ("failed", "contextWindowExceeded", "too large"),
        ("failed", "sessionBudgetExceeded", "budget"),
        (
            "failed",
            "other",
            "stream disconnected before completion: Model output reached the 128 token limit",
        ),
        ("failed", "other", "Model tool arguments failed validation"),
    ],
)
def test_sdk_permanent_and_cancelled_failures_never_become_transport(
    status, info, message
):
    error = sdk_turn_error(
        {"status": status, "error": {"codexErrorInfo": info, "message": message}}
    )
    assert not isinstance(error, ModelTransportError)
    assert not transient_model_error(error)


@pytest.mark.parametrize(
    "failure", [InvalidDecision("bad source"), LeaseLost(), WorkbenchError("HTTP 403")]
)
async def test_pure_decision_does_not_retry_permanent_errors(monkeypatch, failure):
    run = AsyncMock(side_effect=failure)
    monkeypatch.setattr(test_tasks.CodexClient, "run", run)
    with pytest.raises(type(failure)):
        await test_tasks.decision("submit", "rules", {}, {}, None, AsyncMock())
    assert run.await_count == 1


async def test_accepted_result_survives_lost_ack_without_resubmission(monkeypatch):
    calls = []

    async def run(client, *_args, **_kwargs):
        calls.append(client)
        client.dispatcher.report = {"answer": "accepted"}
        raise ModelTransportError("stream lost after receipt")

    monkeypatch.setattr(test_tasks.CodexClient, "run", run)
    assert await test_tasks.decision("submit", "rules", {}, {}, None, AsyncMock()) == {
        "answer": "accepted"
    }
    assert len(calls) == 1
