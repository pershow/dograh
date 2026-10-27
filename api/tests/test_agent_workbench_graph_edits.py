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


async def test_editing_view_refreshes_regenerated_sdk_without_reloading_catalogs(
    workbench,
):
    initial = await workbench.get_context()
    # Valid author-defined identifiers need not survive SDK serialization.
    workbench.base_code = initial["code"].replace("entry", "entryRef")
    old = await workbench.get_context(section="editing")
    assert "entryRef" in old["code"]
    result = await workbench.save_candidate(
        node_updates=[{"name": "Route A", "prompt_append": "Wait for confirmation."}],
        expected_revision=old["revision"],
    )
    catalogs = [
        module.db_client.get_documents_for_organization,
        module.db_client.get_credentials_for_organization,
    ]
    catalog_reads = [reader.await_count for reader in catalogs]
    current = await workbench.get_context(section="editing")
    assert current["state"] == "saved_candidate"
    assert current["revision"] == result["editing_revision"] != old["revision"]
    assert "entryRef" not in current["code"]
    assert "Wait for confirmation." in current["code"]
    with pytest.raises(WorkbenchError, match="revision changed"):
        await workbench.save_candidate(
            edits=[{"old": "Ask which route.", "new": "Choose a route."}],
            expected_revision=old["revision"],
        )
    with pytest.raises(WorkbenchError, match="edit 1 matched 0 times"):
        await workbench.save_candidate(
            edits=[{"old": "const entryRef =", "new": "const other ="}]
        )
    assert (await workbench.get_context(section="editing")) == current
    assert [reader.await_count for reader in catalogs] == catalog_reads
    await workbench.save_candidate(
        edits=[{"old": "Ask which route.", "new": "Choose a route."}],
        expected_revision=current["revision"],
    )
    assert (await workbench.read_node("Entry"))["node"]["data"][
        "prompt"
    ] == "Choose a route."
    assert workbench.edit_stats["full_code_calls"] == 0


async def test_invalid_reference_retains_exact_source_and_can_be_repaired(workbench):
    initial = await workbench.get_context()
    declaration = "const route_a ="
    assert declaration in initial["code"]
    with pytest.raises(WorkbenchError, match="Unknown node variable"):
        await workbench.save_candidate(
            edits=[{"old": declaration, "new": "const renamedRoute ="}]
        )
    current = await workbench.get_context(section="editing")
    assert current["state"] == "retained_submission"
    assert current["parse_errors"] and current["nodes"] is None
    assert current["removals_required_for_current_graph"] is None
    assert current["code"] == initial["code"].replace(
        declaration, "const renamedRoute ="
    )
    assert workbench.base_code == initial["code"]
    with pytest.raises(WorkbenchError, match="read_node"):
        await workbench.read_node("Route A")
    module.db_client.wb_save_candidate.assert_not_awaited()

    await workbench.save_candidate(
        edits=[{"old": "const renamedRoute =", "new": declaration}],
        expected_revision=current["revision"],
    )
    restored = await workbench.get_context(section="editing")
    assert restored["state"] == "saved_candidate" and not restored["parse_errors"]
    assert restored["code"] == initial["code"]
    assert len(restored["transitions"]) == 5


async def test_preloaded_context_keeps_recovery_available_through_existing_tool(
    workbench, monkeypatch
):
    from api.services.agent_workbench.codex_client import CodexClient

    workbench.preloaded_context = {}
    workbench.intent = "build"
    monkeypatch.setattr(module.db_client, "wb_heartbeat", AsyncMock())
    assert "get_context" not in {spec["name"] for spec in workbench.specs()}
    current = await workbench.call("read_node", {})
    assert current["state"] == "initial" and len(current["nodes"]) == 4
    await workbench.call(
        "save_candidate",
        {
            "summary": "Clarify the current prompt",
            "edits": [{"old": "Ask which route.", "new": "Choose a route."}],
            "expected_revision": current["revision"],
        },
    )
    saved = await workbench.call("read_node", {})
    assert "Choose a route." in saved["code"]
    assert saved["revision"] != current["revision"]
    client = CodexClient(workbench, AsyncMock())
    response = await client.handle_request(
        "item/tool/call",
        {
            "tool": "save_candidate",
            "arguments": {
                "summary": "Malformed edit envelope",
                "edits": [
                    {
                        "old": "Choose a route.",
                        "new": "Invalid update.",
                        "expected_revision": saved["revision"],
                    }
                ],
            },
        },
    )
    assert response["success"] is False
    assert "$.edits[0]:" in response["contentItems"][0]["text"]
    assert "expected_revision" in response["contentItems"][0]["text"]
    response = await client.handle_request(
        "item/tool/call",
        {
            "tool": "save_candidate",
            "arguments": {
                "summary": "Incorrect JSON type",
                "edits": "private SDK fragment " * 500,
            },
        },
    )
    assert response["success"] is False
    message = response["contentItems"][0]["text"]
    assert "$.edits: expected JSON array" in message
    assert "private SDK fragment" not in message
    assert len(message) < 200
    assert (await workbench.call("read_node", {})) == saved
    module.db_client.wb_save_candidate.assert_awaited_once()


@pytest.mark.parametrize("invalid_old", ["absent SDK text", "wf.edge("])
async def test_failed_sdk_batch_preserves_the_previous_retained_submission(
    workbench, invalid_old
):
    await workbench.get_context()
    with pytest.raises(WorkbenchError):
        await workbench.save_candidate(
            edits=[{"old": "const route_a =", "new": "const brokenRef ="}]
        )
    retained = await workbench.get_context(section="editing")
    with pytest.raises(WorkbenchError, match="edit 2 matched"):
        await workbench.save_candidate(
            edits=[
                {"old": "const brokenRef =", "new": "const route_a ="},
                {"old": invalid_old, "new": "unwanted"},
            ]
        )
    assert (await workbench.get_context(section="editing")) == retained
    module.db_client.wb_save_candidate.assert_not_awaited()


async def test_retained_rename_uses_current_names_and_carries_only_declared_removals(
    workbench, graph
):
    await workbench.get_context()
    with pytest.raises(WorkbenchError, match="Original graph content was removed"):
        await workbench.save_candidate(
            node_updates=[{"name": "Route A", "data": {"name": "Reviewed route"}}]
        )
    current = await workbench.get_context(section="editing")
    assert current["state"] == "retained_submission"
    assert {n["name"] for n in current["nodes"]} == {
        "Entry",
        "Reviewed route",
        "Route B",
        "Finish",
    }
    assert not current["parse_errors"]
    with pytest.raises(WorkbenchError, match="field-update batch was not applied"):
        await workbench.save_candidate(
            node_updates=[
                {"name": "Route B", "prompt_append": "Must not accumulate."},
                {"name": "Route A", "prompt_append": "Stale reference."},
            ]
        )
    assert (await workbench.get_context(section="editing")) == current
    names = {n["id"]: n["data"]["name"] for n in graph["nodes"]}
    removals = {
        "nodes": ["Route A"],
        "transitions": [
            {
                "source": names[e["source"]],
                "target": names[e["target"]],
                "label": e["data"]["label"],
            }
            for e in graph["edges"]
            if "2" in {e["source"], e["target"]}
        ],
    }
    assert current["removals_required_for_current_graph"] == removals
    await workbench.save_candidate(
        node_updates=[{"name": "Reviewed route", "prompt_append": "Wait for review."}],
        removals=removals,
        expected_revision=current["revision"],
    )
    saved = await workbench.get_context(section="editing")
    assert saved["removals"] == removals
    assert saved["removals_required_for_current_graph"] == removals
    assert "Must not accumulate." not in saved["code"]
    await workbench.save_candidate(
        node_updates=[{"name": "Route B", "prompt_append": "Wait for B."}]
    )
    assert module.db_client.wb_save_candidate.await_count == 2
    # Prior deletion approval never authorizes a different deletion or rename.
    with pytest.raises(WorkbenchError, match="Original graph content was removed"):
        await workbench.save_candidate(
            node_updates=[{"name": "Route B", "data": {"name": "Unauthorized rename"}}]
        )
    assert module.db_client.wb_save_candidate.await_count == 2


async def test_editing_view_pages_exact_code_even_when_syntax_is_invalid(workbench):
    await workbench.get_context()
    with pytest.raises(WorkbenchError):
        await workbench.save_candidate(
            edits=[{"old": "const route_a =", "new": "const ="}]
        )
    source = workbench.last_submission["code"]
    pieces, revisions, offset = [], set(), 0
    while offset is not None:
        page = await workbench.get_context(section="editing", offset=offset, limit=701)
        pieces.append(page["code"])
        revisions.add(page["revision"])
        assert page["offset"] == offset and page["total_characters"] == len(source)
        assert page["parse_errors"]
        offset = page["next_offset"]
    assert "".join(pieces) == source
    assert len(revisions) == 1
    assert workbench.last_submission["code"] == source
    module.db_client.wb_save_candidate.assert_not_awaited()
