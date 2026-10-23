"""Run with: python -m api.services.codex (no database or model credentials)."""

import asyncio
import os
import sys

from .probe import probe_runtime
from .runtime import CodexError, default_binary, source_lock


def main():
    binary = os.getenv("WORKBENCH_CODEX_BINARY") or default_binary()
    try:
        asyncio.run(probe_runtime(binary))
    except (CodexError, OSError) as error:
        print(f"Codex startup check failed: {error}", file=sys.stderr)
        return 1
    print(
        f"Codex {source_lock()['version']} source build and app-server protocol verified."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
