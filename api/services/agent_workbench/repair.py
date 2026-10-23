"""Source-grounded repair diagnostics, independent of an agent's business domain."""

import copy

from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.graph_edits import preserve_graph
from api.services.workflow.dto import EdgeDataDTO, get_node_data_model

REPAIR_PLAN_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "properties": {
            "case": {"type": "string"},
            "assertion_index": {"type": "integer", "minimum": 0},
            "cause": {"type": "string", "minLength": 1},
            "nodes": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "source_node": {"type": "string"},
            "source_quote": {"type": "string", "minLength": 1},
            "correction": {"type": "string", "minLength": 1},
        },
        "required": [
            "case",
            "assertion_index",
            "cause",
            "nodes",
            "source_node",
            "source_quote",
            "correction",
        ],
        "additionalProperties": False,
    },
    "description": (
        "Ground each correction in a failing assertion and an exact quote from an "
        "original node prompt. Identify the state/evidence error, affected nodes and "
        "correction. Do not invent business facts or weaken tests."
    ),
}

EXECUTION_MARKER = "[WORKFLOW_EXECUTION_CONTRACT]"
EXECUTION_CONTRACT = """
[WORKFLOW_EXECUTION_CONTRACT]
内部执行约束，不对用户朗读：当前节点是非结束的对话步骤。
只扮演助手，不代替用户说操作完成的话。完成当前步骤不等于完成整个流程。
当原有出边的条件满足时，调用对应的跳转工具，由目标节点继续引导；不要只重复当前话术，也不要自行跨节点执行后续步骤。
在这个非结束节点不得提前说结束话术、告别或宣布整个任务完成。需要结束时按原有分支先进入结束节点，不能省略该分支要求完成的步骤。
用户插问时，依据原 SOP 已有事实回答，然后回到尚未完成的操作；不能把提问当作操作完成。
触发进入新节点的上一句回复，只证明此前步骤的条件满足。如果本节点原文要求新的确认，先完整说本节点原说明，再等待用户新回复，不能把上一句再次用作对新说明的确认。原文明确允许自动衔接的步骤仍按原文执行。
以上只规定执行方式，不改变原有业务事实、条件、例外和分支。
[/WORKFLOW_EXECUTION_CONTRACT]"""


def compile_execution_contracts(graph, source, diagnostics):
    """Compile graph execution rules into implicated stages, never business rules.

    The failed turn supplies node IDs, including intermediate nodes in a multi-hop
    turn. No matching on business names, expected phrases or domain values.
    """
    result = copy.deepcopy(graph)
    nodes = {str(n["id"]): n for n in result.get("nodes", [])}
    originals = {n["data"]["name"]: n for n in source.get("nodes", [])}
    plan = []
    for case in diagnostics:
        for failure in case["failures"]:
            observed = failure.get("observed_turn")
            if not observed and failure.get("kind") in {"script_consumed", "completed"}:
                observed = next(iter(reversed(case.get("timeline", []))), None)
            if not observed:
                continue
            involved = {str(observed.get(k)) for k in ("node_before", "node_after")}
            for transition in observed.get("transitions", []):
                involved.update(
                    str(transition.get(k))
                    for k in (
                        "node_id",
                        "to_node_id",
                        "previous_node_id",
                    )
                )
            for node_id in sorted(involved):
                node = nodes.get(node_id)
                if not node or node["type"] not in {"agentNode", "startCall"}:
                    continue
                name = node["data"]["name"]
                prompt = node["data"].get("prompt") or ""
                original = originals.get(name, {}).get("data", {}).get("prompt") or ""
                if EXECUTION_MARKER in prompt or not original:
                    continue
                node["data"]["prompt"] = prompt + EXECUTION_CONTRACT
                plan.append(
                    {
                        "case": case["case"],
                        "assertion_index": failure["assertion_index"],
                        "nodes": [name],
                        "source_node": name,
                        "source_quote": original[:160],
                        "cause": "Failed turn involves this stage's execution boundary.",
                        "correction": "Compile the generic nonterminal/transition/fresh-confirmation contract, preserving the source's own conditions and automatic-continuation exceptions.",
                    }
                )
    return result, plan


def failure_diagnostics(evaluation, graph=None):
    """Keep complete failing replies and associate transitions with their real turn.

    Older evaluations lack turn metadata; explicitly leave their routing unlocated
    instead of guessing a correspondence from the number of speech events.
    """
    graph = graph or {}
    names = {str(n["id"]): n["data"]["name"] for n in graph.get("nodes", [])}
    result = []
    for case in evaluation["results"]:
        if case.get("validity", {}).get("status") in {"invalid", "inconclusive"}:
            continue
        if case["status"] == "passed":
            continue
        failures = [
            {"assertion_index": i, **a}
            for i, a in enumerate(case.get("assertions", []))
            if not a.get("passed")
            and not a.get("informational")
            and (a.get("kind") != "reference" or a.get("repair_target") == "agent")
        ]
        if not failures:
            continue
        events = case.get("events", [])
        timeline = []
        active = None
        for message in case.get("transcript", []):
            if message["role"] != "assistant":
                continue
            turn = message.get("turn")
            at_turn = [e for e in events if turn is not None and e.get("turn") == turn]
            transitions = [
                e.get("payload", e.get("data", {}))
                for e in at_turn
                if e.get("type") == "node_transition"
            ]
            before = active
            for transition in transitions:
                active = transition.get("to_node_id") or transition.get("node_id")
            timeline.append(
                {
                    "turn": turn,
                    "user": next(
                        (
                            m["text"]
                            for m in case.get("transcript", [])
                            if m["role"] == "user" and m.get("turn") == turn
                        ),
                        None,
                    ),
                    "assistant": message["text"],
                    "node_before": before,
                    "node_after": active,
                    "transitions": transitions,
                    "transition_tools": [
                        e.get("payload", e.get("data", {}))
                        for e in at_turn
                        if e.get("type") == "tool_call_started"
                    ],
                }
            )
        result.append(
            {
                "case": case["name"],
                "status": case["status"],
                "failures": failures,
                "timeline": timeline,
                "unlocated_transitions": [
                    e.get("payload", e.get("data", {}))
                    for e in events
                    if e.get("type") == "node_transition" and "turn" not in e
                ],
                "error": case.get("error"),
            }
        )
        for failure in failures:
            observed = next(
                (
                    t
                    for t in timeline
                    if t["turn"] is not None and t["turn"] == failure.get("turn")
                ),
                None,
            )
            if observed:
                failure["observed_turn"] = {
                    **observed,
                    "node_before_name": names.get(str(observed["node_before"])),
                    "node_after_name": names.get(str(observed["node_after"])),
                }
            if failure["kind"] in {"contains", "contains_any"}:
                value = failure.get("value")
                terms = value if isinstance(value, list) else [str(value)]
                matches = [
                    {"node": n["data"]["name"], "prompt": n["data"].get("prompt", "")}
                    for n in graph.get("nodes", [])
                    if any(
                        term
                        and term.casefold()
                        in (n["data"].get("prompt") or "").casefold()
                        for term in terms
                    )
                ]
                failure["source_matches"] = matches
                failure["repair_guidance"] = (
                    "The missing content must be delivered at this actual turn. Use the matching "
                    "original facts in the active stage's reply; changing only a later stage "
                    "will not repair this assertion. If transitions is empty, no transition "
                    "occurred on this turn: do not diagnose an observed jump."
                )
    return result


def merge_repair_diagnostics(current, rejected):
    """Keep regressions from a rejected proposal actionable on the retained base."""
    result = copy.deepcopy(current)
    by_case = {c["case"]: c for c in result}
    for case in rejected or []:
        if case["case"] not in by_case:
            added = copy.deepcopy(case)
            added["evidence_origin"] = "rejected_attempt"
            result.append(added)
            by_case[case["case"]] = added
            continue
        target = by_case[case["case"]]
        known = {a["assertion_index"] for a in target["failures"]}
        for failure in case["failures"]:
            if failure["assertion_index"] in known:
                continue
            added = {**copy.deepcopy(failure), "evidence_origin": "rejected_attempt"}
            if not added.get("observed_turn") and added.get("kind") in {
                "script_consumed",
                "completed",
            }:
                added["observed_turn"] = next(
                    iter(reversed(case.get("timeline", []))), None
                )
            target["failures"].append(added)
    return result


def repair_graph_context(graph):
    """Plain fields avoid repeatedly sending a large escaped SDK representation."""
    names = {n["id"]: n["data"]["name"] for n in graph.get("nodes", [])}
    return {
        "nodes": [
            {"id": n["id"], "type": n["type"], **n["data"]}
            for n in graph.get("nodes", [])
        ],
        "transitions": [
            {"source": names[e["source"]], "target": names[e["target"]], **e["data"]}
            for e in graph.get("edges", [])
        ],
    }


def validate_repair(source, current, proposed, diagnostics, plan):
    """Automatic repair may clarify original instructions, never silently erase them."""
    preserve_graph(source, proposed)
    old = {n["data"]["name"]: n for n in source.get("nodes", [])}
    new = {n["data"]["name"]: n for n in proposed.get("nodes", [])}
    for name, node in old.items():
        for field in ("prompt", "greeting"):
            text = node["data"].get(field)
            if text and text not in (new[name]["data"].get(field) or ""):
                raise WorkbenchError(
                    f"Automatic repair must retain the original {field} of {name!r} verbatim. "
                    "Use prompt_append for clarifications. If the original requirement "
                    "contradicts a test, report_verification_issue instead of deleting it."
                )
    source_names = {n["id"]: n["data"]["name"] for n in source.get("nodes", [])}
    proposed_names = {n["id"]: n["data"]["name"] for n in proposed.get("nodes", [])}
    remaining = list(proposed.get("edges", []))
    for edge in source.get("edges", []):
        match = next(
            (
                e
                for e in remaining
                if (
                    proposed_names[e["source"]],
                    proposed_names[e["target"]],
                    e["data"]["label"],
                )
                == (
                    source_names[edge["source"]],
                    source_names[edge["target"]],
                    edge["data"]["label"],
                )
                and all(
                    not edge["data"].get(k)
                    or edge["data"][k] in (e["data"].get(k) or "")
                    for k in ("condition", "transition_speech")
                )
            ),
            None,
        )
        if match is None:
            raise WorkbenchError(
                f"Automatic repair must retain original condition/speech for branch {edge['data']['label']!r}; append a source-grounded clarification instead."
            )
        remaining.remove(match)
    if not plan:
        raise WorkbenchError("Automatic repair requires a source-grounded repair_plan")
    failures = {
        (c["case"], a["assertion_index"]) for c in diagnostics for a in c["failures"]
    }
    covered = set()
    for item in plan:
        if (item["case"], item["assertion_index"]) not in failures:
            raise WorkbenchError(
                "repair_plan must reference an actual failed assertion"
            )
        source_node = old.get(item["source_node"])
        quote = item["source_quote"]
        if (
            not source_node
            or not quote.strip()
            or quote not in (source_node["data"].get("prompt") or "")
        ):
            raise WorkbenchError(
                "repair_plan source_quote must match the original source_node prompt exactly"
            )
        if not set(item["nodes"]) <= set(new):
            raise WorkbenchError("repair_plan names an unknown node")
        covered.update(item["nodes"])
        failure = next(
            a
            for c in diagnostics
            if c["case"] == item["case"]
            for a in c["failures"]
            if a["assertion_index"] == item["assertion_index"]
        )
        observed = failure.get("observed_turn")
        if (
            failure.get("kind") in {"contains", "contains_any", "not_contains"}
            and observed
            and not observed["transitions"]
            and observed.get("node_after_name")
        ):
            relevant = {observed["node_after_name"]} | {
                name for name, node in new.items() if node["type"] == "globalNode"
            }
            if not relevant.intersection(item["nodes"]):
                raise WorkbenchError(
                    f"Repair is localized to the wrong stage. The failed reply was produced in "
                    f"{observed['node_after_name']!r}, with NO transition on turn {observed['turn']}. "
                    "Repair its response (or an applicable global instruction), using source_matches "
                    "for the original facts. A later node's reply cannot fix the missing content here."
                )
    previous = {n["data"]["name"]: n for n in current.get("nodes", [])}

    def node_data(node):
        return (
            get_node_data_model(node["type"]).model_validate(node["data"]).model_dump()
        )

    changed = {
        name
        for name, node in new.items()
        if name not in previous
        or node["type"] != previous[name]["type"]
        or node_data(node) != node_data(previous[name])
    }
    names = {n["id"]: n["data"]["name"] for n in proposed.get("nodes", [])}
    for edge in proposed.get("edges", []):
        if not any(
            all(edge.get(k) == e.get(k) for k in ("source", "target"))
            and EdgeDataDTO.model_validate(edge["data"])
            == EdgeDataDTO.model_validate(e["data"])
            for e in current.get("edges", [])
        ):
            changed.add(names[edge["source"]])
    if not changed <= covered:
        raise WorkbenchError(
            f"repair_plan must explain every changed node: {sorted(changed - covered)}"
        )
