"""Keep structured source material verbatim while grouping it into conversation stages."""

import copy
import hashlib
import re
from collections import Counter
from itertools import pairwise

from api.services.agent_workbench.errors import WorkbenchError

HEADING = re.compile(
    r"(?m)(?:(?:^|(?<=：))节点\s*\d+(?:\.\d+)*[^\n]*|^#{1,4}\s+[^\n]+)"
)
BLOCK = re.compile(
    r"\n\[SOP_SOURCE (s\d+_[a-f0-9]{10})\]\n(.*?)\n\[/SOP_SOURCE\]", re.DOTALL
)
GUIDANCE = """\n[SOP_ROUTING]
以下是本阶段保留的原始业务资料。不要朗读编号、条件和内部规则。
同一阶段中的原文节点是内部步骤：按原文的顺序和条件推进；原文要求确认时等待对应问题的回答。
按原文处理插问和后续步骤；记录已确认信息，不把对另一问题的同意当成当前信息。
涉及其他阶段时仅调用当前提供的跳转工具，不自行执行其他阶段的话术。
缺少事实时不编造；原文中的默认分支只在其原有条件下适用。
原文分段所在阶段：{routes}
[/SOP_ROUTING]"""
ROUTING = re.compile(r"\n\[SOP_ROUTING\].*?\[/SOP_ROUTING\]", re.DOTALL)


def source_placeholders(sections):
    return sorted(
        {
            name
            for section in sections
            for name in re.findall(r"(?<!\{)\{(\w+)\}(?!\})", section["text"])
        }
    )


def source_sections(text):
    """Only structured SOPs are automatically bound; plain edit instructions are not."""
    headings = list(HEADING.finditer(text))
    if len(headings) < 2:
        return []
    starts = [0, *[m.start() for m in headings[1:]], len(text)]
    sections = []
    for index, (start, end) in enumerate(pairwise(starts), 1):
        body = text[start:end].strip()
        digest = hashlib.sha256(body.encode()).hexdigest()[:10]
        sections.append(
            {
                "id": f"s{index:03d}_{digest}",
                "title": headings[index - 1].group(),
                "text": body,
            }
        )
    return sections


def preserved_sections(graph):
    sections = {}
    for node in graph.get("nodes", []):
        for match in BLOCK.finditer(node.get("data", {}).get("prompt", "")):
            key, body = match.groups()
            if hashlib.sha256(body.encode()).hexdigest()[:10] != key.split("_")[1]:
                raise WorkbenchError("Stored SOP source integrity check failed")
            heading = HEADING.search(body)
            sections[key] = {
                "id": key,
                "title": heading.group() if heading else body.splitlines()[0],
                "text": body,
            }
    return sorted(sections.values(), key=lambda s: s["id"])


def merge_sections(previous, current):
    """A partial structured update must not silently erase unrelated prior sections."""
    updates = {section["title"]: section for section in current}
    merged = [updates.pop(section["title"], section) for section in previous]
    merged.extend(updates.values())
    return merged


def without_source(graph):
    result = copy.deepcopy(graph)
    for node in result.get("nodes", []):
        data = node.get("data", {})
        if "prompt" in data:
            data["prompt"] = ROUTING.sub("", BLOCK.sub("", data["prompt"]))
    return result


def source_mapping(graph):
    return {
        node["data"]["name"]: [
            match.group(1) for match in BLOCK.finditer(node["data"].get("prompt", ""))
        ]
        for node in graph.get("nodes", [])
        if BLOCK.search(node.get("data", {}).get("prompt", ""))
    }


def bind_source(graph, sections, assignments):
    """Reject omissions and wrong targets, then insert the source server-side."""
    if not sections:
        return graph
    if not assignments:
        raise WorkbenchError(
            "Map every SOP source section to a stage using source_sections; do not rewrite the source text."
        )
    nodes = graph.get("nodes", [])
    by_name = {}
    for node in nodes:
        by_name.setdefault(node.get("data", {}).get("name"), []).append(node)
    ids = [key for values in assignments.values() for key in values]
    expected = {section["id"] for section in sections}
    duplicates = sorted(
        {
            key
            for values in assignments.values()
            for key, count in Counter(values).items()
            if count > 1
        }
    )
    if set(ids) != expected or duplicates:
        raise WorkbenchError(
            f"SOP mapping must include every section. Shared rules may be assigned to multiple stages, but not repeated within one stage. Missing: {sorted(expected - set(ids))}; unknown: {sorted(set(ids) - expected)}; duplicated within a stage: {duplicates}"
        )
    for name in assignments:
        matches = by_name.get(name, [])
        if len(matches) != 1 or matches[0].get("type") not in {
            "startCall",
            "agentNode",
            "globalNode",
            "endCall",
        }:
            raise WorkbenchError(
                f"SOP stage {name!r} must be an existing, uniquely named conversation node"
            )
    result = without_source(graph)
    routes = "; ".join(
        f"{name}: {', '.join(s['title'] for s in sections if s['id'] in keys)}"
        for name, keys in assignments.items()
    )
    for node in result["nodes"]:
        keys = assignments.get(node.get("data", {}).get("name"))
        if not keys:
            continue
        prompt = node["data"].get("prompt", "") + GUIDANCE.format(routes=routes)
        for section in sections:
            if section["id"] in keys:
                prompt += (
                    f"\n[SOP_SOURCE {section['id']}]\n{section['text']}\n[/SOP_SOURCE]"
                )
        node["data"]["prompt"] = prompt
    return result


def coverage_report(graph, sections):
    stored = {s["id"]: s["text"] for s in preserved_sections(graph)}
    covered = [s["id"] for s in sections if stored.get(s["id"]) == s["text"]]
    return {
        "total": len(sections),
        "preserved": len(covered),
        "complete": len(covered) == len(sections),
    }
