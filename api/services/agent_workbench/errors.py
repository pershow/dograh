class WorkbenchError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class LeaseLost(WorkbenchError):
    def __init__(self):
        super().__init__("Task cancelled or worker lease expired", 409)


class InvalidDecision(WorkbenchError):
    """A read-only model result failed validation, not a transport failure."""


class RecoverableValidationError(WorkbenchError):
    """A validated repair protocol supplies structured errors and progress evidence."""


class SDKTurnError(WorkbenchError):
    """An SDK failure already classified at the protocol boundary."""


class ModelTransportError(SDKTurnError):
    """A transport failure, eligible for bounded replay of pure decisions only."""

    code = "model_stream_interrupted"


def sdk_turn_error(turn):
    """Classify the SDK envelope at its boundary, not arbitrary tool error text."""
    error = turn.get("error") or {}
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    message = error.get("message", "") if isinstance(error, dict) else str(error)
    kind = next(iter(info), None) if isinstance(info, dict) else info
    detail = info.get(kind) or {} if isinstance(info, dict) else {}
    status = detail.get("httpStatusCode", detail.get("http_status_code"))
    transport = kind in {
        "responseStreamDisconnected",
        "response_stream_disconnected",
        "responseStreamConnectionFailed",
        "response_stream_connection_failed",
        "httpConnectionFailed",
        "http_connection_failed",
    } and (status is None or status == 429 or status >= 500)
    # The pinned SDK reports body-read errors as Other rather than a typed
    # stream error. Match only its transport envelope, never model/tool prose.
    transport |= kind in {None, "other"} and message.startswith(
        (
            "stream disconnected before completion: Transport error: network error:",
            "stream disconnected before completion: stream closed before response.completed",
            "stream disconnected before completion: idle timeout waiting for SSE",
        )
    )
    if turn.get("status") == "failed" and transport:
        return ModelTransportError(
            "Model stream interrupted before completion; incomplete output was not accepted.",
            503,
        )
    return SDKTurnError(
        "Codex turn " + str(turn.get("status")) + ": " + str(error)[:1200]
    )


def transient_model_error(error):
    """Only transport failures may restart a pure structured decision."""
    if isinstance(error, ModelTransportError):
        return True
    return not isinstance(
        error, (SDKTurnError, InvalidDecision, LeaseLost, RecoverableValidationError)
    ) and any(
        text in str(error)
        for text in (
            "Responses request did not complete",
            "without a complete response",
            "no data for",
            "connection interrupted",
            "I/O timeout",
            "connection timed out",
        )
    )


def validation_error_message(error):
    """Bounded field paths and rules, without echoing submitted data or contexts."""
    details = error.errors(
        include_input=False, include_context=False, include_url=False
    )
    messages = []
    for detail in details[:8]:
        path = "$"
        for part in detail["loc"]:
            path += f"[{part}]" if isinstance(part, int) else f".{part}"
        messages.append(f"{path[:200]}: {detail['msg'][:600]}")
    if len(details) > 8:
        messages.append(f"另有 {len(details) - 8} 项校验错误。")
    return "；".join(messages)[:2800]
