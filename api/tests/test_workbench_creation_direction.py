"""Creation prerequisites use original evidence and stop before authoring."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jsonschema
import pytest

from api.services.agent_workbench import basis, creation, jobs
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint


def sources(*texts):
    return {
        "fingerprint": "frozen",
        "memory_revision": "m",
        "target_fingerprint": "empty",
        "warnings": [],
        "sources": [
            {
                "id": f"request:{i}",
                "kind": "user_request",
                "text": text,
                "title": "original",
                "content_hash": fingerprint(text),
            }
            for i, text in enumerate(texts)
        ],
    }


def request(mode="auto", text="请创建 Agent"):
    return {
        "id": "new",
        "lease_token": "lease",
        "input": {"text": text, "mode": mode},
        "output": {},
    }


def report(**changes):
    return {
        "intent": "build",
        "status": "resolved",
        "direction": "outbound",
        "reason": "由公司发起联系",
        "question": "",
        "citations": [{"id": "basis:request:0:0", "quote": "联系客户"}],
        **changes,
    }


@pytest.mark.parametrize("direction", ["inbound", "outbound", "both"])
async def test_exact_original_evidence_is_retained_and_resolved_facts_are_not_asked(
    monkeypatch, direction
):
    expected = report(direction=direction)

    async def decide(name, instructions, schema, evidence, *args, validate, reader):
        assert name == "resolve_creation_direction"
        assert evidence["records"][0]["text"] == "联系客户"
        jsonschema.validate(expected, schema)
        validate(expected)
        return deepcopy(expected)

    monkeypatch.setattr(creation, "decision", decide)
    result = await creation.resolve_direction(
        request(), {}, sources("联系客户"), None, AsyncMock(), "responses"
    )
    assert result == {**expected, "basis_fingerprint": "frozen"}
    assert result["question"] == ""


@pytest.mark.parametrize(
    "bad",
    [
        {"citations": [{"id": "basis:request:0:0", "quote": "invented"}]},
        {"citations": []},
        {"question": "请再次确认外呼？"},
        {"status": "missing", "question": "呼入还是呼出？"},
        {"status": "conflict", "direction": None, "question": "以哪份为准？"},
        {"intent": "other"},
    ],
)
async def test_ungrounded_or_inconsistent_decisions_are_rejected(monkeypatch, bad):
    async def decide(name, instructions, schema, evidence, *args, validate, reader):
        candidate = report(**bad)
        jsonschema.validate(candidate, schema)
        validate(candidate)
        return candidate

    monkeypatch.setattr(creation, "decision", decide)
    with pytest.raises(WorkbenchError):
        await creation.resolve_direction(
            request(), {}, sources("联系客户"), None, AsyncMock(), "chat"
        )


async def test_long_sources_must_be_read_before_missing_information_can_be_reported(
    monkeypatch,
):
    original = sources("资料" * 18000 + "客户打入客服电话")
    final = report(
        direction="inbound",
        citations=[{"id": "basis:request:0:36000", "quote": "客户打入客服电话"}],
    )

    async def decide(name, instructions, schema, evidence, *args, validate, reader):
        assert evidence["unread_source_ids"]
        with pytest.raises(WorkbenchError, match="未读取"):
            validate(
                report(
                    status="missing",
                    direction=None,
                    question="呼入还是呼出？",
                    citations=[],
                )
            )
        for key in list(reader.pending):
            await reader.call("read_review_source", {"id": key})
        validate(final)
        return final

    monkeypatch.setattr(creation, "decision", decide)
    assert (
        await creation.resolve_direction(
            request(), {}, original, None, AsyncMock(), "chat"
        )
    )["direction"] == "inbound"


async def test_short_answer_uses_prior_question_and_human_originals(monkeypatch):
    prior = {
        "id": "prior",
        "status": "awaiting_input",
        "output": {
            "task_mode": "build",
            "question": "谁发起通话？",
            "creation_direction": {"status": "missing"},
            "answer": "无依据的助手结论：外呼",
        },
    }

    async def decide(name, instructions, schema, evidence, *args, validate, reader):
        assert evidence["pending_creation_question"] == {
            "job_id": "prior",
            "question": "谁发起通话？",
        }
        assert "无依据的助手结论" not in str(evidence)
        assert [r["text"] for r in evidence["records"]] == [
            "创建维修服务 Agent",
            "由客户打进来",
        ]
        result = report(
            direction="inbound",
            citations=[{"id": "basis:request:1:0", "quote": "由客户打进来"}],
        )
        validate(result)
        return result

    monkeypatch.setattr(creation, "decision", decide)
    result = await creation.resolve_direction(
        request(text="由客户打进来"),
        {"jobs": [prior]},
        sources("创建维修服务 Agent", "由客户打进来"),
        None,
        AsyncMock(),
        "responses",
    )
    assert result["direction"] == "inbound" and not result["question"]


@pytest.mark.parametrize("status", ["missing", "conflict"])
async def test_unresolved_creation_returns_question_before_builder_and_artifacts(
    monkeypatch, status
):
    monkeypatch.setattr(
        basis, "ensure_basis", AsyncMock(return_value=sources("创建客服"))
    )
    monkeypatch.setattr(jobs.WorkbenchTools, "load_agent_memory", AsyncMock())
    monkeypatch.setattr(
        creation,
        "resolve_direction",
        AsyncMock(
            return_value=report(
                status=status, direction=None, question="请明确由谁发起通话。"
            )
        ),
    )
    builder = AsyncMock()
    monkeypatch.setattr(jobs.CodexClient, "run", builder)
    save = AsyncMock()
    monkeypatch.setattr(jobs.db_client, "wb_save_candidate", save)
    context = {
        "target": {"workflow_json": {"nodes": []}},
        "target_fingerprint": "empty",
        "session": {"model_protocol": "chat"},
    }
    result = await jobs._build(
        1, "s", request(), context, SimpleNamespace(llm=None), "创建客服"
    )
    assert result["outcome"] == "needs_input" and result["task_mode"] == "build"
    assert result["question"] == "请明确由谁发起通话。"
    assert result["agent_modified"] is False
    assert jobs.task_result_status("build", result) == "awaiting_input"
    builder.assert_not_called()
    save.assert_not_called()


@pytest.mark.parametrize(
    "direction,expected",
    [("inbound", "inbound"), ("outbound", "outbound"), ("both", "")],
)
def test_direction_default_preserves_other_variables_and_never_conflicts(
    direction, expected
):
    snapshot = {"template_context_variables": {"shop": "source name"}}
    creation.bind_direction(snapshot, report(direction=direction))
    assert snapshot["template_context_variables"] == {
        "shop": "source name",
        "direction": expected,
    }
    before = deepcopy(snapshot)
    creation.bind_direction(snapshot, report(direction=direction))
    assert snapshot == before
    with pytest.raises(WorkbenchError, match="不一致"):
        creation.bind_direction(
            {
                "template_context_variables": {
                    "direction": "outbound" if expected != "outbound" else "inbound"
                }
            },
            report(direction=direction),
        )


async def test_new_agent_source_retrieval_keeps_short_human_answers(monkeypatch):
    original = {
        "id": "old",
        "source_type": "request",
        "content_hash": "hash",
        "created_at": "1",
        "title": "是的，接听来电",
        "session_id": "old-session",
    }

    async def index(org, sid, *, source_type, offset, limit):
        return {
            "items": [original] if source_type == "request" else [],
            "next_offset": None,
        }

    monkeypatch.setattr(basis.db_client, "wb_memory_sources", index)
    monkeypatch.setattr(
        basis.db_client,
        "wb_memory_source",
        AsyncMock(return_value={"content": "是的，接听来电"}),
    )
    from api.services.agent_workbench import test_tasks

    monkeypatch.setattr(
        test_tasks, "decision", AsyncMock(return_value={"source_ids": []})
    )
    found = await basis.discover_originals(
        1,
        "s",
        request(text="继续"),
        {"workflow_json": {"nodes": []}},
        [],
        None,
        AsyncMock(),
        "chat",
    )
    assert [s["text"] for s in found] == ["是的，接听来电"]
    assert found[0]["session_id"] == "old-session"
    # An established Agent still uses scoped relevance selection.
    found = await basis.discover_originals(
        1,
        "s",
        request(text="解释此节点"),
        {"workflow_json": {"nodes": [{"data": {"name": "Stage"}}]}},
        [],
        None,
        AsyncMock(),
        "chat",
    )
    assert found == []


@pytest.mark.parametrize(
    "mode,existing,decision_intent",
    [
        ("auto", False, "build"),
        ("auto", False, "other"),
        ("analyze", False, None),
        ("build", True, None),
    ],
)
async def test_gate_routes_creation_without_changing_analysis_or_existing_agents(
    monkeypatch, mode, existing, decision_intent
):
    from api.services.agent_workbench import tools as tools_module

    monkeypatch.setattr(
        basis, "ensure_basis", AsyncMock(return_value=sources("联系客户"))
    )
    resolve = AsyncMock(
        return_value=report(
            intent=decision_intent,
            status="resolved" if decision_intent == "build" else "not_applicable",
        )
    )
    monkeypatch.setattr(creation, "resolve_direction", resolve)
    progress = AsyncMock()
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", progress)

    async def memory(self):
        self.agent_memory_view = {}

    monkeypatch.setattr(tools_module.WorkbenchTools, "load_agent_memory", memory)
    monkeypatch.setattr(
        tools_module.WorkbenchTools, "get_context", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        tools_module.WorkbenchTools, "cached_run_evidence", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        tools_module.WorkbenchTools, "list_attachments", AsyncMock(return_value=[])
    )
    observed = []

    class Client:
        def __init__(self, tools, event):
            self.tools = tools

        async def run(self, prompt, *args, **kwargs):
            observed.append((self.tools, prompt))
            if self.tools.intent == "build":
                self.tools.candidate_ids, self.tools.suite_ids = (
                    ["candidate"],
                    ["suite"],
                )
            else:
                self.tools.analysis_answer = "分析答复"
            return {}

    monkeypatch.setattr(jobs, "CodexClient", Client)
    context = {
        "target": {
            "workflow_json": {
                "nodes": [
                    {"id": "1", "type": "agentNode", "data": {"name": "existing"}}
                ]
                if existing
                else []
            }
        },
        "target_fingerprint": "target",
        "session": {"model_protocol": "chat"},
    }
    job = request(mode)
    result = await jobs._build(
        1, "s", job, context, SimpleNamespace(llm=None), job["input"]["text"]
    )
    assert resolve.await_count == (0 if existing or mode == "analyze" else 1)
    if decision_intent == "build":
        assert result["outcome"] == "build"
        assert result["creation_direction"]["direction"] == "outbound"
        assert observed[0][0].creation_direction == result["creation_direction"]
        assert job["output"]["creation_direction"] == result["creation_direction"]
        assert "who speaks first" in observed[0][1]
        progress.assert_awaited_once()
    elif decision_intent == "other" or mode == "analyze":
        assert result["outcome"] == "analysis"
        assert "set_task_intent" not in {s["name"] for s in observed[0][0].specs()}
        progress.assert_not_called()
    else:
        assert result["outcome"] == "build" and "creation_direction" not in result
        progress.assert_not_called()


async def test_job_does_not_start_candidate_review_when_creation_needs_input(
    monkeypatch,
):
    from api.services.agent_workbench import test_tasks

    job = {**request("build"), "kind": "build"}
    context = {"target": {}, "session": {"model_protocol": "responses"}}
    monkeypatch.setattr(jobs.db_client, "wb_context", AsyncMock(return_value=context))
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(
            return_value=SimpleNamespace(
                effective=SimpleNamespace(
                    llm=SimpleNamespace(api_key="test", provider="openai")
                ),
                snapshot={},
            )
        ),
    )
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    monkeypatch.setattr(test_tasks, "route_test_request", AsyncMock(return_value=False))
    expected = {
        "outcome": "needs_input",
        "task_mode": "build",
        "question": "谁发起通话？",
    }
    monkeypatch.setattr(jobs, "_build", AsyncMock(return_value=expected))
    complete, progress = AsyncMock(), AsyncMock()
    monkeypatch.setattr(jobs, "_complete_build", complete)
    monkeypatch.setattr(jobs.db_client, "wb_job_progress", progress)
    assert await jobs._perform(1, "s", job) == expected
    complete.assert_not_called()
    progress.assert_not_called()


async def test_test_router_receives_creation_question_mode(monkeypatch):
    from api.services.agent_workbench import test_tasks

    prior = {
        "id": "prior",
        "status": "awaiting_input",
        "input": {"mode": "auto", "text": "创建客服 Agent"},
        "output": {
            "outcome": "needs_input",
            "task_mode": "build",
            "intent": "build",
            "question": "谁发起通话？",
        },
    }

    async def decide(name, instructions, schema, evidence, *args):
        assert evidence["recent_requests"][0]["task_mode"] == "build"
        assert evidence["recent_requests"][0]["intent"] == "build"
        return {"intent": "other", "source_job_id": None, "reason": "回答创建问题"}

    monkeypatch.setattr(test_tasks, "decision", decide)
    restore = AsyncMock()
    monkeypatch.setattr(test_tasks.db_client, "wb_resume_test_request", restore)
    assert not await test_tasks.route_test_request(
        1, "s", request(text="由客户打进来"), {"jobs": [prior]}, None, AsyncMock()
    )
    restore.assert_not_called()


def test_creation_decision_survives_repair_and_is_not_invented_for_existing_edits():
    from api.services.agent_workbench.tools import WorkbenchTools

    original = request("build")
    original["output"]["creation_direction"] = report()
    context = {"target": {"workflow_json": {"nodes": []}}, "target_fingerprint": "f"}
    repair = WorkbenchTools(1, "s", original, context, tuning=True)
    assert repair.creation_direction == original["output"]["creation_direction"]
    ordinary_edit = WorkbenchTools(1, "s", request("build"), context)
    assert ordinary_edit.creation_direction is None
