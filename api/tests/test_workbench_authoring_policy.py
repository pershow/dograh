"""Authoring defaults through the real SDK parser, save and preservation checks."""

import copy
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import SimulationCase
from api.services.agent_workbench import basis, jobs
from api.services.agent_workbench import tools as module
from api.services.agent_workbench.authoring import (
    SDK_EXAMPLE,
    authoring_context,
    node_routing_context,
)
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.evaluation import (
    check_assertions,
    resolve_node_references,
)
from api.services.agent_workbench.evaluation_policy import EvaluationPolicy
from api.services.agent_workbench.graph_edits import transitions
from api.services.agent_workbench.sop import (
    NODE_SCOPE,
    ROUTING,
    bind_source,
    coverage_report,
    preserved_sections,
    source_mapping,
    source_sections,
    without_source,
)

TOOL_ID = "aef13169-2897-4dde-9802-1a8d2b17a001"
SOP = """# 图书馆设备预约
确认借阅证号、设备型号、取件地点和预约时段。一次报告多项时一起接收，只补缺失项。
# 登记和核对
预约费12元，先说明费用及取件规则，再复述全部预约信息等待新的确认。
确认后调用reserve_equipment；只有返回成功和预约编号才可宣称预约成功。
# 异常和挽回
工具无库存时询问是否更换时段，不可伪称成功。用户换取件地点时保留型号和时段。
普通询问费用不是拒绝。同一原因最多3次实际挽回，插问和切换地点不重置。
读卡失败同一步最多1次重试，仍失败则礼貌结束。用户要求停止立即结束。
# 结束
成功时复述预约编号；取消或失败按实际原因告别，不声称预约成功。
"""


@pytest.fixture
def workbench_factory(monkeypatch):
    for name in (
        "get_documents_for_organization",
        "get_credentials_for_organization",
        "get_recordings",
        "list_provider_connections",
    ):
        monkeypatch.setattr(module.db_client, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(
        module.db_client,
        "get_tools_for_organization",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    tool_uuid=TOOL_ID,
                    name="reserve_equipment",
                    description="Reserve equipment",
                    category="http_api",
                )
            ]
        ),
    )
    monkeypatch.setattr(module, "prepare_models", AsyncMock())
    monkeypatch.setattr(
        module, "dependency_report", AsyncMock(return_value={"ready": True})
    )
    monkeypatch.setattr(
        module, "validate_workflow_tool_name_collisions", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        module.db_client,
        "wb_save_candidate",
        AsyncMock(return_value={"id": "c", "fingerprint": "f"}),
    )

    def factory(graph=None, text=SOP):
        wb = module.WorkbenchTools(
            1,
            "isolated",
            {"id": "j", "lease_token": "l", "input": {"mode": "build", "text": text}},
            {
                "target": {
                    "name": "设备预约",
                    "workflow_json": graph or {"nodes": [], "edges": []},
                },
                "target_fingerprint": "f",
                "session": {"workflow_id": 999},
                "assets": [],
            },
        )
        wb.list_attachments = AsyncMock(return_value=[])
        return wb

    return factory


async def example_graph():
    parsed = await module.parse_code(SDK_EXAMPLE)
    assert parsed["ok"], parsed
    graph = parsed["workflow"]
    next(n for n in graph["nodes"] if n["type"] == "agentNode")["data"][
        "tool_uuids"
    ] = [TOOL_ID]
    return graph


@pytest.mark.parametrize("direction,expected", [("inbound", "inbound"), ("outbound", "outbound"), ("both", "")])
async def test_source_backed_creation_direction_reaches_saved_candidate(workbench_factory, direction, expected):
    wb = workbench_factory(text="创建 Agent")
    wb.creation_direction = {"status": "resolved", "direction": direction}
    await wb.save_candidate(code=SDK_EXAMPLE)
    snapshot = module.db_client.wb_save_candidate.call_args.args[4]
    assert snapshot["template_context_variables"]["direction"] == expected


def saved_graph():
    return module.db_client.wb_save_candidate.call_args.args[4]["workflow_json"]


async def test_new_context_example_saves_four_nodes_with_complete_source_and_effects(
    workbench_factory,
):
    wb = workbench_factory()
    context = await wb.get_context()
    assert context["authoring"]["scope"] == "new_agent"
    assert context["sdk_example"] == SDK_EXAMPLE
    graph = await example_graph()
    assignments = {
        "Conversation": [s["id"] for s in wb.source],
        "Closing": [wb.source[-1]["id"]],
    }
    result = await wb.save_candidate(
        code=await module.generate_code(graph, workflow_name="设备预约"),
        source_sections=assignments,
        summary="完整业务在主 Agent 内，保留预约工具及失败分支",
    )
    stored = saved_graph()
    assert Counter(n["type"] for n in stored["nodes"]) == Counter(
        ["startCall", "agentNode", "endCall", "globalNode"]
    )
    main = next(n for n in stored["nodes"] if n["type"] == "agentNode")
    assert main["data"]["tool_uuids"] == [TOOL_ID]
    assert all(s["text"] in main["data"]["prompt"] for s in wb.source)
    assert result["sop_coverage"] == {"total": 4, "preserved": 4, "complete": True}
    assert all(
        n["data"]["add_global_prompt"]
        for n in stored["nodes"]
        if n["type"] != "globalNode"
    )
    terminal_edges = [e for e in stored["edges"] if e["source"] == main["id"]]
    assert len(terminal_edges) == 2 and len({e["target"] for e in terminal_edges}) == 1
    # Both real tool results and error fixtures are still required by simulation.
    case = SimulationCase(
        name="reserve",
        user_messages=["确认"],
        assertions=[{"kind": "completed", "value": True}],
    )
    with pytest.raises(WorkbenchError, match="fixtures"):
        EvaluationPolicy(case, None).validate(stored)


@pytest.mark.parametrize("count,webhook", [(3, False), (7, False), (5, True)])
async def test_explicit_topology_and_post_call_effect_are_not_blocked(
    workbench_factory, count, webhook
):
    graph = await example_graph()
    if count == 3:
        graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "globalNode"]
    elif webhook:
        graph["nodes"].append(
            {
                "id": "receipt",
                "type": "webhook",
                "position": {"x": 0, "y": 600},
                "data": {
                    "name": "Receipt",
                    "endpoint_url": "https://library.example/receipt",
                    "http_method": "POST",
                },
            }
        )
    else:
        main = next(n for n in graph["nodes"] if n["type"] == "agentNode")
        for i in range(3):
            graph["nodes"].append(
                {
                    "id": f"special{i}",
                    "type": "agentNode",
                    "position": {"x": 300, "y": i * 200},
                    "data": {
                        "name": f"Independent {i}",
                        "prompt": f"Handle independent requirement {i}.",
                    },
                }
            )
            graph["edges"].append(
                {
                    "id": f"route{i}",
                    "source": main["id"],
                    "target": f"special{i}",
                    "data": {
                        "label": f"special_{i}",
                        "condition": f"Special requirement {i} applies",
                    },
                }
            )
            graph["edges"].append(
                {
                    "id": f"return{i}",
                    "source": f"special{i}",
                    "target": main["id"],
                    "data": {
                        "label": f"return_{i}",
                        "condition": "Independent requirement completed",
                    },
                }
            )
    wb = workbench_factory(text=f"明确要求{count}个物理节点，保留独立能力。")
    await wb.save_candidate(
        code=await module.generate_code(graph, workflow_name="Explicit"),
        summary="用户指定结构",
    )
    assert len(saved_graph()["nodes"]) == count
    assert transitions(saved_graph()) == transitions(graph)
    if webhook:
        assert (
            next(n for n in saved_graph()["nodes"] if n["type"] == "webhook")["data"][
                "endpoint_url"
            ]
            == "https://library.example/receipt"
        )


async def test_local_edit_preserves_legacy_source_guidance_and_topology(
    workbench_factory,
):
    graph = await example_graph()
    sections = source_sections(SOP)
    graph = bind_source(
        graph,
        sections,
        {"Conversation": [s["id"] for s in sections], "Closing": [sections[-1]["id"]]},
    )
    for node in graph["nodes"]:
        node["data"]["prompt"] = ROUTING.sub(
            "\n[SOP_ROUTING]\nLegacy routing; keep exactly.\n[/SOP_ROUTING]",
            node["data"]["prompt"],
        )
    # An existing non-default topology must remain non-default after a field edit.
    graph["nodes"] = [n for n in graph["nodes"] if n["type"] != "globalNode"]
    wb = workbench_factory(graph, text="仅修改开场，不改业务流程")
    wb.base_code = await module.generate_code(
        without_source(graph), workflow_name="Existing"
    )
    await wb.save_candidate(
        node_updates=[{"name": "Opening", "prompt_append": "请用简短问候。"}],
        summary="开场局部修改",
    )
    stored = saved_graph()
    assert len(stored["nodes"]) == 3
    assert transitions(stored) == transitions(graph)
    for old, new in zip(graph["nodes"], stored["nodes"]):
        if old["data"]["name"] != "Opening":
            assert new["data"]["prompt"] == old["data"]["prompt"]
    assert coverage_report(stored, sections)["complete"]


async def test_bound_source_round_trip_scopes_shared_material_to_each_node_role():
    graph = await example_graph()
    sections = source_sections(
        "Create an Agent and save its tests. Opening: welcome.\n"
        "# Intake\nCollect a destination and a delivery window.\n"
        "# Schedule\nThe window must include both start and end times.\n"
        "# Closing\nSay goodbye only after the receipt tool succeeds."
    )
    # The same verbatim source may legitimately be mapped to several roles.
    assignments = {
        n["data"]["name"]: [s["id"] for s in sections] for n in graph["nodes"]
    }
    bound = bind_source(graph, sections, assignments)
    assert preserved_sections(bound) == sections
    assert without_source(bound) == graph
    assert source_mapping(bound) == assignments
    for node in bound["nodes"]:
        prompt = node["data"]["prompt"]
        assert prompt.rindex("[/SOP_SOURCE]") < prompt.index("[SOP_ROUTING]")
        assert NODE_SCOPE[node["type"]] in prompt
    # SDK serialization and repeated binding must not lose precision requirements,
    # leak injected rules into authored prompts, or accumulate multiple contracts.
    parsed = await module.parse_code(await module.generate_code(bound))
    assert parsed["ok"]
    assert preserved_sections(parsed["workflow"]) == sections
    assert bind_source(bound, sections, assignments) == bound


async def test_changed_node_refreshes_contract_without_upgrading_untouched_nodes():
    graph = await example_graph()
    sections = source_sections(SOP)
    assignments = {
        "Conversation": [s["id"] for s in sections],
        "Closing": [sections[-1]["id"]],
    }
    legacy = bind_source(graph, sections, assignments)
    for node in legacy["nodes"]:
        node["data"]["prompt"] = ROUTING.sub(
            "\n[SOP_ROUTING]\nPrevious contract.\n[/SOP_ROUTING]",
            node["data"]["prompt"],
        )
    edited = without_source(legacy)
    next(n for n in edited["nodes"] if n["type"] == "agentNode")["data"][
        "prompt"
    ] += "\nPreserve the customer's supplied facts when asking a question."
    rebound = bind_source(edited, sections, assignments, previous=legacy)
    for old, new in zip(legacy["nodes"], rebound["nodes"]):
        if new["type"] == "agentNode":
            assert NODE_SCOPE["agentNode"] in new["data"]["prompt"]
            assert "Previous contract." not in new["data"]["prompt"]
        else:
            assert new == old
    assert preserved_sections(rebound) == sections
    assert without_source(rebound) == edited
    assert bind_source(rebound, sections, assignments, previous=rebound) == rebound


@pytest.mark.parametrize(
    "context,expected",
    [
        ({}, "来电者"),
        ({"contact": None}, "来电者"),
        ({"contact": ""}, "来电者"),
        ({"contact": "叶青"}, "叶青"),
    ],
)
async def test_saved_template_opening_uses_existing_runtime_renderer(
    workbench_factory, context, expected
):
    from api.services.workflow.dto import ReactFlowDTO
    from api.services.workflow.pipecat_engine import PipecatEngine
    from api.services.workflow.workflow_graph import WorkflowGraph

    graph = await example_graph()
    start = next(n for n in graph["nodes"] if n["type"] == "startCall")
    start["data"][
        "greeting"
    ] = "您好，{{contact | fallback:来电者}}，这里是设备报修服务。"
    wb = workbench_factory(text="按所提供配置新建设备报修服务。")
    await wb.save_candidate(code=await module.generate_code(graph), summary="模板开场")
    engine = PipecatEngine(
        workflow=WorkflowGraph(ReactFlowDTO.model_validate(saved_graph())),
        call_context_vars=context,
    )
    assert engine.get_start_greeting() == (
        "text",
        f"您好，{expected}，这里是设备报修服务。",
    )


@pytest.mark.parametrize("mode", ["recorded", "dynamic"])
async def test_local_business_edit_preserves_requested_opening_mode(
    workbench_factory, monkeypatch, mode
):
    graph = await example_graph()
    start = next(n for n in graph["nodes"] if n["type"] == "startCall")
    if mode == "recorded":
        start["data"].update(
            greeting_type="audio", greeting_recording_id="42", greeting=""
        )
        monkeypatch.setattr(
            module.db_client,
            "get_recordings",
            AsyncMock(return_value=[SimpleNamespace(recording_id="42")]),
        )
    else:
        start["data"].update(
            greeting="", delayed_start=True, delayed_start_duration=1.7
        )
        start["data"]["prompt"] = "根据当前客户问题动态回应，不使用固定开场。"
    wb = workbench_factory(
        graph, text="仅在业务节点增加简短表达要求，保留既有开场和结构。"
    )
    wb.base_code = await module.generate_code(graph)
    await wb.save_candidate(
        node_updates=[{"name": "Conversation", "prompt_append": "表达简短。"}],
        summary="局部编辑",
    )
    actual = next(n for n in saved_graph()["nodes"] if n["type"] == "startCall")
    assert actual["data"] == start["data"]
    assert transitions(saved_graph()) == transitions(graph)


async def test_routing_scope_tracks_real_edges_and_only_refreshes_affected_nodes():
    graph = await example_graph()
    sections = source_sections(SOP)
    assignments = {
        n["data"]["name"]: [s["id"] for s in sections] for n in graph["nodes"]
    }
    bound = bind_source(graph, sections, assignments)
    opening = next(n for n in bound["nodes"] if n["type"] == "startCall")
    scope = node_routing_context(bound, opening)
    assert "begin_conversation" in scope
    assert "finish_success" not in scope and "finish_other" not in scope
    changed = without_source(bound)
    next(e for e in changed["edges"] if e["source"] == opening["id"])["data"][
        "label"
    ] = "Handle Request"
    rebound = bind_source(changed, sections, assignments, previous=bound)
    for old, new in zip(bound["nodes"], rebound["nodes"]):
        if new["id"] == opening["id"]:
            assert "handle_request → Conversation" in new["data"]["prompt"]
            assert "begin_conversation → Conversation" not in new["data"]["prompt"]
        else:
            assert new == old
    assert preserved_sections(rebound) == sections


async def test_requested_restructure_preserves_all_sources_and_declares_removals(
    workbench_factory,
):
    desired = await example_graph()
    old = copy.deepcopy(desired)
    main = next(n for n in old["nodes"] if n["type"] == "agentNode")
    for i in range(2):
        old["nodes"].append(
            {
                "id": f"stage{i}",
                "type": "agentNode",
                "position": {"x": 100, "y": i * 200},
                "data": {"name": f"Stage {i}", "prompt": "Independent internal phase."},
            }
        )
        old["edges"].append(
            {
                "id": f"stage{i}",
                "source": main["id"],
                "target": f"stage{i}",
                "data": {"label": f"stage_{i}", "condition": "Phase applies"},
            }
        )
    sections = source_sections(SOP)
    old = bind_source(
        old,
        sections,
        {
            "Conversation": [s["id"] for s in sections[:2]],
            "Stage 0": [sections[2]["id"]],
            "Stage 1": [sections[3]["id"]],
        },
    )
    wb = workbench_factory(old, text="重构为默认四节点，保留全部业务规则和工具。")
    wb.base_code = await module.generate_code(
        without_source(old), workflow_name="Library"
    )
    new_code = await module.generate_code(desired, workflow_name="Library")
    mapping = {"Conversation": [s["id"] for s in sections]}
    with pytest.raises(WorkbenchError, match="removed"):
        await wb.save_candidate(
            edits=[{"old": wb.base_code, "new": new_code}],
            source_sections=mapping,
            summary="合并内部阶段",
        )
    removed = transitions(old) - transitions(desired)
    await wb.save_candidate(
        removals={
            "nodes": ["Stage 0", "Stage 1"],
            "transitions": [
                dict(zip(("source", "target", "label"), edge))
                for edge in removed.elements()
            ],
        },
        summary="用户要求将两个阶段并入主 Agent，保留全部资料和副作用",
    )
    assert len(saved_graph()["nodes"]) == 4
    assert source_mapping(saved_graph()) == mapping
    assert coverage_report(saved_graph(), sections)["complete"]
    assert next(n for n in saved_graph()["nodes"] if n["type"] == "agentNode")["data"][
        "tool_uuids"
    ] == [TOOL_ID]


async def test_internal_stage_behavior_requires_more_than_node_visits():
    graph = await example_graph()
    case = SimulationCase(
        name="complete booking",
        user_messages=["登记好了", "确认"],
        assertions=[
            {"kind": "node_visited", "value": "Conversation"},
            {"kind": "contains", "value": "12元", "turn": 1},
            {"kind": "contains", "value": "预约编号", "turn": 2},
            {"kind": "tool_called", "value": "reserve_equipment"},
        ],
    )
    main = next(n for n in graph["nodes"] if n["type"] == "agentNode")
    checks = check_assertions(
        case,
        [
            {"role": "assistant", "text": "已完成", "turn": 1},
            {"role": "assistant", "text": "再见", "turn": 2},
        ],
        [{"type": "node_transition", "payload": {"node_id": main["id"]}}],
        {},
        True,
        [],
        resolve_node_references(case, graph),
    )
    assert [c["passed"] for c in checks] == [True, False, False, False]
    with pytest.raises(WorkbenchError, match="existing node"):
        resolve_node_references(
            case.model_copy(
                update={
                    "assertions": [
                        case.assertions[0].model_copy(
                            update={"value": "internal fee stage"}
                        )
                    ]
                }
            ),
            graph,
        )
    assert authoring_context(graph, automatic_repair=True)["scope"] == "local_repair"


@pytest.mark.parametrize("explicit", ["", "明确要求7个节点，业务阶段独立。\n"])
async def test_structured_build_entry_retains_policy_and_user_structure(
    monkeypatch, explicit
):
    from api.services.agent_workbench import creation

    monkeypatch.setattr(creation, "resolve_direction", AsyncMock(return_value={
        "intent": "build", "status": "resolved", "direction": "inbound", "citations": [],
    }))
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", AsyncMock())
    request = explicit + SOP
    captured = []
    monkeypatch.setattr(
        basis,
        "ensure_basis",
        AsyncMock(
            return_value={
                "sources": [],
                "fingerprint": "basis",
                "target_fingerprint": "f",
                "memory_revision": "m",
                "warnings": [],
            }
        ),
    )

    async def memory(self):
        self.agent_memory_view = {"confirmed": []}

    async def context(self):
        return {
            "source_sections": self.source,
            "authoring": authoring_context(self.base["workflow_json"]),
        }

    monkeypatch.setattr(module.WorkbenchTools, "load_agent_memory", memory)
    monkeypatch.setattr(module.WorkbenchTools, "get_context", context)
    monkeypatch.setattr(
        module.WorkbenchTools, "cached_run_evidence", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        module.WorkbenchTools, "list_attachments", AsyncMock(return_value=[])
    )

    class Client:
        def __init__(self, tools, event):
            self.tools = tools

        async def run(self, prompt, *args, **kwargs):
            captured.append(prompt)
            self.tools.candidate_ids, self.tools.suite_ids = ["c"], ["s"]
            return {}

    monkeypatch.setattr(jobs, "CodexClient", Client)
    await jobs._build(
        1,
        "isolated",
        {"id": "j", "lease_token": "l", "input": {"text": request, "mode": "build"}},
        {
            "target": {"workflow_json": {"nodes": [], "edges": []}},
            "target_fingerprint": "f",
            "session": {"model_protocol": "chat"},
        },
        SimpleNamespace(llm=None),
        request,
    )
    current = captured[0].split("CURRENT USER REQUEST:\n")[-1]
    assert (
        "startCall + one complete-conversation agentNode + endCall + globalNode"
        in current
    )
    assert "unless the user specifies another structure" in current
    assert "existing local edits preserve the topology" in current
    # Source compaction retains original requirements; it cannot erase the exception.
    if explicit:
        assert explicit.strip() in captured[0]
