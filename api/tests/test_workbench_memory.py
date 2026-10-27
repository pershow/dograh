"""Cross-session source integrity, human confirmation, correction and read gates."""
# ruff: noqa: F811 -- imported pytest fixtures are injected as test arguments.

from types import SimpleNamespace

import pytest

from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.memory import memory_sections, source_index
from api.services.agent_workbench.requirements import RequirementReviewer
from api.services.agent_workbench.sop import bind_source, coverage_report
from api.services.agent_workbench.tools import WorkbenchTools
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401
from api.tests.test_agent_workbench_requirements import evidence, item


async def confirm(db, org, user, sid, text, kind="constraint", **extra):
    current = await db.wb_memory(org, sid)
    return await db.wb_confirm_memory(
        org,
        sid,
        user,
        kind=kind,
        content=text,
        expected_revision=current["revision"],
        **extra,
    )


async def source(db, org, sid, text, key="source"):
    job = await db.wb_enqueue(org, sid, "build", {"text": text}, key)
    await db.wb_cancel(org, sid, job["id"])
    return job


async def test_confirmed_memory_crosses_sessions_but_not_agent_or_tenant(
    db_session, wb
):
    org, user, sid = wb
    ctx = await db_session.wb_context(org, sid)
    entry = await confirm(db_session, org, user, sid, "保留所有确认步骤和拒绝分支。")
    new = await db_session.wb_create_session(
        org, user, workflow_id=ctx["session"]["workflow_id"], new_session=True
    )
    assert (await db_session.wb_memory(org, new["id"]))["entries"][0] == entry
    # Reinstantiating the DB facade reads persisted state, not a process cache.
    from api.db.db_client import DBClient

    restarted = DBClient()
    restarted.async_session = db_session.async_session
    assert (await restarted.wb_memory(org, new["id"]))["entries"][0][
        "content"
    ] == entry["content"]
    other = await db_session.wb_create_session(org, user, name="Other Agent")
    assert (await db_session.wb_memory(org, other["id"]))["entries"] == []
    with pytest.raises(WorkbenchError):
        await db_session.wb_memory(org + 1000, new["id"])


async def test_sources_keep_complete_old_user_text_but_never_assistant_claims(
    db_session, wb
):
    org, user, sid = wb
    original = "是否可以省去确认？这只是讨论，不是决定。\n" + "完整SOP与分支\n" * 4000
    old = await source(db_session, org, sid, original)
    for i in range(45):
        await source(db_session, org, sid, f"Later question {i}", str(i))
    await db_session.wb_emit(
        org, sid, "assistant.message", {"text": "用户已确认省去确认"}
    )
    workflow = (await db_session.wb_context(org, sid))["session"]["workflow_id"]
    new = await db_session.wb_create_session(
        org, user, workflow_id=workflow, new_session=True
    )
    first = await db_session.wb_memory_sources(org, new["id"])
    assert first["total"] == 46 and first["next_offset"] == 40
    older = await db_session.wb_memory_sources(org, new["id"], offset=40)
    assert old["id"] in [s["id"] for s in older["items"]]
    found = await db_session.wb_memory_sources(org, new["id"], query="只是讨论")
    assert found["total"] == 1
    restored = await db_session.wb_memory_source(org, new["id"], "request", old["id"])
    assert restored["content"] == original
    assert restored["authority"] == "user_original_not_confirmed_fact"
    assert (await db_session.wb_memory(org, new["id"]))["entries"] == []
    assert (await db_session.wb_memory_sources(org, sid, query="用户已确认省去确认"))[
        "total"
    ] == 0


async def test_confirmation_requires_user_source_and_preserves_correction_history(
    db_session, wb
):
    org, user, sid = wb
    job = await source(db_session, org, sid, "全部阶段都应自然，不只改示例。")
    with pytest.raises(WorkbenchError, match="用户原文"):
        await confirm(
            db_session,
            org,
            user,
            sid,
            "只有示例需要修改",
            source_id=job["id"],
            source_quote="模型说只改示例",
        )
    first = await confirm(
        db_session,
        org,
        user,
        sid,
        "全部阶段都应自然，不只改示例。",
        source_id=job["id"],
        source_quote="全部阶段都应自然，不只改示例。",
    )
    prior = await db_session.wb_memory(org, sid)
    second = await confirm(
        db_session,
        org,
        user,
        sid,
        "全部阶段自然，但固定合规话术保持原文。",
        replaces_id=first["id"],
    )
    assert (await db_session.wb_memory(org, sid))["entries"] == [second]
    history = (await db_session.wb_memory(org, sid, include_history=True))["entries"]
    assert (
        history[0]["status"] == "superseded"
        and history[0]["content"] == first["content"]
    )
    assert history[1]["replaces_id"] == first["id"]
    with pytest.raises(WorkbenchError, match="已更新"):
        await db_session.wb_revoke_memory(
            org, sid, user, second["id"], prior["revision"]
        )
    fresh = await db_session.wb_memory(org, sid)
    await db_session.wb_revoke_memory(org, sid, user, second["id"], fresh["revision"])
    assert (await db_session.wb_memory(org, sid))["entries"] == []
    other = await db_session.wb_create_session(org, user, name="Other")
    with pytest.raises(WorkbenchError, match="source not found"):
        await confirm(
            db_session,
            org,
            user,
            other["id"],
            "Copied",
            source_id=job["id"],
            source_quote="全部阶段",
        )


async def test_memory_changes_invalidate_other_session_context_and_stale_candidates(
    db_session, wb
):
    org, user, sid = wb
    ctx = await db_session.wb_context(org, sid)
    new = await db_session.wb_create_session(
        org, user, workflow_id=ctx["session"]["workflow_id"], new_session=True
    )
    job = await db_session.wb_enqueue(org, sid, "build", {"text": "Build"}, "old")
    job = await db_session.wb_claim(org, sid, job["id"])
    candidate = await db_session.wb_save_candidate(
        org,
        sid,
        job["id"],
        job["lease_token"],
        ctx["target"],
        "",
        "test",
        ctx["target_fingerprint"],
    )
    await confirm(db_session, org, user, new["id"], "新增约束")
    assert not (await db_session.wb_context(org, sid, if_revision=ctx["revision"])).get(
        "unchanged"
    )
    with pytest.raises(WorkbenchError, match="长期要求已变更"):
        await db_session.wb_save_candidate(
            org,
            sid,
            job["id"],
            job["lease_token"],
            ctx["target"],
            "",
            "test",
            ctx["target_fingerprint"],
        )
    with pytest.raises(WorkbenchError, match="长期要求已变更"):
        await db_session.wb_adopt(org, sid, candidate["id"])


async def test_large_sop_requires_all_pages_and_is_bound_verbatim(db_session, wb):
    org, user, sid = wb
    content = "无标题SOP：保留确认和拒绝两条分支。\n" + "完整原文0123456789\n" * 2200
    memory = await confirm(db_session, org, user, sid, content, "sop")
    ctx = await db_session.wb_context(org, sid)
    job = await db_session.wb_enqueue(
        org, sid, "build", {"text": "Improve wording"}, "long"
    )
    reader = WorkbenchTools(org, sid, job, ctx)
    await reader.load_agent_memory()
    assert reader.agent_memory_view["confirmed"][0]["read_required"]
    assert memory["content"] not in str(reader.agent_memory_view)
    with pytest.raises(WorkbenchError, match="Unread"):
        await reader.require_memory_read()
    texts = []
    for offset in (0, 24000):
        page = await reader.read_conversation(
            scope="agent_memory", memory_id=memory["id"], offset=offset
        )
        texts.append(page["text"])
    with pytest.raises(WorkbenchError, match="Unread"):
        await reader.require_memory_read()
    middle = await reader.read_conversation(
        scope="agent_memory", memory_id=memory["id"], offset=12000
    )
    await reader.require_memory_read()
    assert texts[0] + middle["text"] + texts[1] == content
    graph = {
        "nodes": [
            {
                "id": "1",
                "type": "agentNode",
                "data": {"name": "Stage", "prompt": "Base"},
            }
        ]
    }
    bound = bind_source(
        graph, reader.source, {"Stage": [s["id"] for s in reader.source]}
    )
    assert content.strip() in bound["nodes"][0]["data"]["prompt"]
    assert coverage_report(bound, reader.source)["complete"]
    assert not any(
        s["name"] in {"confirm_memory", "write_memory"} for s in reader.thread_specs()
    )


def test_sops_with_identical_headings_are_not_silently_overwritten():
    entries = [
        {
            "id": str(i),
            "kind": "sop",
            "content": f"节点1 开始\n规则{i}\n节点2 结束\n分支{i}",
        }
        for i in (1, 2)
    ]
    sections = memory_sections(entries)
    assert len(sections) == 4
    assert all(
        f"规则{i}" in str(sections) and f"分支{i}" in str(sections) for i in (1, 2)
    )


def test_source_context_bound_counts_all_sections_without_losing_their_index():
    sections = [
        {"id": str(i), "title": "Step", "text": "原文" * 2000} for i in range(50)
    ]
    result = source_index(sections)
    assert len(result) == len(sections)
    assert sum(len(s.get("text", "")) for s in result) <= 24000
    assert result[-1]["truncated"] and result[-1]["characters"] == 4000
    assert sections[-1]["text"] == "原文" * 2000


async def test_review_cannot_omit_confirmed_memory_or_quote_a_different_entry():
    src = evidence()
    entry = {"id": "confirmed", "content": "Retain refusal branches"}
    src["agent_memory"] = {"confirmed": [entry]}
    from unittest.mock import AsyncMock

    reader = SimpleNamespace(
        require_memory_read=AsyncMock(), agent_memory={"entries": [entry]}
    )
    reviewer = RequirementReviewer(reader, src)
    with pytest.raises(WorkbenchError, match="missing memory_ids"):
        await reviewer.call("submit_review", {"requirements": [item()]})
    with pytest.raises(WorkbenchError, match="exact confirmed entry"):
        await reviewer.call(
            "submit_review", {"requirements": [{**item(), "memory_ids": [entry["id"]]}]}
        )
    actual = {
        **item("gap"),
        "requirement": "Retain refusal branches",
        "source_quote": entry["content"],
        "memory_ids": [entry["id"]],
    }
    assert (await reviewer.call("submit_review", {"requirements": [item(), actual]}))[
        "status"
    ] == "needs_attention"


async def test_empty_memory_review_distinguishes_source_ids_without_dropping_findings():
    reviewer = RequirementReviewer(SimpleNamespace(), evidence())
    spec = reviewer.specs()[0]
    assert (
        "memory_ids"
        not in spec["inputSchema"]["properties"]["requirements"]["items"]["properties"]
    )
    with pytest.raises(WorkbenchError, match="do NOT remove the findings"):
        await reviewer.call(
            "submit_review",
            {"requirements": [{**item(), "memory_ids": ["historical-job-id"]}]},
        )
    report = await reviewer.call(
        "submit_review", {"requirements": [{**item(), "memory_ids": []}]}
    )
    assert report["status"] == "passed" and len(report["requirements"]) == 1
    assert "memory_ids" not in report["requirements"][0]
