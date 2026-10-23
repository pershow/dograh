"""Immutable human-source evidence shared by building, review and repair.

Source ancestry is independent of generated prompts. A model interpretation is
never written back as confirmed Agent memory. Large originals remain pageable.
"""

from copy import deepcopy

from api.db import db_client
from api.services.agent_workbench.attachments import read_selected_attachment
from api.services.agent_workbench.context import redact
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint

GUIDANCE = """The task basis contains original human sources, NOT model conclusions.
The current_request defines what to do now. Earlier user requests are version
provenance and continuing constraints, not a queue of tasks to execute again.
Material format does not establish authority: distinguish business facts, required
steps/conditions/prohibitions, quality goals, examples, questions and hypotheses.
Documents can contain several of these roles. A reference conversation, prior audit
or model suggestion is not mandatory policy unless explicitly adopted by the user.
Use original quotations and source IDs. A generated candidate is the object being
checked, never an independent source of business truth. Confirmed memory supersedes
older interpretations; explicit replacements retire only the affected requirement.
If sources conflict and no explicit replacement resolves it, report the conflict and
uncertainty, never invent a compromise. Missing business information is not permission
to invent prices, promises or conditions. Routine implementation choices are allowed.
For agent-wide requests inspect all applicable nodes, branches and global instructions.
Preserve unaffected source content and branches. Node count is not an objective.
"""


def summary(basis):
    return {
        "version": 1,
        "fingerprint": basis["fingerprint"],
        "memory_revision": basis["memory_revision"],
        "target_fingerprint": basis["target_fingerprint"],
        "sources": [
            {
                k: s.get(k)
                for k in (
                    "id",
                    "title",
                    "kind",
                    "source_id",
                    "session_id",
                    "created_at",
                    "sha256",
                    "content_hash",
                    "characters",
                    "available",
                    "reason",
                )
            }
            for s in basis["sources"]
        ],
        "warnings": basis["warnings"],
    }


def source_records(basis):
    """Stable, exact text spans; quotations can be bound without model rewriting."""
    records = []
    for source in basis.get("sources", []):
        text = source.get("text", "")
        for offset in range(0, len(text), 12000):
            records.append(
                {
                    "id": f"basis:{source['id']}:{offset}",
                    "source": "basis",
                    "text": text[offset : offset + 12000],
                    "source_id": source["id"],
                    "source_kind": source["kind"],
                    "title": source["title"],
                    "offset": offset,
                    "content_hash": source["content_hash"],
                    "created_at": source.get("created_at"),
                }
            )
    return records


def source_page(basis, source_id=None, offset=0, limit=12000):
    if not basis:
        raise WorkbenchError("本任务尚未整理资料依据。", 404)
    if source_id is None:
        return {**summary(basis), "guidance": GUIDANCE}
    if offset < 0 or not 1 <= limit <= 24000:
        raise WorkbenchError("资料页范围无效。", 422)
    source = next((s for s in basis["sources"] if s["id"] == source_id), None)
    if source is None:
        raise WorkbenchError("本任务没有该来源。", 404)
    end = min(len(source.get("text", "")), offset + limit)
    return {
        **{k: v for k, v in source.items() if k != "text"},
        "text": source.get("text", "")[offset:end],
        "offset": offset,
        "next_offset": end if end < len(source.get("text", "")) else None,
    }


async def discover_originals(org, sid, job, target, sources, llm, event, protocol):
    """Recover legacy/imported versions without treating all old tasks as active."""
    from api.services.agent_workbench.test_tasks import decision, object_schema

    index = []
    known = {s["id"] for s in sources}
    seen = set()
    for kind in ("request", "attachment"):
        offset = 0
        while True:
            page = await db_client.wb_memory_sources(
                org, sid, source_type=kind, offset=offset, limit=60
            )
            for item in page["items"]:
                key = kind + ":" + item["id"]
                if (
                    key in known
                    or key == "request:" + job["id"]
                    or item["content_hash"] in seen
                    or (
                        job.get("created_at") and item["created_at"] > job["created_at"]
                    )
                ):
                    continue
                seen.add(item["content_hash"])
                index.append({**item, "key": key})
            offset = page["next_offset"]
            if offset is None:
                break
    if not index:
        return []
    selected = []
    # This is metadata selection, not a requirement summary. Every chosen source
    # is subsequently read in full and retained verbatim for both reviewers.
    for offset in range(0, len(index), 60):
        page = index[offset : offset + 60]
        choice = await decision(
            "select_business_sources",
            GUIDANCE
            + "\nSelect original business material/requirements relevant to this Agent's current task, including original creation material from older sessions. Exclude requests merely to run tests, inspect results, report status, previous audits and reference-dialogue spreadsheets. Do not exclude original business rules just because the current task is test-only. Selection is retrieval, not confirmation. Return empty if nothing relevant; do not invent sources.",
            object_schema(
                {
                    "source_ids": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string", "enum": [s["key"] for s in page]},
                    }
                }
            ),
            {
                "current_request": job["input"].get("text")
                or job["input"].get("instruction"),
                "agent_name": target.get("name"),
                "node_names": [
                    n.get("data", {}).get("name")
                    for n in target.get("workflow_json", {}).get("nodes", [])
                ],
                "original_source_index": page,
            },
            llm,
            event,
            protocol,
        )
        selected.extend(choice["source_ids"])
    recovered = []
    for item in index:
        if item["key"] not in selected:
            continue
        original = await db_client.wb_memory_source(
            org, sid, item["source_type"], item["id"]
        )
        recovered.append(
            {
                "id": item["key"],
                "kind": "material"
                if item["source_type"] == "attachment"
                else "user_request",
                "title": item["title"],
                "text": original["content"],
                "source_id": item["id"],
                "session_id": item["session_id"],
                "created_at": item["created_at"],
                "relevance": "retrieved_original_not_confirmed_memory",
            }
        )
    return recovered


async def ensure_basis(
    org,
    sid,
    job,
    target,
    *,
    reference_asset_ids=(),
    llm=None,
    event=None,
    protocol="chat",
):
    """Freeze once per task; resumes and candidate repair use the same originals."""
    cached = job.get("_task_basis") or await db_client.wb_task_basis(
        org, sid, job["id"]
    )
    if cached:
        job["_task_basis"] = cached
        return cached
    target_hash = fingerprint(target)
    sources = await db_client.wb_basis_origins(org, sid, job["id"], target_hash)
    if llm is not None:

        async def quiet_event(_kind, _payload):
            pass

        sources.extend(
            await discover_originals(
                org, sid, job, target, sources, llm, event or quiet_event, protocol
            )
        )
    memory = await db_client.wb_memory(org, sid)
    for entry in memory["entries"]:
        sources.append(
            {
                "id": "memory:" + entry["id"],
                "kind": "memory",
                "title": "已确认的 Agent 要求",
                "text": entry["content"],
                "source_id": entry["id"],
                "session_id": entry["session_id"],
                "created_at": entry["created_at"],
                "provenance": entry["source"],
            }
        )
    request = job["input"].get("text") or job["input"].get("instruction", "")
    sources.append(
        {
            "id": "request:" + job["id"],
            "kind": "current_request",
            "title": "本次要求",
            "text": request,
            "session_id": sid,
            "source_id": job["id"],
            "created_at": job.get("created_at"),
        }
    )
    for aid in job["input"].get("asset_ids", []):
        asset = await db_client.wb_get(org, sid, "assets", aid)
        sources.append(
            {
                "id": "attachment:" + aid,
                "kind": "material",
                "title": asset["name"],
                "session_id": sid,
                "source_id": aid,
                "sha256": asset["sha256"],
                "created_at": asset["created_at"],
            }
        )
    sources = list({s["id"]: deepcopy(s) for s in sources}.values())
    warnings = []
    for source in sources:
        source["available"] = True
        if source["kind"] == "material":
            aid = source["source_id"]
            if aid in reference_asset_ids:
                source.update(
                    kind="reference",
                    text="",
                    reason="测试样本逐条进入评审，不作为业务规则自动导入。",
                )
            else:
                pages, offset = [], 0
                while True:
                    page = await read_selected_attachment(
                        org, source["session_id"], [aid], aid, offset, 24000
                    )
                    if page["kind"] == "image":
                        source.update(
                            available=False,
                            reason="图片需要单独读取并核对，不能用未经核验的识别结果判定业务失败。",
                        )
                        break
                    pages.append(page["text"])
                    offset = page["next_offset"]
                    if offset is None:
                        break
                source["text"] = "".join(pages)
        source["text"] = redact(source.get("text", ""))
        source["characters"] = len(source["text"])
        source["content_hash"] = fingerprint(source["text"])
        if not source["available"]:
            warnings.append(f"{source['title']}：{source['reason']}")
    if not any(
        s["text"]
        and s["kind"] in {"material", "memory", "user_request", "current_request"}
        for s in sources
    ):
        warnings.append(
            "未找到此版本的独立业务原始资料；仅凭生成配置不能证明业务要求已经满足。"
        )
    basis = {
        "version": 1,
        "target_fingerprint": target_hash,
        "memory_revision": memory["revision"],
        "sources": sources,
        "warnings": warnings,
    }
    basis["fingerprint"] = fingerprint(basis)
    basis = await db_client.wb_freeze_basis(
        org, sid, job["id"], job["lease_token"], basis, summary(basis)
    )
    job["_task_basis"] = basis
    job.setdefault("output", {})["task_basis"] = summary(basis)
    return basis
