"""Source binding, complete preparation and resumption without live Agent calls."""

# ruff: noqa: F811 -- imported pytest fixtures
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import basis, test_sources, test_tasks
from api.services.agent_workbench import test_compilation as compiler
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.tests.test_agent_workbench_db import db_session, wb  # noqa: F401
from api.tests.test_workbench_test_compilation import fixture


def reseal(case):
    c = case["compiled"]
    c["fingerprint"] = fingerprint({k: v for k, v in c.items() if k != "fingerprint"})


async def test_frozen_contract_survives_missing_search_hit_without_rewriting_assertions():
    case = fixture()
    original = deepcopy(case)
    unrelated = {
        "sources": [
            {
                "id": "new-request",
                "kind": "current_request",
                "title": "Continue",
                "text": "Continue preparing",
                "content_hash": "new",
            }
        ]
    }
    result, summary = await compiler.compile_cases(
        [case],
        {"snapshot": {}},
        unrelated,
        AsyncMock(side_effect=AssertionError("No regeneration")),
        concurrency=1,
        progress=AsyncMock(),
    )
    assert summary["ready"] == 1
    assert result == [original]


@pytest.mark.parametrize(
    "change",
    ["missing_frozen_source", "changed_source", "reference", "tampered_fingerprint"],
)
def test_continuation_keeps_strict_evidence_validation(change):
    case = fixture()
    source = deepcopy(case["compiled"]["sources"][0])
    records = {source["id"]: source}
    if change == "missing_frozen_source":
        case["compiled"]["sources"] = []
        reseal(case)
    elif change == "changed_source":
        source["text"] += "Different policy"
    elif change == "reference":
        source["source_kind"] = "reference"
    else:
        case["compiled"]["requirements"][0]["expected_behavior"] = "easier rule"
    with pytest.raises(WorkbenchError):
        compiler.validate_compiled(case, records=records)


def test_format_repair_cannot_invent_reference_or_customer_speech():
    selection = {"kind": "inline", "inline_quotes": ["Customer: I decline."]}
    suite = {
        "cases": [
            {
                "user_messages": ["I decline."],
                "reference_turns": [{"role": "assistant", "text": "Invented answer"}],
            }
        ]
    }
    with pytest.raises(WorkbenchError, match="不能补造"):
        test_sources.validate_generated_sources(suite, selection, [])
    suite["cases"][0]["reference_turns"] = []
    test_sources.validate_generated_sources(suite, selection, [])
    suite["cases"][0]["user_messages"] = ["Prepare tests from the uploaded examples"]
    with pytest.raises(WorkbenchError, match="未逐字"):
        test_sources.validate_generated_sources(suite, selection, [])


async def test_missing_requested_data_never_falls_through_to_fabrication(monkeypatch):
    job = {
        "id": "j",
        "lease_token": "l",
        "input": {"text": "Convert the supplied examples"},
        "output": {},
    }
    monkeypatch.setattr(
        test_sources.db_client,
        "wb_test_source_context",
        AsyncMock(return_value={"assets": [], "source_history": []}),
    )
    monkeypatch.setattr(
        test_sources.db_client, "wb_memory", AsyncMock(return_value={"entries": []})
    )
    save = AsyncMock()
    monkeypatch.setattr(test_sources.db_client, "wb_set_test_sources", save)
    monkeypatch.setattr(
        test_tasks,
        "decision",
        AsyncMock(
            return_value={
                "kind": "missing",
                "asset_ids": [],
                "inline_quotes": [],
                "question": "Which examples?",
            }
        ),
    )
    result = await test_sources.resolve_sources(
        1, "s", job, None, AsyncMock(), "responses"
    )
    assert result["kind"] == "missing"
    save.assert_not_awaited()


async def test_instruction_quote_is_not_a_customer_case_even_when_literal(monkeypatch):
    text = "Convert the attached examples into tests"
    job = {"id": "j", "input": {"text": text}, "output": {}}
    monkeypatch.setattr(
        test_sources.db_client,
        "wb_test_source_context",
        AsyncMock(return_value={"assets": [], "source_history": []}),
    )
    monkeypatch.setattr(
        test_sources.db_client, "wb_memory", AsyncMock(return_value={"entries": []})
    )
    save = AsyncMock()
    monkeypatch.setattr(test_sources.db_client, "wb_set_test_sources", save)
    monkeypatch.setattr(
        test_tasks,
        "decision",
        AsyncMock(
            side_effect=[
                {
                    "kind": "inline",
                    "asset_ids": [],
                    "inline_quotes": [text],
                    "question": "",
                },
                {
                    "supported": False,
                    "reason": "This is an instruction, not sample dialogue",
                },
            ]
        ),
    )
    with pytest.raises(WorkbenchError, match="输入来源校验"):
        await test_sources.resolve_sources(1, "s", job, None, AsyncMock(), "responses")
    save.assert_not_awaited()


async def test_sources_persist_with_lease_and_tenant_boundaries(
    db_session, wb, monkeypatch
):
    org, _user, sid = wb
    monkeypatch.setattr(test_sources, "db_client", db_session)
    asset = await db_session.wb_asset(
        org,
        sid,
        name="cases.csv",
        content_type="text/csv",
        byte_size=2,
        sha256="content",
        object_key="local",
        status="ready",
        parsed={},
    )
    job = await db_session.wb_enqueue(
        org, sid, "build", {"text": "Prepare supplied examples", "mode": "test"}, "new"
    )
    job = await db_session.wb_claim(org, sid, job["id"])
    monkeypatch.setattr(
        test_tasks,
        "decision",
        AsyncMock(
            return_value={
                "kind": "attachments",
                "asset_ids": [asset["id"]],
                "inline_quotes": [],
                "question": "",
            }
        ),
    )
    selection = await test_sources.resolve_sources(
        org, sid, job, None, AsyncMock(), "responses"
    )
    assert job["input"]["asset_ids"] == [asset["id"]]
    assert (await db_session.wb_get(org, sid, "jobs", job["id"]))["input"][
        "asset_ids"
    ] == [asset["id"]]
    test_tasks.decision.reset_mock()
    assert (
        await test_sources.resolve_sources(
            org, sid, job, None, AsyncMock(), "responses"
        )
        == selection
    )
    test_tasks.decision.assert_not_awaited()
    with pytest.raises(WorkbenchError):
        await db_session.wb_test_source_context(org + 999, sid, job["id"])
    with pytest.raises(WorkbenchError):
        await db_session.wb_set_test_sources(
            org, sid, job["id"], "wrong-lease", selection
        )
    with pytest.raises(WorkbenchError, match="已固定"):
        await db_session.wb_set_test_sources(
            org,
            sid,
            job["id"],
            job["lease_token"],
            {**selection, "inline_quotes": ["changed"]},
        )


async def test_complete_upload_prepare_checkpoint_and_new_task_resume(
    db_session, wb, monkeypatch, tmp_path
):
    import csv
    import hashlib

    import api.db
    from api.services.agent_workbench import dependencies, jobs, test_operations
    from api.services.agent_workbench.fixtures import FixtureCodec

    org, _user, sid = wb
    for module in (api.db, basis, test_sources, test_tasks, test_operations, jobs):
        monkeypatch.setattr(module, "db_client", db_session)
    seed = fixture()
    csvfile = tmp_path / "cases.csv"
    with csvfile.open("w") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "before", "revised"])
        for name in ("first", "second"):
            writer.writerow([name, "Customer: discarded", seed["reference_dialogue"]])
        writer.writerow(["invalid", "", "no identifiable speaker"])
    raw = csvfile.read_bytes()
    asset = await db_session.wb_asset(
        org,
        sid,
        name="cases.csv",
        content_type="text/csv",
        byte_size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        object_key="local",
        status="ready",
        parsed={},
    )

    async def download(_self, key, path):
        assert key == "local"
        path.write_bytes(raw)

    monkeypatch.setattr(test_tasks.PrivateAssetStore, "download", download)
    config = SimpleNamespace(
        llm=SimpleNamespace(provider="openai", api_key="local-only")
    )
    runtime = SimpleNamespace(effective=config, snapshot={"version": 3, "services": {}})
    for module in (jobs, test_tasks):
        monkeypatch.setattr(module, "prepare_models", AsyncMock(return_value=runtime))
    monkeypatch.setattr(dependencies, "replay_models", AsyncMock(return_value=runtime))
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    no_execute = AsyncMock(side_effect=AssertionError("Simulation is paused"))
    monkeypatch.setattr(test_tasks, "evaluate", no_execute)
    monkeypatch.setattr(
        jobs, "_build", AsyncMock(side_effect=AssertionError("Agent editing forbidden"))
    )
    source = {
        "id": "original-business",
        "kind": "material",
        "title": "Policy",
        "text": seed["compiled"]["sources"][0]["text"],
        "content_hash": "original",
    }
    basis_calls = 0

    async def original_basis(*args, **kwargs):
        nonlocal basis_calls
        basis_calls += 1
        # The resumed search no longer returns the original authority. Its
        # immutable snapshot in the checkpoint must still be validated and used.
        return {"sources": [source] if basis_calls == 1 else []}

    monkeypatch.setattr(basis, "ensure_basis", original_basis)
    first_id = None
    calls = []
    interrupt_second = True

    async def decide(name, rules, schema, evidence, *_args, **kwargs):
        calls.append((name, deepcopy(evidence)))
        if name == "classify_request":
            value = {"intent": "test", "reason": "prepare data"}
            if "source_job_id" in schema["properties"]:
                value["source_job_id"] = first_id
        elif name == "classify_test_operation":
            value = {
                "operation": "continue" if first_id else "prepare",
                "reason": "preparation only",
            }
        elif name == "resolve_test_sources":
            assert evidence["selected_asset_ids"] == []
            assert evidence["assets"][0]["id"] == asset["id"]
            value = {
                "kind": "attachments",
                "asset_ids": [asset["id"]],
                "inline_quotes": [],
                "question": "",
            }
        elif name == "select_test_method":
            assert evidence["source_selection"]["assets"][0]["id"] == asset["id"]
            value = {"mode": "dynamic", "reason": "customer evidence, not fixed replay"}
        elif name == "select_test_source":
            value = {
                "needs_input": "",
                "tables": [
                    {
                        "asset_id": asset["id"],
                        "sheet": evidence["tables"][0]["sheet"],
                        "dialogue_column": "revised",
                        "id_column": "id",
                        "user_labels": ["客户"],
                        "assistant_labels": ["坐席"],
                    }
                ],
            }
        elif "business_rubric" in name:
            r = {
                k: v
                for k, v in seed["compiled"]["requirements"][0].items()
                if k != "id"
            }
            r["citations"] = [
                {"span_id": evidence["records"][0]["citation_spans"][0]["span_id"]}
            ]
            value = {"requirements": [r], "questions": []}
        elif "test_environment" in name:
            value = {"capabilities": []}
        elif name == "compile_customer_fixture":
            value = FixtureCodec(seed["reference_turns"]).encode(
                seed["compiled"]["scenario"]
            )
        elif name == "verify_customer_fixture":
            if (
                interrupt_second
                and "second" in evidence["preparation_scope"]["case_name"]
            ):
                raise WorkbenchError("Interrupted after the customer draft was saved")
            value = {
                "verdict": "supported",
                "reason": "original source checked",
                "corrections": dict.fromkeys(compiler.PLAN["properties"]),
            }
        else:
            raise AssertionError(name)
        if kwargs.get("validate"):
            kwargs["validate"](value)
        return value

    monkeypatch.setattr(test_tasks, "decision", decide)
    initial_target = (await db_session.wb_context(org, sid))["target_fingerprint"]
    job = await db_session.wb_enqueue(
        org,
        sid,
        "build",
        {
            "text": "Convert uploaded revised dialogues; customer replies are evidence, operator replies are reference. Prepare only.",
            "asset_ids": [],
            "mode": "auto",
        },
        "prepare",
    )
    job = await db_session.wb_claim(org, sid, job["id"])
    first = await jobs._perform(org, sid, job)
    assert first["outcome"] == "test_preparation"
    assert first["compilation"]["total"] == 3
    assert first["compilation"]["ready"] == 1
    assert first["compilation"]["preparation_error"] == 2
    assert first["compilation"]["import_error"] == 1
    await db_session.wb_finish(
        org, sid, job["id"], job["lease_token"], "needs_attention", output=first
    )
    prior = await db_session.wb_get(org, sid, "jobs", job["id"])
    checkpoints = {
        r["name"]: await db_session.wb_get(org, sid, "suites", r["suite_id"])
        for r in prior["output"]["preparation_receipts"]
    }
    old_cases = {name: s["payload"]["cases"][0] for name, s in checkpoints.items()}
    ready = next(c for c in old_cases.values() if c["compiled"]["status"] == "ready")
    pending = next(
        c for c in old_cases.values() if c["compiled"]["status"] == "preparation_error"
    )
    assert pending["compiled"]["preparation_checkpoint"]["stage"] == "verification"
    first_id = job["id"]
    interrupt_second = False
    calls.clear()
    resumed = await db_session.wb_enqueue(
        org, sid, "build", {"text": "Continue preparing", "mode": "auto"}, "resume"
    )
    resumed = await db_session.wb_claim(org, sid, resumed["id"])
    final = await jobs._perform(org, sid, resumed)
    assert final["compilation"]["total"] == 3
    assert final["compilation"]["ready"] == 2
    assert (
        final["compilation"]["preparation_error"]
        == final["compilation"]["import_error"]
        == 1
    )
    names = [name for name, _ in calls]
    assert "select_test_source" not in names
    assert "compile_customer_fixture" not in names
    assert "compile_business_rubric" not in names
    assert names.count("verify_customer_fixture") == 1
    suites = [
        await db_session.wb_get(org, sid, "suites", s) for s in final["suite_ids"]
    ]
    cases = [c for s in suites for c in s["payload"]["cases"]]
    assert next(c for c in cases if c["name"] == ready["name"]) == ready
    for c in cases:
        assert c["compiled"]["requirements"] == ready["compiled"]["requirements"]
        assert c["compiled"]["sources"] == ready["compiled"]["sources"]
        compiler.validate_compiled(c)
    assert await db_session.wb_get(org, sid, "jobs", first_id) == prior
    assert (await db_session.wb_context(org, sid))[
        "target_fingerprint"
    ] == initial_target
    no_execute.assert_not_awaited()


async def test_same_file_history_crosses_sessions_but_not_agents_or_tenants(
    db_session, wb
):
    org, user, sid = wb
    current = await db_session.wb_context(org, sid)
    prior = await db_session.wb_enqueue(
        org,
        sid,
        "build",
        {
            "text": "Use revision_b; derive facts from customer utterances.",
            "mode": "test",
        },
        "history",
    )
    prior = await db_session.wb_claim(org, sid, prior["id"])
    manifest = [
        {
            "sha256": "same-bytes",
            "dialogue_column": "revision_b",
            "user_labels": ["Customer"],
            "assistant_labels": ["Operator"],
            "rows": 5,
        }
    ]
    await db_session.wb_finish(
        org,
        sid,
        prior["id"],
        prior["lease_token"],
        "completed",
        output={"source_manifest": manifest},
    )
    second = await db_session.wb_create_session(
        org, user, workflow_id=current["session"]["workflow_id"], new_session=True
    )
    for session_id in (
        second["id"],
        (await db_session.wb_create_session(org, user, name="Unrelated"))["id"],
    ):
        await db_session.wb_asset(
            org,
            session_id,
            name="renamed.csv",
            content_type="text/csv",
            byte_size=4,
            sha256="same-bytes",
            object_key="local",
            status="ready",
            parsed={},
        )
        job = await db_session.wb_enqueue(
            org, session_id, "build", {"text": "Prepare these examples"}, "new"
        )
        context = await db_session.wb_test_source_context(org, session_id, job["id"])
        assert [r["request"] for r in context["source_history"]] == (
            [prior["input"]["text"]] if session_id == second["id"] else []
        )
    with pytest.raises(WorkbenchError):
        await db_session.wb_test_source_context(org + 999, second["id"], job["id"])
