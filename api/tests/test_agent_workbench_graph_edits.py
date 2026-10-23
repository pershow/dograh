"""Local edits must preserve untouched source/branches and support targeted repair."""

import copy
import re
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import tools as module
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.graph_edits import preserve_graph, update_fields


@pytest.fixture
def graph():
    return {
        "nodes": [
            {
                "id": str(i + 1),
                "type": kind,
                "position": {"x": i * 100, "y": 1},
                "data": {"name": name, "prompt": prompt},
            }
            for i, (kind, name, prompt) in enumerate(
                [
                    ("startCall", "Entry", "Ask which route."),
                    ("agentNode", "Route A", "Preserve the original rule A."),
                    ("agentNode", "Route B", "Preserve the original rule B."),
                    ("endCall", "Finish", "Goodbye."),
                ]
            )
        ],
        "edges": [
            {
                "id": label,
                "source": source,
                "target": target,
                "data": {"label": label, "condition": condition},
            }
            for source, target, label, condition in [
                ("1", "2", "choose_a", "Route A requested"),
                ("1", "2", "default_a", "Explicit original default rule"),
                ("1", "3", "choose_b", "Route B requested"),
                ("2", "4", "done_a", "A completed"),
                ("3", "4", "done_b", "B completed"),
            ]
        ],
    }


@pytest.fixture
def workbench(monkeypatch, graph):
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
    monkeypatch.setattr(
        module.db_client,
        "wb_save_candidate",
        AsyncMock(return_value={"id": "candidate", "fingerprint": "f"}),
    )
    return module.WorkbenchTools(
        1,
        "s",
        {"id": "j", "lease_token": "l", "input": {"mode": "auto"}},
        {
            "target": {"name": "Agent", "workflow_json": graph},
            "target_fingerprint": "f",
            "session": {"workflow_id": 1},
            "assets": [],
        },
    )


async def test_existing_agent_only_exposes_patches_and_loads_context_once(
    workbench, monkeypatch
):
    generate = AsyncMock(wraps=module.generate_code)
    monkeypatch.setattr(module, "generate_code", generate)
    initial_specs = workbench.specs()
    assert "validate_candidate" not in {s["name"] for s in initial_specs}
    save = next(s for s in initial_specs if s["name"] == "save_candidate")
    assert "code" not in save["inputSchema"]["properties"]
    assert "edits" in save["inputSchema"]["properties"]
    first = await workbench.get_context()
    assert "wf.edge" in first["code"]
    assert (await workbench.get_context())["already_supplied"]
    await workbench.set_task_intent("build")
    assert workbench.specs() == initial_specs
    generate.assert_awaited_once()
    with pytest.raises(WorkbenchError, match="must use"):
        await workbench.save_candidate(code=first["code"], summary="rewrite")


async def test_local_prompt_edit_preserves_all_other_sop_and_parallel_branches(
    workbench, graph
):
    workbench.base_code = await module.generate_code(graph, workflow_name="Agent")
    await workbench.save_candidate(
        summary="Clarify A",
        edits=[
            {
                "old": "Preserve the original rule A.",
                "new": "Preserve the original rule A. Wait for explicit confirmation.",
            }
        ],
    )
    stored = module.db_client.wb_save_candidate.call_args.args[4]["workflow_json"]
    assert len(stored["edges"]) == len(graph["edges"])
    assert [e["data"] for e in stored["edges"]] == [e["data"] for e in graph["edges"]]
    assert [(e["source"], e["target"]) for e in stored["edges"]] == [
        (e["source"], e["target"]) for e in graph["edges"]
    ]
    for original, changed in zip(graph["nodes"], stored["nodes"]):
        assert changed["data"]["prompt"].startswith(original["data"]["prompt"])
        if original["data"]["name"] != "Route A":
            assert changed["data"]["prompt"] == original["data"]["prompt"]
    assert workbench.edit_stats["full_code_calls"] == 0


async def test_missing_incoming_reports_names_and_reuses_failed_submission(
    workbench, graph
):
    workbench.base_code = await module.generate_code(graph, workflow_name="Agent")
    statements = re.findall(r"wf\.edge\([^;]*;\n", workbench.base_code)
    line = next(line for line in statements if '"choose_b"' in line)
    anchor = next(line for line in statements if '"done_b"' in line)
    with pytest.raises(WorkbenchError, match="Route B") as error:
        await workbench.save_candidate(
            summary="bad edit", edits=[{"old": line, "new": ""}]
        )
    assert "original_incoming" in str(error.value) and "choose_b" in str(error.value)
    assert line not in workbench.last_submission["code"]
    module.db_client.wb_save_candidate.assert_not_awaited()
    await workbench.save_candidate(
        summary="restore omitted branch", edits=[{"old": anchor, "new": line + anchor}]
    )
    stored = module.db_client.wb_save_candidate.call_args.args[4]["workflow_json"]
    assert len(stored["edges"]) == len(graph["edges"])


async def test_connected_graph_still_cannot_silently_drop_an_alternative(
    workbench, graph
):
    workbench.base_code = await module.generate_code(graph, workflow_name="Agent")
    line = next(
        line
        for line in re.findall(r"wf\.edge\([^;]*;\n", workbench.base_code)
        if '"default_a"' in line
    )
    with pytest.raises(WorkbenchError, match="default_a"):
        await workbench.save_candidate(
            summary="accidental omission", edits=[{"old": line, "new": ""}]
        )
    module.db_client.wb_save_candidate.assert_not_awaited()
    # An actual deletion request must identify the removed branch explicitly.
    await workbench.save_candidate(
        summary="User explicitly withdrew the default route",
        removals={
            "transitions": [
                {"source": "Entry", "target": "Route A", "label": "default_a"}
            ]
        },
    )
    assert module.db_client.wb_save_candidate.await_count == 1


def test_preservation_accounts_for_duplicate_branches_and_rejects_stale_removals(graph):
    duplicate = copy.deepcopy(graph)
    duplicate["edges"].append(copy.deepcopy(graph["edges"][0]))
    with pytest.raises(WorkbenchError, match="choose_a"):
        preserve_graph(duplicate, graph)
    with pytest.raises(WorkbenchError):
        preserve_graph(graph, graph, {"nodes": ["Route A"]})


async def test_named_field_updates_preserve_quotes_and_unrelated_content(
    workbench, graph
):
    workbench.base_code = await module.generate_code(graph, workflow_name="Agent")
    text = '用户说"值不对"时，说明正确内容；保留原分支。'
    await workbench.save_candidate(
        summary="focused correction",
        template_context_variables={"business": "Original business default"},
        node_updates=[{"name": "Route A", "prompt_append": text}],
        edge_updates=[
            {
                "source": "Route A",
                "target": "Finish",
                "label": "done_a",
                "data": {"condition": "A explicitly completed"},
            }
        ],
    )
    saved = module.db_client.wb_save_candidate.call_args.args[4]["workflow_json"]
    assert (
        saved["nodes"][1]["data"]["prompt"]
        == graph["nodes"][1]["data"]["prompt"] + "\n" + text
    )
    assert len(saved["nodes"]) == 4 and len(saved["edges"]) == 5
    assert [e["data"] for e in saved["edges"] if e["source"] != "2"] == [
        e["data"] for e in graph["edges"] if e["source"] != "2"
    ]
    # A subsequent patch/read uses the saved editing state, not the initial code.
    assert (await workbench.read_node("Route A"))["node"]["data"]["prompt"].endswith(
        text
    )
    await workbench.save_candidate(
        summary="another local correction",
        node_updates=[{"name": "Route B", "prompt_append": "Wait here."}],
    )
    latest = module.db_client.wb_save_candidate.call_args.args[4]["workflow_json"]
    assert latest["nodes"][1]["data"]["prompt"].endswith(text)
    assert latest["edges"][3]["data"]["condition"] == "A explicitly completed"
    assert (
        module.db_client.wb_save_candidate.call_args.args[4][
            "template_context_variables"
        ]["business"]
        == "Original business default"
    )
    assert workbench.edit_stats["full_code_calls"] == 0


def test_field_patches_are_atomic_and_reject_unknown_or_ambiguous_targets(graph):
    before = copy.deepcopy(graph)
    for updates, edges in [
        ([{"name": "Unknown", "prompt_append": "new"}], []),
        ([{"name": "Route A", "data": {"invented_field": True}}], []),
        ([{"name": "Route A", "prompt_edits": [{"old": "missing", "new": "new"}]}], []),
        (
            [{"name": "Route A", "prompt_append": "new"}],
            [
                {
                    "source": "Entry",
                    "target": "Route B",
                    "label": "wrong",
                    "data": {"condition": "new"},
                }
            ],
        ),
    ]:
        with pytest.raises(WorkbenchError):
            update_fields(graph, updates, edges)
        assert graph == before


async def test_automatic_repair_uses_plain_context_and_protects_source(workbench):
    workbench.automatic_repair = {
        "source_graph": copy.deepcopy(workbench.base["workflow_json"]),
        "diagnostics": [
            {
                "case": "route",
                "failures": [
                    {
                        "assertion_index": 0,
                        "kind": "not_contains",
                        "value": "Goodbye",
                        "observed_turn": {
                            "node_before": "2",
                            "node_after": "2",
                            "transitions": [],
                            "node_after_name": "Route A",
                            "turn": 1,
                        },
                    }
                ],
            }
        ],
    }
    context = await workbench.get_context()
    assert "code" not in context and context["graph"]["nodes"]
    specs = {s["name"]: s for s in workbench.specs()}
    assert "read_agent_run" not in specs and "read_conversation" not in specs
    assert "repair_plan" in specs["save_candidate"]["inputSchema"]["required"]
    await workbench.save_candidate(
        summary="Derive a completion boundary from the source",
        node_updates=[
            {"name": "Route A", "prompt_append": "Wait for completion evidence."}
        ],
        repair_plan=[
            {
                "case": "route",
                "assertion_index": 0,
                "cause": "Left too soon",
                "nodes": ["Route A"],
                "source_node": "Route A",
                "source_quote": "Preserve the original rule A.",
                "correction": "Wait for completion evidence.",
            }
        ],
    )
    assert workbench.repair_plan
    assert workbench.edit_stats["compiled_contracts"] == 1
    saved = module.db_client.wb_save_candidate.call_args.args
    assert "WORKFLOW_EXECUTION_CONTRACT" in saved[5]


def test_automatic_repair_cannot_rewrite_branch_conditions(graph):
    from api.services.agent_workbench.repair import validate_repair

    changed = copy.deepcopy(graph)
    changed["edges"][0]["data"]["condition"] = "Always advance, regardless of route"
    with pytest.raises(WorkbenchError, match="retain original condition"):
        validate_repair(graph, graph, changed, [], [])


async def test_rejected_behavior_patch_does_not_accumulate_duplicate_clarifications(
    workbench,
):
    workbench.automatic_repair = {
        "source_graph": copy.deepcopy(workbench.base["workflow_json"]),
        "diagnostics": [{"case": "route", "failures": [{"assertion_index": 0}]}],
    }
    await workbench.get_context()
    plan = [
        {
            "case": "route",
            "assertion_index": 0,
            "nodes": ["Route A"],
            "source_node": "Route A",
            "source_quote": "Invented quote",
            "cause": "Missing confirmation",
            "correction": "Wait for confirmation",
        }
    ]
    with pytest.raises(WorkbenchError, match="attempt was discarded"):
        await workbench.save_candidate(
            summary="Rejected attempt",
            repair_plan=plan,
            node_updates=[
                {"name": "Route A", "prompt_append": "Rejected clarification."}
            ],
        )
    assert workbench.last_submission is None
    plan[0]["source_quote"] = "Preserve the original rule A."
    await workbench.save_candidate(
        summary="Corrected attempt",
        repair_plan=plan,
        node_updates=[{"name": "Route A", "prompt_append": "Validated clarification."}],
    )
    snapshot = module.db_client.wb_save_candidate.call_args.args[4]
    prompt = next(
        n["data"]["prompt"]
        for n in snapshot["workflow_json"]["nodes"]
        if n["data"]["name"] == "Route A"
    )
    assert "Rejected clarification" not in prompt
    assert prompt.count("Validated clarification.") == 1
