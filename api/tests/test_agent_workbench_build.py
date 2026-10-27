"""Generic source preservation and build/evaluate/repair contracts."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import SimulationCase
from api.services.agent_workbench import jobs
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import (
    check_assertions,
    resolve_node_references,
)
from api.services.agent_workbench.sop import (
    bind_source,
    coverage_report,
    merge_sections,
    preserved_sections,
    source_sections,
    without_source,
)


@pytest.fixture(autouse=True)
def checkpoint_storage(monkeypatch):
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())


def test_source_binding_preserves_every_section_without_repeating_generated_text():
    source = "请构建：节点1 核实\n原文A，含条件和例外。\n节点2 办理\n原文B，保留数值123。\n节点3 结束\n原文C。"
    sections = source_sections(source)
    assert len(sections) == 3
    assert sections[0]["title"] == "节点1 核实"
    graph = {
        "nodes": [
            {
                "id": "1",
                "type": "agentNode",
                "data": {"name": "阶段", "prompt": "处理本阶段"},
            }
        ]
    }
    bound = bind_source(graph, sections, {"阶段": [s["id"] for s in sections]})
    assert all(s["text"] in bound["nodes"][0]["data"]["prompt"] for s in sections)
    assert coverage_report(bound, sections) == {
        "total": 3,
        "preserved": 3,
        "complete": True,
    }
    assert without_source(bound) == graph
    assert [s["text"] for s in preserved_sections(bound)] == [
        s["text"] for s in sections
    ]
    assert len(source_sections("# Step A\nConfirm.\n# Step B\nFinish.")) == 2
    assert not source_sections("Change the greeting, please")
    updated = source_sections("节点1 核实\nUpdated A.\n节点2 办理\nUpdated B.")
    merged = merge_sections(preserved_sections(bound), updated)
    assert [s["text"] for s in merged] == [
        updated[0]["text"],
        updated[1]["text"],
        sections[2]["text"],
    ]
    for mapping in (
        {},
        {"阶段": [sections[0]["id"]]},
        {"Missing node": [s["id"] for s in sections]},
        {"阶段": [s["id"] for s in sections] * 2},
    ):
        with pytest.raises(WorkbenchError):
            bind_source(graph, sections, mapping)
    bound["nodes"][0]["data"]["prompt"] = bound["nodes"][0]["data"]["prompt"].replace(
        "123", "456"
    )
    with pytest.raises(WorkbenchError, match="integrity"):
        preserved_sections(bound)


def test_assertions_detect_wrong_turn_missing_turn_and_wrong_order():
    graph = {
        "nodes": [
            {"id": str(i), "data": {"name": name}}
            for i, name in enumerate(["A", "B", "C"])
        ]
    }
    case = SimulationCase(
        name="Generic ordering",
        user_messages=["yes", "question", "last input"],
        assertions=[
            {"kind": "contains_any", "value": ["confirmed", "accepted"], "turn": 1},
            {"kind": "contains", "value": "confirmed", "turn": 2},
            {"kind": "not_contains", "value": "unexpected", "turn": 3},
            {"kind": "node_sequence", "value": ["A", "B"]},
            {"kind": "node_sequence", "value": ["B", "A"]},
            {"kind": "node_not_visited", "value": "C"},
        ],
    )
    result = check_assertions(
        case,
        [
            {"role": "assistant", "text": "confirmed", "turn": 1},
            {"role": "assistant", "text": "answer", "turn": 2},
        ],
        [{"type": "node_transition", "payload": {"node_id": str(i)}} for i in (0, 1)],
        {},
        False,
        [],
        resolve_node_references(case, graph),
    )
    assert [a["passed"] for a in result] == [True, False, False, True, False, True]
    with pytest.raises(ValueError, match="Assertion turn must be between"):
        SimulationCase(
            name="invalid turn",
            user_messages=["one"],
            assertions=[{"kind": "contains", "value": "text", "turn": 3}],
        )


@pytest.mark.parametrize(
    "outcomes,expected,rounds,issue",
    [
        (["passed", "passed"], "initial", 0, None),
        (["passed", "failed"], "repaired", 1, None),
        (["passed", "error"], "initial", 0, None),
        (
            ["passed", "failed"],
            "initial",
            0,
            "Case 2 expects completion before the source's required confirmation.",
        ),
    ],
)
async def test_automatic_verification_freezes_suite_and_uses_runtime_model(
    monkeypatch, outcomes, expected, rounds, issue
):
    snapshot = {"name": "Agent", "workflow_json": {"nodes": []}}
    suite = {"payload": {"name": "Frozen", "cases": [1, 2]}}

    async def get(_org, _sid, table, _id):
        return {"snapshot": snapshot} if table == "candidates" else suite

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    pin = AsyncMock(
        return_value={"evaluation_model_configuration_fingerprint": "voice"}
    )
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", pin)
    runtime = SimpleNamespace(effective=object(), snapshot={"version": 3})
    monkeypatch.setattr(jobs, "prepare_models", AsyncMock(return_value=runtime))

    def report(cid, states):
        return {
            "id": cid + "-eval",
            "candidate_id": cid,
            "results": [{"name": str(i), "status": s, "assertions": [{"kind": "completed", "value": True, "passed": s == "passed"}]} for i, s in enumerate(states)],
            "summary": {
                **{
                    s: states.count(s)
                    for s in ("passed", "failed", "error", "inconclusive")
                },
                "total": 2,
            },
        }

    evaluate = AsyncMock(
        side_effect=[
            report("initial", outcomes),
            report(expected, ["passed", "passed"]),
            report(expected, ["passed", "passed"]),
        ]
    )
    monkeypatch.setattr(jobs, "evaluate", evaluate)
    build = AsyncMock(
        return_value={"verification_issue": issue}
        if issue
        else {
            "candidate_id": "repaired",
            "model_requests": 1,
            "sop_coverage": None,
        }
    )
    monkeypatch.setattr(jobs, "_build", build)
    builder = object()
    result = await jobs._verify_build(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"text": "requirements"}},
        {},
        builder,
        {"candidate_id": "initial", "suite_id": "frozen", "model_requests": 2},
    )
    assert result["candidate_id"] == expected and len(result["rounds"]) == rounds
    if result["verification"]["status"] == "passed":
        assert result["verification"]["confirmed"] is True
        assert result["verification_checks"][-1]["confirmation"] is True
    pin.assert_awaited_once_with(1, "s", "j", "l", runtime.snapshot, evaluation=True)
    for call in evaluate.call_args_list:
        assert call.args[-2:] == ("frozen", runtime.effective)
    if rounds:
        assert build.call_args.args[4] is builder
        assert build.call_args.kwargs["tuning"] is True
    if issue:
        assert evaluate.await_count == 1
        assert result["verification"]["status"] == "inconclusive"
        assert result["verification"]["reason"] == issue


async def test_automatic_repair_keeps_best_candidate_when_proposal_regresses(
    monkeypatch,
):
    async def get(_org, _sid, table, _id):
        return (
            {"snapshot": {}}
            if table == "candidates"
            else {"payload": {"cases": [1, 2]}}
        )

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(effective=None, snapshot={})),
    )

    def report(cid, states):
        return {
            "id": cid,
            "candidate_id": cid,
            "results": [{"name": str(i), "status": s, "assertions": [{"kind": "completed", "value": True, "passed": s == "passed"}]} for i, s in enumerate(states)],
            "summary": {
                "passed": 1,
                "failed": 1,
                "error": 0,
                "inconclusive": 0,
                "total": 2,
            },
        }

    monkeypatch.setattr(
        jobs,
        "evaluate",
        AsyncMock(
            side_effect=[
                report("best", ["passed", "failed"]),
                report("bad", ["failed", "passed"]),
                report("bad-again", ["failed", "passed"]),
                report("bad-final", ["failed", "passed"]),
            ]
        ),
    )
    monkeypatch.setattr(jobs, "_build", AsyncMock(return_value={"candidate_id": "bad"}))
    result = await jobs._verify_build(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"text": "requirements"}},
        {},
        None,
        {"candidate_id": "best", "suite_id": "frozen"},
    )
    assert result["candidate_id"] == "best"
    assert result["verification"]["status"] == "failed"
    assert result["rounds"][0]["accepted"] is False
    assert len(result["rounds"]) == 3
    assert "Rejected attempt" in jobs._build.call_args.args[5]


async def test_confirmation_failure_becomes_next_repair_evidence(monkeypatch):
    async def get(_org, _sid, table, _id):
        return (
            {"snapshot": {"workflow_json": {"nodes": [], "edges": []}}}
            if table == "candidates"
            else {"payload": {"cases": [1]}}
        )

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(effective=None, snapshot={})),
    )

    def result(cid, eid, flags):
        passed = all(flags)
        return {
            "id": eid,
            "candidate_id": cid,
            "summary": {
                "passed": int(passed),
                "failed": int(not passed),
                "error": 0,
                "inconclusive": 0,
                "total": 1,
            },
            "results": [
                {
                    "name": "stage boundary",
                    "status": "passed" if passed else "failed",
                    "assertions": [
                        {"kind": "contains", "value": value, "passed": flag}
                        for value, flag in zip(["entry", "confirmation"], flags)
                    ],
                }
            ],
        }

    evaluate = AsyncMock(
        side_effect=[
            result("original", "baseline", [False, False]),
            result("first", "lucky-pass", [True, True]),
            result("first", "confirmation-failed", [True, False]),
            result("second", "repaired-pass", [True, True]),
            result("second", "confirmed-pass", [True, True]),
        ]
    )
    monkeypatch.setattr(jobs, "evaluate", evaluate)
    build = AsyncMock(
        side_effect=[
            {"candidate_id": "first", "sop_coverage": None},
            {"candidate_id": "second", "sop_coverage": None},
        ]
    )
    monkeypatch.setattr(jobs, "_build", build)
    output = await jobs._verify_build(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"text": "Preserve source"}},
        {},
        None,
        {"candidate_id": "original", "suite_id": "frozen"},
    )
    assert output["candidate_id"] == "second"
    assert output["evaluation_id"] == "confirmed-pass"
    assert output["verification"]["confirmed"] is True
    assert [r["accepted"] for r in output["rounds"]] == [True, True]
    next_diagnostic = build.call_args_list[1].args[3]["automatic_repair"]["diagnostics"]
    assert next_diagnostic[0]["failures"][0]["value"] == "confirmation"
    assert len(output["verification_checks"]) == 5
    assert all(call.args[4] == "frozen" for call in evaluate.call_args_list)


@pytest.mark.parametrize("cap,expected_rounds", [("", 4), ("2", 2)])
async def test_repair_continues_past_two_rounds_while_assertions_improve(
    monkeypatch, cap, expected_rounds
):
    monkeypatch.setenv("WORKBENCH_BUILD_REPAIR_ROUNDS", cap)

    async def get(_org, _sid, table, _id):
        return (
            {"snapshot": {"workflow_json": {"nodes": [], "edges": []}}}
            if table == "candidates"
            else {"payload": {"cases": [1]}}
        )

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(jobs.db_client, "wb_emit", AsyncMock())
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(effective=None, snapshot={})),
    )

    def result(count):
        passed = count == 4
        return {
            "id": f"evaluation-{count}",
            "candidate_id": f"c{count}",
            "summary": {
                "passed": int(passed),
                "failed": int(not passed),
                "error": 0,
                "inconclusive": 0,
                "total": 1,
            },
            "results": [
                {
                    "name": "steps",
                    "status": "passed" if passed else "failed",
                    "assertions": [
                        {"kind": "contains", "value": str(i), "passed": i < count}
                        for i in range(4)
                    ],
                }
            ],
        }

    monkeypatch.setattr(
        jobs, "evaluate", AsyncMock(side_effect=[result(i) for i in [0, 1, 2, 3, 4, 4]])
    )
    build = AsyncMock(
        side_effect=[
            {"candidate_id": f"c{i}", "sop_coverage": None} for i in range(1, 5)
        ]
    )
    monkeypatch.setattr(jobs, "_build", build)
    output = await jobs._verify_build(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"text": "Keep every step"}},
        {},
        None,
        {"candidate_id": "c0", "suite_id": "frozen"},
    )
    assert len(output["rounds"]) == expected_rounds
    assert output["candidate_id"] == f"c{expected_rounds}"
    assert output["verification"]["confirmed"] == (expected_rounds == 4)


async def test_failed_source_mapping_reuses_code_and_variables(monkeypatch):
    from api.services.agent_workbench.tools import WorkbenchTools

    tools = WorkbenchTools(
        1,
        "s",
        {"input": {}, "id": "j", "lease_token": "l"},
        {"target": {}, "target_fingerprint": "base"},
    )
    validate = AsyncMock(
        side_effect=[
            WorkbenchError("SOP mapping invalid"),
            {"workflow_json": {"nodes": [], "edges": []}},
        ]
    )
    monkeypatch.setattr(tools, "_validate", validate)
    monkeypatch.setattr(
        jobs.db_client,
        "wb_save_candidate",
        AsyncMock(return_value={"id": "c", "fingerprint": "f"}),
    )
    with pytest.raises(WorkbenchError, match="omit code"):
        await tools.save_candidate(
            "code", "summary", {"name": "value"}, {"stage": ["missing"]}
        )
    await tools.save_candidate(
        summary="corrected", source_sections={"stage": ["correct"]}
    )
    assert validate.call_args.args == (
        "code",
        {"name": "value"},
        {"stage": ["correct"]},
    )
    with pytest.raises(WorkbenchError, match="first save"):
        await tools.save_candidate(summary="no stale code")
    tools.preloaded_context = {}
    assert "get_context" not in {spec["name"] for spec in tools.specs()}


def test_sdk_edits_reject_ambiguous_changes_without_executing_code():
    from api.services.agent_workbench.tools import edit_code

    assert (
        edit_code("before; after;", [{"old": "before", "new": "changed"}])
        == "changed; after;"
    )
    for old in ["missing", ""]:
        with pytest.raises(WorkbenchError, match="exactly once"):
            edit_code("before; after;", [{"old": old, "new": "x"}])
    with pytest.raises(WorkbenchError, match="exactly once"):
        edit_code("same same", [{"old": "same", "new": "x"}])


@pytest.mark.parametrize("node_count", [3, 45])
async def test_sdk_save_preserves_explicit_checkpoints_at_any_graph_size(
    monkeypatch, node_count
):
    from api.services.agent_workbench import tools as module

    for method in (
        "get_tools_for_organization",
        "get_documents_for_organization",
        "get_credentials_for_organization",
        "get_recordings",
    ):
        monkeypatch.setattr(module.db_client, method, AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "prepare_models", AsyncMock())
    monkeypatch.setattr(
        module, "validate_workflow_tool_name_collisions", AsyncMock(return_value=[])
    )
    saved = AsyncMock(return_value={"id": "c", "fingerprint": "f"})
    monkeypatch.setattr(module.db_client, "wb_save_candidate", saved)
    instruction = "\n".join(
        f"# Checkpoint {i}\nAsk for confirmation {i} before advancing."
        for i in range(node_count)
    )
    toolset = module.WorkbenchTools(
        1,
        "s",
        {
            "input": {"text": instruction, "mode": "build"},
            "id": "j",
            "lease_token": "l",
        },
        {
            "target": {"name": "Original", "workflow_json": {"nodes": [], "edges": []}},
            "target_fingerprint": "f",
        },
    )
    graph = {
        "nodes": [
            {
                "id": str(i + 1),
                "type": "startCall"
                if i == 0
                else "endCall"
                if i == node_count - 1
                else "agentNode",
                "position": {"x": i * 300, "y": 0},
                "data": {
                    "name": f"Checkpoint {i}",
                    "prompt": f"Handle confirmation {i}.",
                },
            }
            for i in range(node_count)
        ],
        "edges": [
            {
                "id": f"e{i}",
                "source": str(i + 1),
                "target": str(i + 2),
                "data": {
                    "label": f"confirmed_{i}",
                    "condition": f"The user confirmed checkpoint {i}.",
                },
            }
            for i in range(node_count - 1)
        ],
    }
    code = await module.generate_code(graph, workflow_name="Generic")
    result = await toolset.save_candidate(
        code,
        "preserve confirmation boundaries",
        source_sections={
            f"Checkpoint {i}": [s["id"]] for i, s in enumerate(toolset.source)
        },
    )
    stored = saved.call_args.args[4]["workflow_json"]
    assert [(n["id"], n["type"], n["data"]["name"]) for n in stored["nodes"]] == [
        (n["id"], n["type"], n["data"]["name"]) for n in graph["nodes"]
    ]
    assert [
        (e["source"], e["target"], e["data"]["condition"]) for e in stored["edges"]
    ] == [(e["source"], e["target"], e["data"]["condition"]) for e in graph["edges"]]
    assert toolset.coverage == {
        "total": node_count,
        "preserved": node_count,
        "complete": True,
    }
    assert result["nodes"] == [
        {"id": n["id"], "name": n["data"]["name"]} for n in graph["nodes"]
    ]
    names = {s["name"] for s in toolset.specs()}
    assert "save_prompt_agent" not in names
    assert {"save_candidate", "save_suite", "save_scenarios"} <= names


async def test_suite_preflight_rejects_invalid_nodes_and_structural_only_cases(
    monkeypatch,
):
    from api.services.agent_workbench import tools as module

    toolset = module.WorkbenchTools(
        7,
        "s",
        {"input": {"text": "# A\nAsk.\n# B\nFinish."}, "id": "j", "lease_token": "l"},
        {
            "target": {"name": "Test", "workflow_json": {"nodes": [], "edges": []}},
            "target_fingerprint": "f",
        },
    )
    with pytest.raises(WorkbenchError, match="Save a valid candidate"):
        await toolset.save_suite({})
    toolset.candidate_ids.append("candidate")
    get = AsyncMock(
        return_value={
            "snapshot": {
                "workflow_json": {
                    "nodes": [
                        {
                            "id": "1",
                            "type": "startCall",
                            "data": {"name": "Conversation", "prompt": ""},
                        }
                    ],
                    "edges": [],
                },
                "template_context_variables": {},
            }
        }
    )
    save = AsyncMock(return_value={"id": "suite", "fingerprint": "suite-f"})
    monkeypatch.setattr(module.db_client, "wb_get", get)
    monkeypatch.setattr(module.db_client, "wb_save_suite", save)
    suite = {
        "name": "Requirements",
        "cases": [
            {
                "name": "Path",
                "user_messages": ["yes"],
                "assertions": [{"kind": "node_visited", "value": "startCall"}],
            }
        ],
    }
    with pytest.raises(WorkbenchError, match="available_nodes") as error:
        await toolset.save_suite(suite)
    assert "source-derived" in str(error.value)
    get.assert_awaited_once_with(7, "s", "candidates", "candidate")
    save.assert_not_awaited()
    suite["cases"][0]["assertions"] = [
        {"kind": "node_visited", "value": "Conversation"},
        {"kind": "contains", "value": "confirmed", "turn": 1},
    ]
    await toolset.save_suite(suite)
    assert save.call_args.args[:2] == (7, "s")
    await toolset.save_scenarios(
        "Paired turns",
        [
            {
                "name": "A then B",
                "initial_context": {"contact": "Test"},
                "completed": False,
                "turns": [
                    {"user": "first", "contains": ["step A"]},
                    {
                        "user": "second",
                        "contains_any": ["step B", "next B"],
                        "not_contains": ["finished"],
                    },
                ],
            }
        ],
    )
    compiled = save.call_args.args[2]["cases"][0]
    assert compiled["user_messages"] == ["first", "second"]
    assert compiled["initial_context"] == {"contact": "Test"}
    assert [(a["kind"], a["turn"]) for a in compiled["assertions"]] == [
        ("completed", None),
        ("contains", 1),
        ("not_contains", 2),
        ("contains_any", 2),
    ]


async def test_sdk_repair_reuses_current_source_mapping(monkeypatch):
    from api.services.agent_workbench.tools import WorkbenchTools

    sections = source_sections("# A\nOriginal.\n# B\nMore original.")
    graph = bind_source(
        {
            "edges": [],
            "nodes": [
                {
                    "id": "1",
                    "type": "agentNode",
                    "data": {"name": "Stage", "prompt": "Old"},
                }
            ],
        },
        sections,
        {"Stage": [s["id"] for s in sections]},
    )
    toolset = WorkbenchTools(
        1,
        "s",
        {"input": {}, "id": "j", "lease_token": "l"},
        {"target": {"workflow_json": graph}, "target_fingerprint": "f"},
        tuning=True,
    )
    toolset.base_code = "Old orchestration"
    repaired = copy.deepcopy(graph)
    repaired["nodes"][0]["data"]["prompt"] = repaired["nodes"][0]["data"][
        "prompt"
    ].replace("Old", "New", 1)
    validate = AsyncMock(return_value={"workflow_json": repaired, "name": "Agent"})
    monkeypatch.setattr(toolset, "_validate", validate)
    monkeypatch.setattr(
        jobs.db_client,
        "wb_save_candidate",
        AsyncMock(return_value={"id": "c", "fingerprint": "f"}),
    )
    await toolset.save_candidate(summary="repair", edits=[{"old": "Old", "new": "New"}])
    assert validate.call_args.args == (
        "New orchestration",
        None,
        {"Stage": [s["id"] for s in sections]},
    )
    validate.return_value = {"workflow_json": graph, "name": "Renamed only"}
    with pytest.raises(WorkbenchError, match="executable candidate is unchanged"):
        await toolset.save_candidate(
            edits=[{"old": "New", "new": "Unchanged"}], summary="claim repaired"
        )
    assert jobs.db_client.wb_save_candidate.await_count == 1
    stopped = await toolset.save_candidate(summary="claim repaired again")
    assert stopped["verification"] == "needs_attention"
    assert toolset.verification_issue
    assert jobs.db_client.wb_save_candidate.await_count == 1


@pytest.mark.parametrize(
    "mode,instruction",
    [
        ("build", "Change only the opening to the new wording supplied here."),
        (
            "auto",
            "# First question\nExplain this.\n# Second question\nWhy does this happen?",
        ),
    ],
)
async def test_followup_request_is_not_replaced_by_preserved_prior_source(
    monkeypatch, mode, instruction
):
    from api.services.agent_workbench.tools import WorkbenchTools

    from api.services.agent_workbench import basis
    monkeypatch.setattr(basis, "ensure_basis", AsyncMock(return_value={"sources": [], "fingerprint": "basis", "target_fingerprint": "f", "memory_revision": "m", "warnings": []}))
    sections = source_sections("# First\nOriginal A.\n# Second\nOriginal B.")
    graph = bind_source(
        {
            "nodes": [
                {
                    "id": "1",
                    "type": "agentNode",
                    "data": {"name": "Stage", "prompt": "Process"},
                }
            ]
        },
        sections,
        {"Stage": [s["id"] for s in sections]},
    )
    captured = []

    async def get_context(self):
        return {"source_sections": self.source}

    async def load_agent_memory(self):
        self.agent_memory_view = {"confirmed": []}

    monkeypatch.setattr(WorkbenchTools, "get_context", get_context)
    monkeypatch.setattr(WorkbenchTools, "load_agent_memory", load_agent_memory)
    monkeypatch.setattr(WorkbenchTools, "read_conversation", AsyncMock(return_value={}))
    monkeypatch.setattr(
        WorkbenchTools, "cached_run_evidence", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(WorkbenchTools, "list_attachments", AsyncMock(return_value=[]))

    class Client:
        def __init__(self, tools, event):
            self.tools = tools

        async def run(self, prompt, *args, **kwargs):
            captured.append(prompt)
            self.tools.candidate_ids = ["c"]
            self.tools.suite_ids = ["s"]
            self.tools.analysis_answer = "Explanation"
            return {}

    monkeypatch.setattr(jobs, "CodexClient", Client)
    await jobs._build(
        1,
        "s",
        {"input": {"text": instruction, "mode": mode}},
        {
            "target": {"workflow_json": graph},
            "target_fingerprint": "f",
            "session": {"model_protocol": "chat"},
        },
        SimpleNamespace(llm=None),
        instruction,
    )
    assert captured[0].endswith("CURRENT USER REQUEST:\n" + instruction)


@pytest.mark.parametrize("runtime_error", [False, True])
async def test_parallel_evaluation_is_bounded_and_results_keep_suite_order(
    monkeypatch, runtime_error
):
    from api.services.agent_workbench import evaluation
    from api.services.workflow import text_chat_runner

    monkeypatch.setenv("WORKBENCH_EVALUATION_CONCURRENCY", "2")
    suite = {
        "name": "generic",
        "cases": [
            {
                "name": str(i),
                "user_messages": ["continue"],
                "initial_context": {"case": i},
                "assertions": [{"kind": "contains", "value": "response"}],
            }
            for i in range(4)
        ],
    }

    async def get(_org, _sid, table, _id):
        return (
            {"snapshot": {"workflow_json": {"nodes": []}}, "fingerprint": "c"}
            if table == "candidates"
            else {"payload": suite, "fingerprint": "s"}
        )

    monkeypatch.setattr(evaluation.db_client, "wb_get", get)
    for method, value in [
        ("wb_context", {"session": {"workflow_id": 1}}),
        ("wb_evaluation", {"id": "e"}),
        ("wb_heartbeat", None),
        ("wb_emit", None),
        ("wb_evaluation_result", None),
        ("update_workflow_run", None),
    ]:
        monkeypatch.setattr(evaluation.db_client, method, AsyncMock(return_value=value))

    async def create(*args):
        return 100 + args[-1]["case"]

    monkeypatch.setattr(evaluation.db_client, "wb_create_run", create)
    active = peak = 0
    both_running = asyncio.Event()
    finished = []

    async def turn(**kwargs):
        nonlocal active, peak
        if runtime_error:
            raise ValueError("Workflow must have at least one Start Call")
        index = kwargs["workflow_run_id"] - 100
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_running.set()
        await asyncio.wait_for(both_running.wait(), 1)
        # The first case waits for the second to prove persistence ordering.
        if index == 0:
            await asyncio.wait_for(second_finished.wait(), 1)
        else:
            await asyncio.sleep(0)
        finished.append(index)
        if index == 1:
            second_finished.set()
        active -= 1
        return SimpleNamespace(
            checkpoint={},
            gathered_context={},
            is_completed=index == 3 or len(kwargs["session_data"]["turns"]) > 1,
            assistant_text="response",
            events=[{"type": "node_transition", "payload": {"node_id": "stage"}}],
            usage={},
        )

    second_finished = asyncio.Event()
    monkeypatch.setattr(text_chat_runner, "execute_text_chat_pending_turn", turn)
    result = await evaluation.evaluate(
        1,
        "s",
        {"id": "j", "lease_token": "l"},
        "c",
        "s",
        SimpleNamespace(llm=SimpleNamespace(model="test")),
    )
    if runtime_error:
        # A database run ID is only an attempt; an actual model response proves execution.
        assert result["summary"]["error"] == 4
        assert result["summary"]["attempted"] == 4
        assert result["summary"]["executed"] == 0
        assert all(not r["transcript"] for r in result["results"])
        return
    assert peak == 2 and finished[0] == 1
    assert [r["name"] for r in result["results"]] == ["0", "1", "2", "3"]
    assert result["summary"]["passed"] == 3
    assert result["summary"]["failed"] == 1
    assert result["summary"]["executed"] == 4
    first_case = result["results"][0]
    assert [e["turn"] for e in first_case["events"]] == [0, 1]
    assert [m["turn"] for m in first_case["transcript"]] == [0, 1, 1]
    early_end = result["results"][3]
    assert early_end["assertions"][0]["passed"] is True
    assert early_end["assertions"][-1] == {
        "kind": "script_consumed",
        "value": 1,
        "actual": 0,
        "passed": False,
        "reason": "固定脚本执行覆盖率；提前结束本身不证明 Agent 违反流程，需结合实际分支复核。",
        "informational": False,
    }
    writes = evaluation.db_client.wb_evaluation_result.call_args_list
    assert writes[-1].args[-2] == "completed"
    assert all(
        [r["name"] for r in call.args[5]] == sorted(r["name"] for r in call.args[5])
        for call in writes
    )
