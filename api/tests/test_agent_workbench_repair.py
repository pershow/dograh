"""Generic repair contracts: no domain-specific node names, values or scripts."""

import copy

import pytest

from api.services.agent_workbench.config import WorkbenchSettings
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import acceptable_improvement
from api.services.agent_workbench.repair import (
    EXECUTION_MARKER,
    compile_execution_contracts,
    failure_diagnostics,
    merge_repair_diagnostics,
    validate_repair,
)


def test_repair_rounds_default_to_convergence_and_honor_explicit_deployment_cap(
    monkeypatch,
):
    monkeypatch.delenv("WORKBENCH_BUILD_REPAIR_ROUNDS", raising=False)
    assert WorkbenchSettings.load().build_repair_rounds is None
    monkeypatch.setenv("WORKBENCH_BUILD_REPAIR_ROUNDS", "")
    assert WorkbenchSettings.load().build_repair_rounds is None
    monkeypatch.setenv("WORKBENCH_BUILD_REPAIR_ROUNDS", "0")
    assert WorkbenchSettings.load().build_repair_rounds == 0
    monkeypatch.setenv("WORKBENCH_BUILD_REPAIR_ROUNDS", "5")
    assert WorkbenchSettings.load().build_repair_rounds == 5


def test_diagnostics_locate_multi_hop_and_waiting_turns_without_truncating_replies():
    reply = "Instructions " * 1000 + "must wait for the next reply."
    result = {
        "name": "handoff",
        "status": "failed",
        "assertions": [
            {"kind": "not_contains", "turn": 2, "value": "goodbye", "passed": False},
        ],
        "transcript": [
            {"role": "assistant", "text": "start", "turn": 0},
            {"role": "user", "text": "ready", "turn": 1},
            {"role": "assistant", "text": reply, "turn": 1},
            {"role": "user", "text": "question", "turn": 2},
            {"role": "assistant", "text": "goodbye", "turn": 2},
        ],
        "events": [
            {"type": "node_transition", "turn": 0, "payload": {"node_id": "A"}},
            {"type": "node_transition", "turn": 1, "payload": {"node_id": "B"}},
            {"type": "node_transition", "turn": 1, "payload": {"node_id": "C"}},
            {
                "type": "tool_call_started",
                "turn": 1,
                "payload": {"function_name": "advance"},
            },
            {"type": "node_transition", "payload": {"node_id": "legacy"}},
        ],
    }
    diagnostic = failure_diagnostics({"results": [result]})[0]
    assert diagnostic["failures"][0]["assertion_index"] == 0
    first, second, last = diagnostic["timeline"]
    assert first["node_after"] == "A"
    assert second["node_before"] == "A" and second["node_after"] == "C"
    assert second["assistant"] == reply and second["user"] == "ready"
    assert second["transition_tools"] == [{"function_name": "advance"}]
    assert last["node_before"] == last["node_after"] == "C"
    assert diagnostic["unlocated_transitions"] == [{"node_id": "legacy"}]


def test_assertion_progress_is_accepted_without_case_pass_but_never_regression():
    old = {
        "results": [
            {
                "name": "route",
                "status": "failed",
                "assertions": [
                    {"kind": "contains", "value": value, "passed": passed}
                    for value, passed in [
                        ("one", True),
                        ("two", False),
                        ("three", False),
                    ]
                ],
            }
        ]
    }
    new = copy.deepcopy(old)
    new["results"][0]["assertions"][1]["passed"] = True
    assert acceptable_improvement(old, new)
    assert not acceptable_improvement(new, new)
    new["results"][0]["assertions"][0]["passed"] = False
    assert not acceptable_improvement(old, new)
    new["results"][0]["assertions"][0]["passed"] = True
    new["results"][0]["assertions"][1]["value"] = "weakened test"
    assert not acceptable_improvement(old, new)


def test_missing_reply_content_is_grounded_in_actual_stage_and_source():
    graph = {
        "nodes": [
            {
                "id": "1",
                "type": "agentNode",
                "data": {
                    "name": "Input",
                    "prompt": "Ask for the code. Wait for input.",
                },
            },
            {
                "id": "2",
                "type": "agentNode",
                "data": {"name": "Details", "prompt": "The allowed code is ZX-42."},
            },
        ],
        "edges": [
            {
                "id": "edge",
                "source": "1",
                "target": "2",
                "data": {
                    "label": "confirmed",
                    "condition": "The input is confirmed",
                },
            }
        ],
    }
    diagnostic = failure_diagnostics(
        {
            "results": [
                {
                    "name": "interruption",
                    "status": "failed",
                    "assertions": [
                        {
                            "kind": "contains",
                            "value": "ZX-42",
                            "turn": 1,
                            "passed": False,
                        }
                    ],
                    "transcript": [
                        {"role": "assistant", "text": "Enter the code", "turn": 0},
                        {"role": "user", "text": "What code?", "turn": 1},
                        {"role": "assistant", "text": "Please enter it", "turn": 1},
                    ],
                    "events": [
                        {
                            "type": "node_transition",
                            "turn": 0,
                            "payload": {"node_id": "1"},
                        }
                    ],
                }
            ]
        },
        graph,
    )
    failure = diagnostic[0]["failures"][0]
    assert failure["source_matches"] == [
        {"node": "Details", "prompt": "The allowed code is ZX-42."}
    ]
    assert failure["observed_turn"]["node_after_name"] == "Input"
    assert failure["observed_turn"]["transitions"] == []
    plan = [
        {
            "case": "interruption",
            "assertion_index": 0,
            "nodes": ["Details"],
            "source_node": "Details",
            "source_quote": "The allowed code is ZX-42.",
            "cause": "Missing content",
            "correction": "Explain the allowed code.",
        }
    ]
    changed = copy.deepcopy(graph)
    changed["nodes"][1]["data"]["prompt"] += " Explain the code."
    with pytest.raises(WorkbenchError, match="wrong stage"):
        validate_repair(graph, graph, changed, diagnostic, plan)
    changed = copy.deepcopy(graph)
    changed["nodes"][0]["data"]["prompt"] += (
        " If asked, explain the allowed code is ZX-42, then wait for input."
    )
    plan[0]["nodes"] = ["Input"]
    validate_repair(graph, graph, changed, diagnostic, plan)


def test_rejected_regressions_remain_actionable_without_replacing_current_evidence():
    current = [
        {
            "case": "route",
            "failures": [{"assertion_index": 0, "kind": "contains"}],
            "timeline": [{"turn": 9, "node_after": "base"}],
        }
    ]
    rejected = [
        {
            "case": "route",
            "failures": [
                {"assertion_index": 0, "kind": "contains"},
                {"assertion_index": 1, "kind": "script_consumed"},
            ],
            "timeline": [{"turn": 7, "node_after": "early-end"}],
        }
    ]
    result = merge_repair_diagnostics(current, rejected)
    assert result[0]["failures"][0] == current[0]["failures"][0]
    assert result[0]["failures"][1]["observed_turn"]["turn"] == 7
    assert result[0]["failures"][1]["evidence_origin"] == "rejected_attempt"
    assert result[0]["timeline"][0]["turn"] == 9
    assert len(current[0]["failures"]) == 1


@pytest.mark.parametrize(
    "name,source_text", [("Input", "Use code ZX-42."), ("预约", "确认八点半预约。")]
)
def test_execution_contract_compiles_from_trace_without_domain_matching(
    name, source_text
):
    graph = {
        "nodes": [
            {
                "id": "a",
                "type": "agentNode",
                "data": {"name": name, "prompt": source_text},
            },
            {
                "id": "b",
                "type": "agentNode",
                "data": {"name": "Confirm", "prompt": "Ask for a new confirmation."},
            },
            {
                "id": "z",
                "type": "endCall",
                "data": {"name": "End", "prompt": "Goodbye."},
            },
            {
                "id": "other",
                "type": "agentNode",
                "data": {"name": "Other route", "prompt": "Unrelated route."},
            },
        ],
        "edges": [],
    }
    diagnostics = [
        {
            "case": "boundary",
            "failures": [
                {"assertion_index": 0, "kind": "script_consumed"},
            ],
            "timeline": [
                {
                    "turn": 3,
                    "node_before": "a",
                    "node_after": "z",
                    "transitions": [
                        {"node_id": "b", "previous_node_id": "a"},
                        {"node_id": "z", "previous_node_id": "b"},
                    ],
                }
            ],
        }
    ]
    original = copy.deepcopy(graph)
    patched, plan = compile_execution_contracts(graph, graph, diagnostics)
    assert graph == original
    assert [p["nodes"] for p in plan] == [[name], ["Confirm"]]
    for old, new in zip(graph["nodes"], patched["nodes"]):
        assert new["data"]["prompt"].startswith(old["data"]["prompt"])
        assert (EXECUTION_MARKER in new["data"]["prompt"]) == (old["id"] in {"a", "b"})
    validate_repair(graph, graph, patched, diagnostics, plan)
    repeated, second_plan = compile_execution_contracts(patched, graph, diagnostics)
    assert repeated == patched and second_plan == []


@pytest.mark.parametrize(
    "name,value", [("Collect account", "ZX-42"), ("确认预约", "八点半")]
)
def test_source_guard_and_repair_plan_are_domain_independent(name, value):
    graph = {
        "nodes": [
            {
                "id": "n",
                "type": "agentNode",
                "data": {
                    "name": name,
                    "prompt": f"Use {value}. Wait for confirmation.",
                },
            }
        ],
        "edges": [],
    }
    new = copy.deepcopy(graph)
    new["nodes"][0]["data"]["prompt"] += " Check a reported value before advancing."
    diagnostic = [{"case": "Mismatch", "failures": [{"assertion_index": 1}]}]
    plan = [
        {
            "case": "Mismatch",
            "assertion_index": 1,
            "cause": "Premature advance",
            "nodes": [name],
            "source_node": name,
            "source_quote": f"Use {value}.",
            "correction": "Check against the original requirement before advancing.",
        }
    ]
    validate_repair(graph, graph, new, diagnostic, plan)
    invalid = copy.deepcopy(new)
    invalid["nodes"][0]["data"]["prompt"] = "Rewritten, source lost."
    with pytest.raises(WorkbenchError, match="retain the original"):
        validate_repair(graph, graph, invalid, diagnostic, plan)
    with pytest.raises(WorkbenchError, match="requires a source-grounded"):
        validate_repair(graph, graph, new, diagnostic, [])
    plan[0]["assertion_index"] = 2
    with pytest.raises(WorkbenchError, match="actual failed assertion"):
        validate_repair(graph, graph, new, diagnostic, plan)
    plan[0]["assertion_index"] = 1
    plan[0]["source_quote"] = "Invented value"
    with pytest.raises(WorkbenchError, match="match the original"):
        validate_repair(graph, graph, new, diagnostic, plan)
