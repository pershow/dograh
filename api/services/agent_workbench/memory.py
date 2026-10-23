"""Bounded model views over exact, source-backed cross-session memory."""

import hashlib

from api.services.agent_workbench.context import redact
from api.services.agent_workbench.sop import source_sections

MEMORY_GUIDANCE = """Agent memory is shared by this Agent's sessions only. Confirmed
entries are human-authored/confirmed requirements, not proof of implementation or
of external-world facts. They remain applicable until explicitly replaced/revoked.
The current revision supersedes ANY older thread summary or assistant claim.
Historical user sources are exact evidence: questions, examples, proposals and
hypotheticals are NOT confirmed decisions. Read relevant originals, resolve their
chronology and scope, and ask about genuine conflicts instead of silently merging
them. Never promote assistant statements/inferences into facts. There is no model
tool to confirm memory. A user can confirm, replace or revoke entries in Agent memory.
Before saving a candidate, read every confirmed entry in full; unread IDs are listed.
Use read_conversation(scope='agent_memory', memory_id=..., offset=...) for full text,
or source_id/source_type for user originals; query/offset searches and pages the
entire Agent archive. Other sessions' assistant conclusions are deliberately excluded.
Preserve original SOP text and branches; a summary never replaces source material.
If the current request conflicts with confirmed memory, explain the conflict and
request an explicit memory replacement; do not silently delete either requirement.
"""


def memory_sections(entries):
    result = []
    for entry in entries:
        if entry["kind"] != "sop":
            continue
        sections = source_sections(entry["content"])
        if not sections:
            text = entry["content"].strip()
            sections = [
                {
                    "id": "s001_" + hashlib.sha256(text.encode()).hexdigest()[:10],
                    "title": "SOP " + entry["id"],
                    "text": text,
                }
            ]
        # Different SOP documents can have the same section title. Do not merge by
        # title here: only an explicit human replacement retires a document.
        result.extend(sections)
    return list({s["id"]: s for s in result}.values())


def model_memory(snapshot, sources, attachments):
    remaining = 16000
    entries = []
    loaded = set()
    for row in snapshot["entries"]:
        item = {
            k: row[k]
            for k in (
                "id",
                "kind",
                "status",
                "content_hash",
                "created_at",
                "replaces_id",
            )
        }
        item["characters"] = len(row["content"])
        item["source"] = {k: v for k, v in row["source"].items() if k != "quote"}
        if len(row["content"]) <= min(4000, remaining):
            item["content"] = redact(row["content"])
            loaded.add(row["id"])
            remaining -= len(row["content"])
        else:
            item["preview"] = redact(row["content"][:200])
            item["read_required"] = True
        entries.append(item)
    return {
        "revision": snapshot["revision"],
        "workflow_id": snapshot["workflow_id"],
        "guidance": MEMORY_GUIDANCE,
        "confirmed": entries,
        "confirmed_count": len(entries),
        "id_guidance": "Only IDs in confirmed are memory_id. IDs in user_sources and attachments are source_id; these originals have NOT been confirmed as enduring decisions.",
        "user_sources": redact(sources),
        "attachments": redact(attachments),
    }, loaded


def source_index(sections):
    """Keep many individually small SOP sections from bypassing the context bound."""
    remaining = 24000
    result = []
    for section in sections:
        if len(section["text"]) <= remaining:
            result.append(section)
            remaining -= len(section["text"])
        else:
            result.append(
                {
                    "id": section["id"],
                    "title": section["title"],
                    "characters": len(section["text"]),
                    "truncated": True,
                    "guidance": "Read the full original using read_conversation(scope='agent_memory', sop_section_id=..., offset=...). Confirmed memory entries must also be read using their memory_id.",
                }
            )
    return result
