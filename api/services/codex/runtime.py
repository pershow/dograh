"""Resolve and verify Dograh's source-built Codex runtime, without app imports."""

import hashlib
import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path

SOURCE_LOCK = Path(__file__).with_name("source.json")
REPO_ROOT = Path(__file__).resolve().parents[3]


class CodexError(RuntimeError):
    """Runtime or protocol failure at the Codex adapter boundary."""


def default_binary() -> str:
    name = "codex-app-server.exe" if os.name == "nt" else "codex-app-server"
    return str(REPO_ROOT / ".runtime/codex" / name)


@lru_cache(maxsize=1)
def source_lock():
    return json.loads(SOURCE_LOCK.read_text())


def validate_runtime(binary: str) -> dict:
    """Reject missing, stale, or replaced builds before accepting work."""
    path = Path(binary).resolve()
    metadata = path.with_name("build.json")
    try:
        binary_stat, metadata_stat = path.stat(), metadata.stat()
    except OSError as error:
        raise CodexError(
            "Source-built Codex is missing. Run python scripts/build_codex.py."
        ) from error
    if not path.is_file() or not os.access(path, os.X_OK):
        raise CodexError("The configured Codex app-server is not executable.")
    return _validate(
        str(path),
        binary_stat.st_size,
        binary_stat.st_mtime_ns,
        metadata_stat.st_size,
        metadata_stat.st_mtime_ns,
    )


@lru_cache(maxsize=8)
def _validate(binary, _size, _mtime, _metadata_size, _metadata_mtime):
    path = Path(binary)
    try:
        manifest = json.loads(path.with_name("build.json").read_text())
        if not isinstance(manifest, dict):
            raise TypeError("Build record must be a JSON object.")
        if any(manifest.get(key) != value for key, value in source_lock().items()):
            raise CodexError("Codex build is from a different source pin; rebuild it.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if manifest.get("binary_sha256") != digest:
            raise CodexError(
                "Codex executable differs from its build record; rebuild it."
            )
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if result.stdout.strip() != f"codex-app-server {manifest['version']}":
            raise CodexError("Codex app-server version does not match the source pin.")
        return manifest
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        raise CodexError("Cannot verify the Codex source build; rebuild it.") from error
