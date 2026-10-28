"""Bounded transport diagnostics without credentials, headers or model payloads."""

import re
import traceback
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
    chain, seen, pending = [], set(), [error]
    while pending and len(chain) < 6:
        error = pending.pop(0)
        if error is None or id(error) in seen:
            continue
        seen.add(id(error))
        chain.append(
            {
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
        )
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions[:6])
        pending.append(
            error.__cause__
            or (None if error.__suppress_context__ else error.__context__)
        )
    return chain
