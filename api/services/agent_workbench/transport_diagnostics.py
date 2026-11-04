"""Bounded transport diagnostics without credentials, headers or model payloads."""

import re
import traceback
from itertools import chain
from urllib.parse import urlsplit


def safe_message(error, secrets=()):
    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    # URLs may carry userinfo, API keys or signed query parameters.
    text = re.sub(
        r"(?:https?|socks[45]h?|socks4a)://[^\s'\"<>]+",
        lambda m: urlsplit(m[0]).hostname or "[url]",
        text,
    )
    text = re.sub(r"(?i)\b(bearer|basic)\s+[^\s,'\";]+", r"\1 [redacted]", text)
    text = re.sub(
        r"(?i)(authorization|api[_-]?key|token|password|cookie|secret)([\s'\"]*[:=][\s'\"]*)[^\s,;\n}\"]+",
        r"\1\2[redacted]",
        text,
    )
    return text[:600]


def exception_details(error, secrets=()):
    # TaskGroups can nest once per concurrency layer. Wrapper groups must not
    # consume the diagnostic budget before any actionable leaf is reached.
    root = error
    details, seen, pending = [], set(), [iter((error,))]
    visited = 0
    while pending and visited < 512:
        error = next(pending[-1], None)
        if error is None:
            pending.pop()
            continue
        visited += 1
        if id(error) in seen:
            continue
        seen.add(id(error))
        cause = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
        children = error.exceptions if isinstance(error, BaseExceptionGroup) else ()
        if not children:
            if len(details) == 6:
                break
            details.append(_exception_detail(error, secrets))
        pending.append(iter(chain(children, (cause,) if cause else ())))
    if not details:
        details.append(_exception_detail(root, secrets))
    if pending:
        details[-1]["truncated"] = True
    return details


def _exception_detail(error, secrets):
    return {
        "type": type(error).__name__,
        "message": safe_message(error, secrets),
        "frames": [
            {
                "file": f.filename.rsplit("/", 1)[-1],
                "function": f.name,
                "line": f.lineno,
            }
            for f in traceback.extract_tb(error.__traceback__)[-8:]
        ],
    }
