"""Lossless storage of shared compilation evidence, separate from case payloads.

Services always consume the original, fully hydrated suite. Only persistence
uses references; neither source text nor assertions are truncated to fit a batch.
"""

import json
from copy import deepcopy

from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint

MAX_PAYLOAD_BYTES = 256000
SHARED_FIELDS = frozenset(
    {"requirements", "sources", "environment", "rubric_questions", "question_review"}
)
REFERENCE_KEY = "_shared_artifacts"


def payload_bytes(value):
    return len(json.dumps(value, ensure_ascii=False).encode())


def split_suite(payload):
    """Return bounded storage payload and content-addressed immutable artifacts."""
    stored, artifacts = deepcopy(payload), {}
    for case in stored.get("cases", []):
        compiled = case.get("compiled")
        if not compiled:
            continue
        if REFERENCE_KEY in compiled:
            raise WorkbenchError(
                "测试依据引用必须先完整读取并校验，不能直接提交存储引用。"
            )
        refs = {}
        for key in sorted(SHARED_FIELDS & compiled.keys()):
            value = compiled.pop(key)
            if payload_bytes(value) > MAX_PAYLOAD_BYTES:
                raise WorkbenchError(
                    f"Shared compilation artifact {key} exceeds 256 KB"
                )
            digest = fingerprint(value)
            artifacts[digest] = value
            refs[key] = digest
        if refs:
            compiled[REFERENCE_KEY] = refs
    return stored, artifacts


def artifact_ids(payload):
    ids = set()
    for case in payload.get("cases", []):
        compiled = case.get("compiled") or {}
        if REFERENCE_KEY not in compiled:
            continue
        refs = compiled[REFERENCE_KEY]
        if (
            not isinstance(refs, dict)
            or not refs
            or not refs.keys() <= SHARED_FIELDS
            or refs.keys() & compiled.keys()
            or any(
                not isinstance(d, str)
                or len(d) != 64
                or any(c not in "0123456789abcdef" for c in d)
                for d in refs.values()
            )
        ):
            raise WorkbenchError("冻结测试依据的存储引用无效。", 409)
        ids.update(refs.values())
    return ids


def hydrate_suite(payload, artifacts):
    for digest in artifact_ids(payload):
        if digest not in artifacts or fingerprint(artifacts[digest]) != digest:
            raise WorkbenchError("冻结测试依据缺失或完整性校验失败，不能执行。", 409)
    hydrated = deepcopy(payload)
    for case in hydrated.get("cases", []):
        compiled = case.get("compiled") or {}
        for key, digest in compiled.pop(REFERENCE_KEY, {}).items():
            compiled[key] = deepcopy(artifacts[digest])
    return hydrated
