"""Real DB handoffs, stale decisions, fenced mutations and crash recovery."""

from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import WorkbenchDecision
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError
from api.services.agent_workbench.orchestration import ApprovalRequired, Orchestration
from api.services.agent_workbench.tools import WorkbenchTools
from api.tests import test_agent_workbench_db as fixtures

db_session = fixtures.db_session
wb = fixtures.wb

BRIEF = {
    "goal": "客户来电预约",
    "direction": "inbound",
    "information_to_collect": ["时间"],
    "business_rules": ["只收集预约，不承诺成功"],
    "success_criteria": ["收集方便的时间"],
    "assumptions": [],
}
CREATE = {
    "name": "Calculate",
    "category": "calculator",
    "definition": {"type": "calculator", "config": {}},
}


async def start(db, fixture, key="build"):
    org, _, sid = fixture
    job = await db.wb_enqueue(
        org, sid, "build", {"mode": "build", "text": "构建一个客户呼入预约助手"}, key
    )
    job = await db.wb_claim(org, sid, job["id"])
    tools = WorkbenchTools(org, sid, job, await db.wb_context(org, sid))
    tools.orchestration = Orchestration(tools)
    await tools.orchestration.load()
    return tools


async def pause(tools, kind="requirements", proposal=None):
    with pytest.raises(ApprovalRequired) as exc:
        await tools.orchestration.request(kind, proposal or {"brief": BRIEF})
    from api.db import db_client

    a = exc.value.approval
    await db_client.wb_finish(
        *tools.orchestration.scope, "awaiting_input", output={"pending_approval": a}
    )
    return a


def decision(a, action="approve", feedback=""):
    return WorkbenchDecision(
        approval_id=a["id"], revision=a["revision"], decision=action, feedback=feedback
    )


async def reclaim(db, tools):
    tools.job = await db.wb_claim(tools.org, tools.session_id, tools.job["id"])
    tools.orchestration = Orchestration(tools)
    await tools.orchestration.load()


async def test_decision_is_scoped_revision_bound_and_idempotent(db_session, wb):
    tools = await start(db_session, wb)
    a = await pause(tools)
    org, user, sid = wb
    jid = tools.job["id"]
    with pytest.raises(WorkbenchError):
        await db_session.wb_decide(org + 1000, sid, jid, user, decision(a))
    with pytest.raises(WorkbenchError):
        await db_session.wb_decide(
            org, sid, jid, user, decision({**a, "revision": "0" * 64})
        )
    result = await db_session.wb_decide(org, sid, jid, user, decision(a))
    assert result["status"] == "queued"
    assert await db_session.wb_decide(org, sid, jid, user, decision(a)) == result
    with pytest.raises(WorkbenchError):
        await db_session.wb_decide(
            org, sid, jid, user, decision(a, "revise", "改成呼出")
        )
    saved = await db_session.wb_get(org, sid, "jobs", jid)
    assert "_orchestration" not in saved["output"]
    assert (
        "_orchestration"
        not in (await db_session.wb_context(org, sid))["jobs"][0]["output"]
    )


async def test_approval_rejects_changed_draft(db_session, wb, async_session):
    from sqlalchemy import update

    from api.db.models import WorkflowModel

    tools = await start(db_session, wb)
    a = await pause(tools)
    await async_session.execute(
        update(WorkflowModel)
        .where(WorkflowModel.id == tools.context["session"]["workflow_id"])
        .values(name="Changed elsewhere")
    )
    with pytest.raises(WorkbenchError, match="changed"):
        await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))


async def test_rejected_requirements_preserve_original_and_record_human_feedback(
    db_session, wb
):
    from api.services.agent_workbench.basis import ensure_basis

    tools = await start(db_session, wb)
    basis = await ensure_basis(wb[0], wb[2], tools.job, tools.base)
    a = await pause(tools)
    await db_session.wb_decide(
        wb[0],
        wb[2],
        tools.job["id"],
        wb[1],
        decision(a, "revise", "改为客户主动呼入，不要外呼"),
    )
    revised = await db_session.wb_task_basis(wb[0], wb[2], tools.job["id"])
    assert revised["parent_fingerprint"] == basis["fingerprint"]
    assert revised["sources"][:-1] == basis["sources"]
    assert revised["sources"][-1]["text"] == "改为客户主动呼入，不要外呼"


async def test_requirements_pause_before_build_and_reuse_approved_brief(
    db_session, wb, monkeypatch
):
    from api.services.agent_workbench import test_tasks
    from api.services.agent_workbench.basis import ensure_basis

    tools = await start(db_session, wb)
    basis = await ensure_basis(wb[0], wb[2], tools.job, tools.base)
    prepare = AsyncMock(return_value=BRIEF)
    monkeypatch.setattr(test_tasks, "decision", prepare)
    with pytest.raises(ApprovalRequired) as exc:
        await tools.orchestration.requirements(
            basis, {"direction": "inbound"}, None, None, "chat"
        )
    assert not tools.candidate_ids
    a = exc.value.approval
    await db_session.wb_finish(*tools.orchestration.scope, "awaiting_input")
    await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))
    await reclaim(db_session, tools)
    assert (
        await tools.orchestration.requirements(
            basis, {"direction": "inbound"}, None, None, "chat"
        )
        == BRIEF
    )
    assert prepare.await_count == 1


async def test_tool_creation_requires_approval_and_replays_committed_mutation(
    db_session, wb, monkeypatch
):
    from api.services import tool_management

    monkeypatch.setattr(tool_management, "capture_tenant_event", AsyncMock())
    tools = await start(db_session, wb)
    with pytest.raises(ApprovalRequired) as exc:
        await tools.propose_tool_change("create", CREATE, "需要计算报价")
    assert await db_session.get_tools_for_organization(wb[0]) == []
    a = exc.value.approval
    await db_session.wb_finish(*tools.orchestration.scope, "awaiting_input")
    await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))
    await reclaim(db_session, tools)
    # Crash after the shared tool commits, before its local receipt saves.
    real_save = tools.orchestration.save
    tools.orchestration.save = AsyncMock(side_effect=RuntimeError("crash"))
    with pytest.raises(RuntimeError, match="crash"):
        await tools.orchestration.apply_approved_tools()
    assert len(await db_session.get_tools_for_organization(wb[0])) == 1
    tools.orchestration.save = real_save
    await tools.orchestration.apply_approved_tools()
    await tools.orchestration.apply_approved_tools()
    assert len(await db_session.get_tools_for_organization(wb[0])) == 1
    assert tools.orchestration.data["steps"]["approved-tool:" + a["id"]]["saved"]


async def test_denied_tool_never_mutates_and_cancel_fences_approved_tool(
    db_session, wb
):
    tools = await start(db_session, wb)
    proposal = {"operation": "create", "request": CREATE, "reason": "calculate"}
    a = await pause(tools, "tool_change", proposal)
    await db_session.wb_decide(
        wb[0], wb[2], tools.job["id"], wb[1], decision(a, "deny")
    )
    await reclaim(db_session, tools)
    await tools.orchestration.apply_approved_tools()
    assert await db_session.get_tools_for_organization(wb[0]) == []
    a = await pause(tools, "tool_change", {**proposal, "reason": "revised"})
    await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))
    await reclaim(db_session, tools)
    await db_session.wb_cancel(wb[0], wb[2], tools.job["id"])
    with pytest.raises(LeaseLost):
        await tools.orchestration.apply_approved_tools()
    assert await db_session.get_tools_for_organization(wb[0]) == []


async def test_update_checks_shared_tool_revision_at_commit(
    db_session, wb, monkeypatch
):
    from api.services import tool_management

    monkeypatch.setattr(tool_management, "capture_tenant_event", AsyncMock())
    existing = await db_session.create_tool(
        wb[0], wb[1], "Original", CREATE["definition"], category="calculator"
    )
    tools = await start(db_session, wb)
    with pytest.raises(ApprovalRequired) as exc:
        await tools.propose_tool_change(
            "update", {"name": "Proposed"}, "rename", existing.tool_uuid
        )
    a = exc.value.approval
    await db_session.wb_finish(*tools.orchestration.scope, "awaiting_input")
    await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))
    await reclaim(db_session, tools)
    await db_session.update_tool(existing.tool_uuid, wb[0], name="Changed by colleague")
    await tools.orchestration.apply_approved_tools()
    assert (
        tools.orchestration.data["steps"]["approved-tool:" + a["id"]]["error_code"]
        == "stale_tool"
    )
    assert (
        await db_session.get_tool_by_uuid(existing.tool_uuid, wb[0])
    ).name == "Changed by colleague"


async def test_artifact_save_receipts_are_atomic_and_lease_fenced(db_session, wb):
    tools = await start(db_session, wb)
    scope = tools.orchestration.scope
    first = await db_session.wb_save_candidate(
        *scope,
        tools.base,
        "code",
        "candidate",
        tools.base_fingerprint,
        operation_key="candidate-step",
    )
    second = await db_session.wb_save_candidate(
        *scope,
        tools.base,
        "code",
        "candidate",
        tools.base_fingerprint,
        operation_key="candidate-step",
    )
    assert first["id"] == second["id"]
    suite = {"name": "regression", "cases": []}
    first = await db_session.wb_save_suite(
        wb[0], wb[2], suite, job_id=scope[2], token=scope[3], operation_key="suite-step"
    )
    second = await db_session.wb_save_suite(
        wb[0], wb[2], suite, job_id=scope[2], token=scope[3], operation_key="suite-step"
    )
    assert first["id"] == second["id"]
    await db_session.wb_cancel(wb[0], wb[2], tools.job["id"])
    with pytest.raises(LeaseLost):
        await db_session.wb_checkpoint(*scope, "late", {})


async def test_worker_loss_restores_pending_handoff_without_running_it(db_session, wb):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from api.db.models import WorkbenchJobModel

    tools = await start(db_session, wb)
    with pytest.raises(ApprovalRequired):
        await tools.orchestration.request("requirements", {"brief": BRIEF})
    async with db_session.async_session() as db, db.begin():
        await db.execute(
            update(WorkbenchJobModel)
            .where(WorkbenchJobModel.id == tools.job["id"])
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=5))
        )
    assert await db_session.wb_pending() == []
    job = await db_session.wb_get(wb[0], wb[2], "jobs", tools.job["id"])
    assert job["status"] == "awaiting_input"
    assert job["output"]["pending_approval"]["status"] == "pending"


async def test_checkpoint_step_recovery_skips_completed_work(db_session, wb):
    tools = await start(db_session, wb)
    work = AsyncMock(return_value={"result": "preserved"})
    assert await tools.orchestration.step("expensive-step", work) == {
        "result": "preserved"
    }
    await db_session.wb_finish(*tools.orchestration.scope, "failed")
    await db_session.wb_resume_orchestration(wb[0], wb[2], tools.job["id"])
    await reclaim(db_session, tools)
    assert await tools.orchestration.step("expensive-step", work) == {
        "result": "preserved"
    }
    assert work.await_count == 1


async def test_authoring_and_repair_keep_independent_editing_checkpoints(
    db_session, wb
):
    tools = await start(db_session, wb)
    tools.base_code = "original working code"
    await tools.orchestration.save("editing", None)
    repair_tools = WorkbenchTools(wb[0], wb[2], tools.job, tools.context)
    repair = Orchestration(repair_tools, "repair-round-1")
    await repair.load()
    assert repair_tools.base_code is None
    repair_tools.base_code = "repair working code"
    await repair.save("editing", None)
    restored = WorkbenchTools(wb[0], wb[2], tools.job, tools.context)
    await Orchestration(restored).load()
    assert restored.base_code == "original working code"


async def test_build_entry_requires_brief_then_continues_same_request(
    db_session, wb, monkeypatch
):
    from types import SimpleNamespace

    from api.services.agent_workbench import basis, creation, jobs, test_tasks

    tools = await start(db_session, wb)
    monkeypatch.setattr(basis, "discover_originals", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        creation,
        "resolve_direction",
        AsyncMock(
            return_value={
                "intent": "build",
                "status": "resolved",
                "direction": "inbound",
                "citations": [],
                "question": "",
            }
        ),
    )
    monkeypatch.setattr(test_tasks, "decision", AsyncMock(return_value=BRIEF))
    build = AsyncMock()
    monkeypatch.setattr(jobs.CodexClient, "run", build)
    with pytest.raises(ApprovalRequired) as exc:
        await jobs._build(
            wb[0],
            wb[2],
            tools.job,
            tools.context,
            SimpleNamespace(llm=None),
            tools.job["input"]["text"],
        )
    build.assert_not_called()
    a = exc.value.approval
    await db_session.wb_finish(*tools.orchestration.scope, "awaiting_input")
    await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))
    await reclaim(db_session, tools)
    monkeypatch.setattr(WorkbenchTools, "get_context", AsyncMock(return_value={}))

    async def complete(client, prompt, *_args, **_kwargs):
        assert "Human-approved requirements" in prompt
        assert BRIEF["goal"] in prompt
        client.dispatcher.candidate_ids.append("candidate")
        client.dispatcher.suite_ids.append("suite")
        return {}

    monkeypatch.setattr(jobs.CodexClient, "run", complete)
    result = await jobs._build(
        wb[0],
        wb[2],
        tools.job,
        tools.context,
        SimpleNamespace(llm=None),
        tools.job["input"]["text"],
    )
    assert result["outcome"] == "build"
    assert result["candidate_id"] == "candidate" and result["suite_id"] == "suite"
    assert test_tasks.decision.await_count == 1


async def test_existing_threads_can_propose_tools_through_stable_reader(db_session, wb):
    tools = await start(db_session, wb)
    tools.preloaded_context = {}
    assert "get_context" not in {s["name"] for s in tools.specs()}
    with pytest.raises(ApprovalRequired):
        await tools.call(
            "read_node",
            {
                "organization_action": "propose_tool_change",
                "operation": "create",
                "request": CREATE,
                "reason": "需要计算",
            },
        )
    assert await db_session.get_tools_for_organization(wb[0]) == []


async def test_analysis_cannot_propose_account_mutations(db_session, wb):
    from jsonschema import ValidationError

    tools = await start(db_session, wb)
    tools.mode = "analyze"
    with pytest.raises(ValidationError):
        await tools.call(
            "read_node",
            {
                "organization_action": "propose_tool_change",
                "operation": "create",
                "request": CREATE,
                "reason": "不应执行",
            },
        )
    assert not (await db_session.wb_orchestration(wb[0], wb[2], tools.job["id"])).get(
        "approvals"
    )


async def test_confirmed_brief_is_human_evidence_in_later_candidate_ancestry(
    db_session, wb
):
    from api.services.agent_workbench.basis import ensure_basis

    tools = await start(db_session, wb)
    original = await ensure_basis(wb[0], wb[2], tools.job, tools.base)
    a = await pause(tools)
    await db_session.wb_decide(wb[0], wb[2], tools.job["id"], wb[1], decision(a))
    current = await db_session.wb_task_basis(wb[0], wb[2], tools.job["id"])
    assert current["parent_fingerprint"] == original["fingerprint"]
    assert current["sources"][-1]["kind"] == "user_request"
    assert BRIEF["business_rules"][0] in current["sources"][-1]["text"]
    await reclaim(db_session, tools)
    candidate = await db_session.wb_save_candidate(
        *tools.orchestration.scope, tools.base, "code", "saved", tools.base_fingerprint
    )
    await db_session.wb_finish(*tools.orchestration.scope, "completed")
    later = await db_session.wb_enqueue(
        wb[0], wb[2], "build", {"text": "补充一个步骤"}, "later"
    )
    sources = await db_session.wb_basis_origins(
        wb[0], wb[2], later["id"], candidate["fingerprint"]
    )
    assert any(
        s["id"] == "decision:" + a["id"] and BRIEF["business_rules"][0] in s["text"]
        for s in sources
    )
