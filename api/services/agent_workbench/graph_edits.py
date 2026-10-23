"""Generic preservation checks and actionable diagnostics for local graph edits."""

import copy
import json
from collections import Counter

from api.services.agent_workbench.errors import WorkbenchError
from api.services.workflow.dto import EdgeDataDTO
from api.services.workflow.node_specs import get_spec


def update_fields(graph, node_updates, edge_updates):
    """Apply named field patches atomically, without SDK quoting or graph replacement."""
    result = copy.deepcopy(graph)
    nodes = result.get("nodes", [])
    names = {n["id"]: n["data"]["name"] for n in nodes}
    for update in node_updates or []:
        matches = [n for n in nodes if n["data"]["name"] == update["name"]]
        if len(matches) != 1:
            raise WorkbenchError(
                f"Node name {update['name']!r} must match exactly one node"
            )
        node = matches[0]
        data = update.get("data", {})
        known = {p.name for p in get_spec(node["type"]).properties}
        if set(data) - known:
            raise WorkbenchError(
                f"Unknown fields for {update['name']!r}: {sorted(set(data) - known)}"
            )
        node["data"].update(copy.deepcopy(data))
        for edit in update.get("prompt_edits", []):
            prompt = node["data"].get("prompt", "")
            count = prompt.count(edit["old"]) if edit["old"] else 0
            if count != 1:
                raise WorkbenchError(
                    f"Prompt edit for {update['name']!r} matched {count} times; use the exact plain prompt text, without SDK escaping"
                )
            node["data"]["prompt"] = prompt.replace(edit["old"], edit["new"], 1)
        if update.get("prompt_append"):
            if "prompt" not in known:
                raise WorkbenchError(f"Node {update['name']!r} has no prompt field")
            node["data"]["prompt"] = (
                node["data"].get("prompt", "") + "\n" + update["prompt_append"]
            )
    for update in edge_updates or []:
        matches = [
            e
            for e in result.get("edges", [])
            if (names[e["source"]], names[e["target"]], e["data"]["label"])
            == (update["source"], update["target"], update["label"])
        ]
        if len(matches) != 1:
            raise WorkbenchError(
                f"Transition {update['source']!r} / {update['label']!r} / {update['target']!r} must match exactly one branch"
            )
        if set(update["data"]) - set(EdgeDataDTO.model_fields):
            raise WorkbenchError("Unknown transition fields")
        matches[0]["data"].update(copy.deepcopy(update["data"]))
    return result


def transitions(graph):
    names = {n["id"]: n["data"]["name"] for n in graph.get("nodes", [])}
    return Counter(
        (names[e["source"]], names[e["target"]], e["data"]["label"])
        for e in graph.get("edges", [])
    )


def preserve_graph(previous, proposed, removals=None):
    """Omission is not a deletion instruction; require explicit, exact removals."""
    removals = removals or {}
    nodes = Counter(n["data"]["name"] for n in previous.get("nodes", []))
    remaining = Counter(n["data"]["name"] for n in proposed.get("nodes", []))
    missing_nodes = nodes - remaining
    missing_edges = transitions(previous) - transitions(proposed)
    declared_nodes = Counter(removals.get("nodes", []))
    declared_edges = Counter(
        (e["source"], e["target"], e["label"]) for e in removals.get("transitions", [])
    )
    if missing_nodes != declared_nodes or missing_edges != declared_edges:
        details = {
            "missing_nodes": list(missing_nodes.elements()),
            "missing_transitions": [
                dict(zip(("source", "target", "label"), edge))
                for edge in missing_edges.elements()
            ],
        }
        raise WorkbenchError(
            "Original graph content was removed. Restore omitted nodes/branches with local edits. "
            "Only when the request requires deletion or rerouting, declare the exact removals "
            "and explain the requirement in summary. "
            + json.dumps(details, ensure_ascii=False)
        )


def graph_diagnostics(errors, graph, previous):
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = {e["id"]: e for e in graph.get("edges", [])}
    previous_names = {n["id"]: n["data"]["name"] for n in previous.get("nodes", [])}
    result = []
    for error in errors[:5]:
        item = dict(error)
        node = nodes.get(error.get("id")) if error.get("kind") == "node" else None
        edge = edges.get(error.get("id")) if error.get("kind") == "edge" else None
        if node:
            item["node_name"] = node["data"]["name"]
            if "incoming" in error["message"]:
                item["original_incoming"] = [
                    {"source": previous_names[e["source"]], **e["data"]}
                    for e in previous.get("edges", [])
                    if previous_names[e["target"]] == item["node_name"]
                ][:4]
        if edge:
            item.update(
                source=nodes[edge["source"]]["data"]["name"],
                target=nodes[edge["target"]]["data"]["name"],
                label=edge["data"]["label"],
            )
        result.append(item)
    return (
        "Fix these specific nodes/transitions with save_candidate.edits; the failed code "
        "is retained. Do not regenerate the workflow or invent connections. "
        + json.dumps(
            {"total_errors": len(errors), "errors": result}, ensure_ascii=False
        )
    )
