"""Keep structured source material verbatim while grouping it into conversation stages."""

import copy
import hashlib
import re
from collections import Counter
from itertools import pairwise

from api.services.agent_workbench.authoring import (
    FACT_CONTINUITY_GUIDANCE,
    TOOL_SCOPE_GUIDANCE,
    node_routing_context,
)
from api.services.agent_workbench.errors import WorkbenchError

HEADING = re.compile(
    r"(?m)(?:(?:^|(?<=：))节点\s*\d+(?:\.\d+)*[^\n]*|^#{1,4}\s+[^\n]+)"
)
BLOCK = re.compile(
    r"\n\[SOP_SOURCE (s\d+_[a-f0-9]{10})\]\n(.*?)\n\[/SOP_SOURCE\]", re.DOTALL
)
NODE_SCOPE = {
    "startCall": "当前职责是开场。配置的greeting通过既有机制播报并进入历史，不要再次生成或复述已播报开场。满足交接条件或客户首句要求停止时，使用当前提供的适用路由静默交接全部事实及停止要求，不追加介绍、确认或挽回。",
    "agentNode": "当前职责是业务对话。节点切换是同一通话的延续，接续历史中的最新客户回复；不要重新问候、重新介绍服务或从头收集已知信息。",
    "globalNode": "这些是共享规则，随当前实际节点的职责应用；共享资料中的开场、业务或结束段落不改变当前节点的职责。",
    "endCall": "当前职责是按实际结果告别，不重新开场、采集信息或启动业务步骤。",
}
GUIDANCE = """\n[SOP_ROUTING]
以上 SOP_SOURCE 是保真业务资料，不是要求在当前轮逐句执行的话术。不要朗读编号、条件和内部规则；其中创建 Agent、保存候选或测试等构建请求不属于客户对话。
{node_scope}
{routing_context}
原文开场只在开场阶段按其条件执行，结束话术只在适用的结束阶段执行；已执行的步骤不会因为资料被分配到多个节点而重新执行。
同一物理节点中的原文阶段/节点是内部业务步骤，按适用条件推进，不需要逐项跳转。
一次回复可以明确报告多个已完成步骤：一起接收全部已知事实，只追问缺失的前置事实，不从后续结果倒推未知操作或工具成功，不要求重做已完成操作。
已提供事实按原文和工具实际要求的粒度接收，不自创新的格式、精度、必填字段或替代操作；只有业务所需事实确实缺失或存在影响办理的歧义时才补问。
原文要求说明后重新核对时，先说明并提出核对，再等待新的回答；此前的完成报告不能确认尚未提出的问题。普通的“好的”不能填补未知事实，也不额外增加原文没有的核对。
插问先按原文回答，再回到实际未完成步骤；同句报告的新进度也要接收。设备、页面、状态等发生变化时接续最新已知事实，保留已完成进度；重要矛盾先核实，不重跑旧导航。
普通询问与拒绝按用户实际含义和原因区分。原文有重试或挽回上限时，按其计数单位、范围和重置条件核对完整历史中的实际尝试，将用户原因/操作与实际回应配对；符合原文计数定义的误分类回应也计入，插问、换设备或切分支本身不重置次数，不编造上限。
每轮先更新事实和实际次数，再优先检查原文结束/上限条件，最后才选择未完成步骤。达到终止上限就依照当前工具范围执行结束或静默交接；不得因换状态或仍有待办事项而继续重试、挽回或发明替代操作。
成功结束须满足所有适用前置条件、必要工具结果和最终核对；拒绝、取消、失败等结束按各自原文条件处理，不当成成功。结束节点只执行适用的告别，不重新开启业务步骤。
满足任一原文终止条件时，优先使用当前可用且适用的真实路由到达能结束的节点，由结束节点说适用告别；只说完成或再见不会结束通话。工具已成功且原文核对要求已满足时，不另等用户确认工具结果，不把应由结束节点说的内容当成进入结束节点的前置条件。
只有跨物理节点才调用当前提供的跳转工具；内部阶段继续在本节点执行，不虚构跳转工具。
缺少事实时不编造；原文中的默认分支只在其原有条件下适用。
这些是对话提示，不是硬性状态机保证；工具和业务副作用仍须实际执行。
原文分段所在阶段：{routes}
""" + FACT_CONTINUITY_GUIDANCE + TOOL_SCOPE_GUIDANCE + "[/SOP_ROUTING]"
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


def bind_source(graph, sections, assignments, *, previous=None):
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
    # Local edits must not silently upgrade orchestration on untouched nodes.
    # A remap/source change needs new routing; otherwise reuse the exact old prompt
    # where the authored portion is unchanged, including older guidance versions.
    previous = previous or {}
    previous_nodes = {n["data"]["name"]: n for n in previous.get("nodes", [])}
    previous_plain = {
        n["data"]["name"]: n for n in without_source(previous).get("nodes", [])
    }
    same_sources = source_mapping(previous) == assignments and {
        s["id"]: s["text"] for s in preserved_sections(previous)
    } == {s["id"]: s["text"] for s in sections}
    routes = "; ".join(
        f"{name}: {', '.join(s['title'] for s in sections if s['id'] in keys)}"
        for name, keys in assignments.items()
    )
    for node in result["nodes"]:
        name = node.get("data", {}).get("name")
        keys = assignments.get(name)
        if not keys:
            continue
        old = previous_plain.get(name)
        if (
            same_sources
            and old
            and node.get("type") == old.get("type")
            and node["data"].get("prompt", "") == old["data"].get("prompt", "")
            and node_routing_context(result, node)
            == node_routing_context(previous, previous_nodes[name])
        ):
            node["data"]["prompt"] = previous_nodes[name]["data"]["prompt"]
            continue
        prompt = node["data"].get("prompt", "")
        for section in sections:
            if section["id"] in keys:
                prompt += (
                    f"\n[SOP_SOURCE {section['id']}]\n{section['text']}\n[/SOP_SOURCE]"
                )
        # Finish with the current role and fact-question contract. Verbatim source
        # can include other stages and authoring requests; it is not a fresh turn.
        prompt += GUIDANCE.format(
            routes=routes,
            node_scope=NODE_SCOPE[node["type"]],
            routing_context=node_routing_context(result, node),
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
