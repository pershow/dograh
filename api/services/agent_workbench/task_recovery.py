"""Durable retry policy for checkpointed test work, never arbitrary agent edits."""


def recoverable_test(kind, payload, output):
    return (
        kind == "build"
        and payload.get("mode") not in {"build", "analyze"}
        and (payload.get("mode") == "test" or output.get("intent") == "test")
        and output.get("outcome") not in {"build", "analysis", "review", "needs_input"}
    )


def retry_delay(attempt):
    return min(900, 30 * 2 ** (attempt - 1))
