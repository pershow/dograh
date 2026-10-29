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


def transient_model_error(error):
    """Only transport failures may restart a pure structured decision."""
    return not isinstance(error, (InvalidDecision, LeaseLost)) and any(
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
