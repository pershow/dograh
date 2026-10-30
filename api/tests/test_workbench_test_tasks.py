import asyncio
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import Instruction, SimulationCase
from api.services.agent_workbench import test_tasks as tasks
from api.services.agent_workbench.errors import WorkbenchError
from api.services.documents.file_parser import parse_file


async def test_batches_share_case_slots_and_aggregate_latest_snapshots_once():
    later_started = asyncio.Event()
    active, peak = 0, 0
    progress = AsyncMock()

    async def save(payload):
        return {"id": str(payload)}

    async def run(suite, callback, semaphore):
        complete = 0

        async def case(index):
            nonlocal active, peak, complete
            async with semaphore:
                active += 1
                peak = max(peak, active)
                if suite["id"] == "0" and index == 0:
                    await later_started.wait()
                if suite["id"] == "1":
                    later_started.set()
                active -= 1
                complete += 1
                await callback("e" + suite["id"], {"passed": complete}, complete)

        await asyncio.gather(case(0), case(1))
        return {"id": "e" + suite["id"], "summary": {"passed": 2, "executed": 2}}

    initial = {
        "total": 5,
        "passed": 0,
        "failed": 0,
        "error": 0,
        "inconclusive": 1,
        "executed": 0,
    }
    suites, evaluations, counts = await asyncio.wait_for(
        tasks.execute_batches([0, 1], save, run, progress, initial, 2), 2
    )
    assert peak <= 2 and later_started.is_set()
    assert suites == ["0", "1"] and evaluations == ["e0", "e1"]
    assert counts == {**initial, "passed": 4, "executed": 4}
    finished = [c.kwargs["summary"]["executed"] for c in progress.call_args_list]
    assert finished == sorted(finished)


async def test_current_request_only_classification_and_explicit_mode(monkeypatch):
    classify = AsyncMock(return_value={"intent": "test", "reason": "run attachment"})
    monkeypatch.setattr(tasks, "decision", classify)
    payload = {
        "text": "请用附件的测试用例仿真测试，不修改 Agent",
        "mode": "auto",
        "history": ["把所有节点改得更拟人"],
    }
    assert await tasks.test_intent(payload, None, AsyncMock())
    assert classify.call_args.args[3] == {"current_request": payload["text"]}
    classify.reset_mock()
    assert await tasks.test_intent({"mode": "test"}, None, AsyncMock())
    assert not await tasks.test_intent({"mode": "build"}, None, AsyncMock())
    classify.assert_not_called()
    assert Instruction(mode="test", text="执行测试", idempotency_key="t").mode == "test"


async def test_resume_keeps_original_test_request_assets_and_target(monkeypatch):
    from api.services.agent_workbench import jobs
    from api.tasks import arq

    prior = {
        "id": "prior",
        "status": "cancelled",
        "input": {"text": "run adjusted column", "asset_ids": ["original-file"]},
        "output": {
            "intent": "test",
            "candidate_id": "frozen",
            "evaluation_ids": ["evaluation"],
        },
    }

    async def get(org, sid, table, item):
        assert (org, sid) == (1, "s")
        return prior if table == "jobs" else {"snapshot": {}}

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(
        jobs.db_client, "wb_context", AsyncMock(return_value={"target": {}})
    )
    monkeypatch.setattr(jobs, "prepare_models", AsyncMock())
    enqueue = AsyncMock(return_value={"id": "next"})
    monkeypatch.setattr(jobs.db_client, "wb_enqueue", enqueue)
    monkeypatch.setattr(
        arq,
        "get_arq_redis",
        AsyncMock(return_value=type("Queue", (), {"enqueue_job": AsyncMock()})()),
    )
    await jobs.enqueue(
        1,
        "s",
        "build",
        {
            "continue_test_job_id": "prior",
            "text": "continue",
            "asset_ids": ["unrelated"],
        },
        "key",
    )
    payload = enqueue.call_args.args[3]
    assert payload["text"] == "run adjusted column"
    assert (
        payload["asset_ids"] == ["original-file"]
        and payload["candidate_id"] == "frozen"
    )
    assert payload["resume_test_job_id"] == "prior" and payload["mode"] == "test"
    prior["status"] = "running"
    with pytest.raises(WorkbenchError, match="可恢复"):
        await jobs.enqueue(
            1,
            "s",
            "build",
            {"continue_test_job_id": "prior", "text": "continue"},
            "key",
        )


async def test_completed_runtime_evidence_is_reused_without_running_it_again(
    monkeypatch,
):
    from types import SimpleNamespace

    from api.services.agent_workbench import evaluation
    from api.services.workflow import text_chat_runner

    case = {
        "name": "one",
        "user_messages": ["hello"],
        "assertions": [{"kind": "contains", "value": "hi"}],
    }

    async def get(org, sid, table, item):
        return (
            {"snapshot": {"workflow_json": {"nodes": []}}, "fingerprint": "c"}
            if table == "candidates"
            else {"payload": {"name": "suite", "cases": [case]}, "fingerprint": "s"}
        )

    monkeypatch.setattr(evaluation.db_client, "wb_get", get)
    for method, value in [
        ("wb_context", {"session": {"workflow_id": 1}}),
        ("wb_evaluation", {"id": "e"}),
        ("wb_evaluation_result", None),
    ]:
        monkeypatch.setattr(evaluation.db_client, method, AsyncMock(return_value=value))
    execute = AsyncMock(side_effect=AssertionError("Must not replay completed case"))
    monkeypatch.setattr(text_chat_runner, "execute_text_chat_pending_turn", execute)
    progress = AsyncMock()
    result = await evaluation.evaluate(
        1,
        "s",
        {"id": "j", "lease_token": "l"},
        "c",
        "suite",
        SimpleNamespace(llm=SimpleNamespace(model="test")),
        prior_results={
            "one": {
                "name": "one",
                "status": "passed",
                "run_id": 42,
                "transcript": [{"role": "assistant", "text": "hi"}],
                "original_evaluation_id": "original",
            }
        },
        on_progress=progress,
    )
    execute.assert_not_awaited()
    assert result["summary"]["reused"] == result["summary"]["executed"] == 1
    assert result["results"][0]["run_id"] == 42
    assert result["results"][0]["original_evaluation_id"] == "original"
    assert evaluation.db_client.wb_evaluation_result.call_args.args[-2] == "completed"


async def test_resume_chain_reuses_original_completed_cases_and_rejects_changed_contract(
    monkeypatch,
):
    from api.services.agent_workbench.evaluation import EVALUATION_VERSION

    def case(name):
        return {
            "name": name,
            "user_messages": ["hi"],
            "reference_dialogue": "reference",
            "source": {"row": name},
        }

    def result(name, status, run_id):
        return {
            "name": name,
            "status": status,
            "run_id": run_id,
            "transcript": [{"role": "assistant", "text": "response"}],
        }

    output = {
        "test_target_fingerprint": "target",
        "evaluation_model_configuration_fingerprint": "model",
    }
    records = {
        ("jobs", "new"): {
            "id": "new",
            "input": {"text": "request", "resume_test_job_id": "old"},
            "output": {**output, "evaluation_ids": ["en"]},
        },
        ("jobs", "old"): {
            "id": "old",
            "input": {"text": "request"},
            "output": {**output, "evaluation_ids": ["eo"]},
        },
        ("evaluations", "en"): {
            "id": "en",
            "job_id": "new",
            "candidate_id": "c",
            "suite_id": "s",
            "summary": {"evaluation_version": EVALUATION_VERSION},
            "results": [result("b", "failed", 12)],
        },
        ("evaluations", "eo"): {
            "id": "eo",
            "job_id": "old",
            "candidate_id": "c",
            "suite_id": "s",
            "summary": {"evaluation_version": EVALUATION_VERSION},
            "results": [result("a", "passed", 10), result("b", "error", 11)],
        },
        ("suites", "s"): {"payload": {"cases": [case("a"), case("b")]}},
    }

    async def get(org, sid, table, item):
        assert (org, sid) == (1, "session")
        return records[table, item]

    monkeypatch.setattr(tasks.db_client, "wb_get", get)
    args = (
        1,
        "session",
        "new",
        {"id": "c", "fingerprint": "target"},
        [case("a"), case("b")],
        "request",
        "model",
    )
    reused = await tasks.reusable_test_results(*args)
    assert {k: v["run_id"] for k, v in reused.items()} == {"a": 10, "b": 12}
    uncertain = records["evaluations", "en"]["results"][0]
    uncertain.update(
        status="inconclusive",
        reference_review={
            "version": 2,
            "status": "inconclusive",
            "checks": [{"category": "script_mismatch"}],
        },
    )
    reused = await tasks.reusable_test_results(*args)
    assert reused["b"]["run_id"] == 12 and not reused["b"]["review_pending"]
    uncertain["reference_review"] = {
        "status": "inconclusive",
        "reason": "connection interrupted",
    }
    reused = await tasks.reusable_test_results(*args)
    assert reused["b"]["run_id"] == 12 and reused["b"]["review_pending"]
    uncertain.update(
        status="error",
        validity={"replay_context": {"variables": {"recipient": "Frozen subject"}}},
    )
    reused = await tasks.reusable_test_results(*args)
    assert reused["b"]["replay_pending"] is True
    assert reused["b"]["validity"]["replay_context"]["variables"] == {
        "recipient": "Frozen subject"
    }
    records["evaluations", "eo"]["results"][0].update(
        status="inconclusive",
        run_id=None,
        transcript=[],
        validity={"stop_reason": "replay_context_ambiguous"},
    )
    reused = await tasks.reusable_test_results(*args)
    assert reused["a"]["validity"]["stop_reason"] == "replay_context_ambiguous"
    assert reused["a"]["review_pending"] is False
    records["evaluations", "en"]["summary"]["evaluation_version"] = 0
    records["evaluations", "eo"]["summary"] = {}
    assert await tasks.reusable_test_results(*args) == {}
    records["jobs", "old"]["input"]["text"] = "different requirements"
    with pytest.raises(WorkbenchError, match="测试要求已变化"):
        await tasks.reusable_test_results(*args)


async def test_resume_only_retries_interrupted_review_without_replaying_runtime(
    monkeypatch,
):
    from copy import deepcopy
    from types import SimpleNamespace

    from api.services.agent_workbench import evaluation
    from api.services.workflow import text_chat_runner

    case = {
        "name": "one",
        "user_messages": ["hello"],
        "reference_dialogue": "reference",
    }
    transcript = [
        {"role": "user", "text": "hello", "turn": 1},
        {"role": "assistant", "text": "hi", "turn": 1},
    ]
    previous = {
        "name": "one",
        "status": "inconclusive",
        "run_id": 42,
        "transcript": transcript,
        "events": [{"type": "node_transition", "turn": 1}],
        "tool_calls": [],
        "usage": [{"tokens": 9}],
        "assertions": [
            {"kind": "script_consumed", "passed": True, "informational": True},
            {"kind": "reference", "passed": False, "value": "review interrupted"},
        ],
        "reference_review": {"status": "inconclusive", "reason": "I/O timeout"},
        "review_pending": True,
        "original_evaluation_id": "old",
    }
    original = deepcopy(previous)

    async def get(org, sid, table, item):
        assert (org, sid) == (1, "s")
        return (
            {"snapshot": {"workflow_json": {"nodes": []}}, "fingerprint": "c"}
            if table == "candidates"
            else {"payload": {"name": "suite", "cases": [case]}, "fingerprint": "suite"}
        )

    monkeypatch.setattr(evaluation.db_client, "wb_get", get)
    for method, value in [
        ("wb_context", {"session": {"workflow_id": 1}}),
        ("wb_evaluation", {"id": "e"}),
        ("wb_evaluation_result", None),
        ("wb_emit", None),
    ]:
        monkeypatch.setattr(evaluation.db_client, method, AsyncMock(return_value=value))
    execute = AsyncMock(
        side_effect=AssertionError("Completed runtime must not run again")
    )
    create_run = AsyncMock(side_effect=AssertionError("Do not create another run"))
    update_run = AsyncMock(side_effect=AssertionError("Do not mutate the original run"))
    monkeypatch.setattr(text_chat_runner, "execute_text_chat_pending_turn", execute)
    monkeypatch.setattr(evaluation.db_client, "wb_create_run", create_run)
    monkeypatch.setattr(evaluation.db_client, "update_workflow_run", update_run)
    check = {
        "criterion": "source mismatch",
        "status": "inconclusive",
        "category": "script_mismatch",
        "turn": 1,
        "reason": "different question",
        "actual_quote": "hi",
        "reference_quote": "reference",
    }
    judge = AsyncMock(
        return_value={
            "version": 2,
            "status": "inconclusive",
            "checks": [check],
            "categories": ["script_mismatch"],
        }
    )
    result = await evaluation.evaluate(
        1,
        "s",
        {"id": "j", "lease_token": "l"},
        "c",
        "suite",
        SimpleNamespace(llm=SimpleNamespace(model="runtime")),
        reference_judge=judge,
        prior_results={"one": previous},
    )
    execute.assert_not_awaited()
    create_run.assert_not_awaited()
    update_run.assert_not_awaited()
    judge.assert_awaited_once()
    assert judge.call_args.args[1:] == (transcript, previous["events"])
    assert previous == original
    actual = result["results"][0]
    assert (
        actual["run_id"] == 42
        and actual["transcript"] == transcript
        and actual["usage"] == previous["usage"]
    )
    assert actual["previous_reference_reviews"] == [
        {"evaluation_id": "old", "review": previous["reference_review"]}
    ]
    assert actual["reference_review"]["version"] == 2
    assert len(actual["assertions"]) == 2
    assert result["summary"]["reused"] == result["summary"]["inconclusive"] == 1


async def test_read_only_dispatcher_cannot_call_mutations_or_history():
    dispatcher = tasks.StructuredDecision(
        "select_test_source", "Read only", tasks.MAPPING_SCHEMA
    )
    assert dispatcher.tuning is True  # ephemeral, no durable Agent conversation
    for name in ("save_candidate", "read_conversation", "read_agent_run"):
        with pytest.raises(WorkbenchError):
            await dispatcher.call(name, {})


async def test_resume_reuses_confirmed_mapping_only_for_identical_request_and_sources(
    monkeypatch,
):
    selection = {
        "asset_id": "file",
        "sheet": "Tests",
        "dialogue_column": "Adjusted",
        "id_column": "ID",
        "user_labels": ["Customer"],
        "assistant_labels": ["Agent"],
    }
    original = {"text": "Use adjusted dialogue", "asset_ids": ["file"]}
    records = {
        "new": {"input": {**original, "resume_test_job_id": "old"}, "output": {}},
        "old": {
            "input": original,
            "output": {"source_manifest": [{**selection, "sha256": "hash", "rows": 2}]},
        },
    }

    async def get(org, sid, table, item):
        assert (org, sid, table) == (1, "session", "jobs")
        return records[item]

    monkeypatch.setattr(tasks.db_client, "wb_get", get)
    payload = {**original, "resume_test_job_id": "new"}
    tables = [{"asset_id": "file", "sheet": "Tests", "sha256": "hash"}]
    assert await tasks.reusable_source_mapping(1, "session", payload, tables) == {
        "needs_input": "",
        "tables": [selection],
    }
    assert await tasks.reusable_source_mapping(1, "session", original, tables) is None
    with pytest.raises(WorkbenchError, match="要求或附件已变化"):
        await tasks.reusable_source_mapping(
            1, "session", {**payload, "text": "other"}, tables
        )
    tables[0]["sha256"] = "changed"
    with pytest.raises(WorkbenchError, match="附件内容已变化"):
        await tasks.reusable_source_mapping(1, "session", payload, tables)


async def test_structured_decision_finishes_only_after_validated_tool_receipt():
    from api.services.agent_workbench.codex_client import CodexClient

    dispatcher = tasks.StructuredDecision(
        "select_test_source", "Read only", tasks.MAPPING_SCHEMA
    )
    client = CodexClient(dispatcher, AsyncMock())
    result = await client.handle_request(
        "item/tool/call", {"tool": "select_test_source", "arguments": {}}
    )
    assert not result["success"] and not client.done.done()
    result = await client.handle_request(
        "item/tool/call",
        {
            "tool": "select_test_source",
            "arguments": {"needs_input": "请选择对话列", "tables": []},
        },
    )
    assert result["success"] and client.done.done()
    assert dispatcher.report["needs_input"] == "请选择对话列"


async def test_repeated_invalid_decisions_stop_even_when_payloads_differ():
    from api.services.agent_workbench.codex_client import CodexClient

    dispatcher = tasks.StructuredDecision(
        "select_test_source", "Read only", tasks.MAPPING_SCHEMA
    )
    client = CodexClient(dispatcher, AsyncMock())
    for attempt in range(3):
        result = await client.handle_request(
            "item/tool/call",
            {"tool": "select_test_source", "arguments": {"invalid": attempt}},
        )
        assert not result["success"]
    assert dispatcher.report is None and client.done.done()
    with pytest.raises(WorkbenchError, match="连续返回无效结果"):
        client.done.result()


async def test_missing_submission_gets_one_fresh_readonly_attempt(monkeypatch):
    calls = []

    class Client:
        def __init__(self, dispatcher, event):
            self.dispatcher = dispatcher

        async def run(self, *args, **kwargs):
            calls.append(self.dispatcher)
            if len(calls) == 2:
                await self.dispatcher.call("report", {"ok": True})

    monkeypatch.setattr(tasks, "CodexClient", Client)
    result = await tasks.decision(
        "report",
        "read only",
        tasks.object_schema({"ok": {"type": "boolean"}}),
        {},
        None,
        AsyncMock(),
    )
    assert result == {"ok": True}
    assert len(calls) == 2 and calls[0] is not calls[1]


async def test_attachment_pages_recover_truncated_preview_from_verified_original(
    monkeypatch,
):
    import hashlib

    from api.services.agent_workbench import attachments

    original = "x" * 200000 + "last-row"
    asset = {
        "status": "ready",
        "name": "cases.txt",
        "object_key": "private",
        "sha256": hashlib.sha256(original.encode()).hexdigest(),
        "parsed": {"text": original[:200000], "truncated": True},
    }
    monkeypatch.setattr(attachments.db_client, "wb_get", AsyncMock(return_value=asset))
    saved = AsyncMock()
    monkeypatch.setattr(attachments.db_client, "wb_asset_result", saved)

    async def download(_self, _key, path):
        path.write_text(original)

    monkeypatch.setattr(attachments.PrivateAssetStore, "download", download)
    page = await attachments.read_selected_attachment(1, "s", ["a"], "a", offset=200000)
    assert page["text"] == "last-row"
    assert page["total_characters"] == len(original)
    assert page["truncated_by_parser"] is False
    assert saved.call_args.kwargs["parsed"]["full_text"] == original


async def test_material_tail_and_literal_search_do_not_walk_all_pages(monkeypatch):
    from api.services.agent_workbench import attachments

    text = "x" * 220000 + "first [a+b] record\nlast [a+b] record"
    asset = {
        "status": "ready",
        "name": "source.txt",
        "parsed": {"text": text[:200000], "full_text": text, "truncated": True},
    }
    monkeypatch.setattr(attachments.db_client, "wb_get", AsyncMock(return_value=asset))
    tail = await attachments.read_selected_attachment(
        1, "s", ["a"], "a", limit=20, from_end=True
    )
    assert tail["text"].endswith("last [a+b] record")
    assert tail["offset"] == len(text) - 20
    first = await attachments.read_selected_attachment(
        1, "s", ["a"], "a", query="[a+b]", limit=40
    )
    assert first["found"] and first["match_offset"] > 200000
    second = await attachments.read_selected_attachment(
        1, "s", ["a"], "a", query="[a+b]", offset=first["next_match_offset"], limit=40
    )
    assert second["match_offset"] > first["match_offset"]
    missing = await attachments.read_selected_attachment(
        1, "s", ["a"], "a", query="unknown"
    )
    assert missing["found"] is False and missing["text"] == ""


def test_full_structured_import_exceeds_old_preview_without_dropping_rows(tmp_path):
    import csv

    path = tmp_path / "cases.csv"
    dialogue = "客服：" + "开场" * 1000 + "\n用户：继续\n用户：好的\n客服：结束"
    with path.open("w") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "before", "after"])
        for index in range(266):
            writer.writerow([str(index), "客服：旧\n用户：旧", dialogue])
    assert parse_file(path)["truncated"]
    table = {
        **parse_file(path, tables=True)["tables"][0],
        "asset_id": "a",
        "name": path.name,
        "sha256": "digest",
    }
    selection = {
        "asset_id": "a",
        "sheet": "CSV",
        "dialogue_column": "after",
        "id_column": "id",
        "user_labels": ["用户"],
        "assistant_labels": ["客服"],
    }
    cases, issues, manifest = tasks.compile_tables(
        [table], {"needs_input": "", "tables": [selection]}
    )
    assert len(cases) == manifest[0]["rows"] == 266
    assert not issues
    assert cases[-1]["source"]["row"] == 267
    assert cases[0]["user_messages"] == ["继续\n好的"]
    assert all(c["reference_dialogue"] == dialogue for c in cases)
    batches = tasks.pack_suites(cases, "test", "只核对流程，忽略示例编号差异")
    assert sum(len(b["cases"]) for b in batches) == 266
    assert len({c["name"] for b in batches for c in b["cases"]}) == 266
    assert all(b["instructions"] == "只核对流程，忽略示例编号差异" for b in batches)


def test_import_reports_invalid_rows_instead_of_silently_dropping_them():
    table = {
        "asset_id": "a",
        "sheet": "s",
        "name": "f",
        "sha256": "x",
        "rows": [
            ["dialogue"],
            ["座席：你好\n客户：你好"],
            ["座席：你好\n陌生人：被忽略"],
        ],
    }
    mapping = {
        "needs_input": "",
        "tables": [
            {
                "asset_id": "a",
                "sheet": "s",
                "dialogue_column": "dialogue",
                "id_column": None,
                "user_labels": ["客户"],
                "assistant_labels": ["座席"],
            }
        ],
    }
    cases, issues, manifest = tasks.compile_tables([table], mapping)
    assert len(cases) == len(issues) == 1
    assert manifest[0]["rows"] == 2
    assert issues[0]["source"]["row"] == 3
    assert issues[0]["status"] == "inconclusive"


def test_long_reference_scripts_are_supported_but_empty_tests_are_not():
    assert (
        len(
            SimulationCase(
                name="long",
                user_messages=["continue"] * 61,
                reference_dialogue="reference",
            ).user_messages
        )
        == 61
    )
    with pytest.raises(ValueError, match="need assertions"):
        SimulationCase(name="empty", user_messages=["hello"])


async def test_structured_decision_duplicate_submit_is_idempotent():
    dispatcher = tasks.StructuredDecision(
        "submit", "read only", tasks.object_schema({"value": {"type": "string"}})
    )
    await dispatcher.call("submit", {"value": "first"})
    await dispatcher.call("submit", {"value": "later"})
    assert dispatcher.report == {"value": "first"}


async def test_test_route_never_enters_builder_or_repair_loop(monkeypatch):
    from types import SimpleNamespace

    from api.services.agent_workbench import jobs, test_operations

    monkeypatch.setattr(
        test_operations, "resolve_test_operation", AsyncMock(return_value="execute")
    )

    context = {
        "session": {"model_protocol": "chat"},
        "target": {},
        "target_fingerprint": "f",
        "jobs": [{"input": {"text": "修改全部拟人度"}}],
    }
    monkeypatch.setattr(jobs.db_client, "wb_context", AsyncMock(return_value=context))
    monkeypatch.setattr(jobs.db_client, "wb_pin_models", AsyncMock(return_value={}))
    config = SimpleNamespace(llm=SimpleNamespace(api_key="test", provider="openai"))
    monkeypatch.setattr(
        jobs,
        "prepare_models",
        AsyncMock(return_value=SimpleNamespace(effective=config, snapshot={})),
    )
    monkeypatch.setattr(jobs, "model_report", lambda *_: {})
    build, repair, run = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(return_value={"outcome": "test", "summary": {"executed": 2}}),
    )
    monkeypatch.setattr(jobs, "_build", build)
    monkeypatch.setattr(jobs, "_complete_build", repair)
    monkeypatch.setattr(tasks, "run_test_task", run)
    result = await jobs._perform(
        1,
        "s",
        {
            "id": "j",
            "lease_token": "t",
            "kind": "build",
            "input": {"mode": "test", "text": "运行附件用例"},
        },
    )
    assert result["summary"]["executed"] == 2
    run.assert_awaited_once()
    build.assert_not_called()
    repair.assert_not_called()


async def test_fresh_rerun_pins_sources_and_models_but_never_resumes_results(
    monkeypatch,
):
    from api.services.agent_workbench import jobs
    from api.tasks import arq

    prior = {
        "id": "old",
        "status": "completed",
        "input": {"text": "all revised cases", "asset_ids": ["file"]},
        "output": {
            "intent": "test",
            "candidate_id": "frozen",
            "evaluation_ids": ["old-eval"],
        },
    }

    async def get(org, sid, table, item):
        assert (org, sid) == (5, "scoped")
        return prior if table == "jobs" else {"snapshot": {}}

    monkeypatch.setattr(jobs.db_client, "wb_get", get)
    monkeypatch.setattr(
        jobs.db_client, "wb_context", AsyncMock(return_value={"target": {}})
    )
    pinned = AsyncMock()
    monkeypatch.setattr(jobs, "prepare_models", pinned)
    enqueue = AsyncMock(return_value={"id": "new"})
    monkeypatch.setattr(jobs.db_client, "wb_enqueue", enqueue)
    monkeypatch.setattr(
        arq,
        "get_arq_redis",
        AsyncMock(return_value=type("Queue", (), {"enqueue_job": AsyncMock()})()),
    )
    await jobs.enqueue(
        5, "scoped", "build", {"rerun_test_job_id": "old", "text": "rerun"}, "unique"
    )
    payload = enqueue.call_args.args[3]
    assert (
        payload["text"] == "all revised cases" and payload["candidate_id"] == "frozen"
    )
    assert payload["asset_ids"] == ["file"] and payload["mode"] == "test"
    assert "resume_test_job_id" not in payload
    pinned.assert_awaited_once_with(5, {}, "build")
    with pytest.raises(ValueError, match="either resume"):
        Instruction(
            text="run",
            idempotency_key="i",
            continue_test_job_id="a",
            rerun_test_job_id="b",
        )


async def test_batch_preflight_is_bounded_before_any_case_slot_is_taken():
    active = peak = 0
    completed = []

    async def save(payload):
        return {"id": str(payload)}

    async def run(suite, callback, semaphore):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            # Models and database connections are acquired before run_case.
            # A case-only semaphore cannot protect this preflight operation.
            await asyncio.sleep(0.001)
            assert active <= 3
            async with semaphore:
                completed.append(suite["id"])
            return {"id": "e" + suite["id"], "summary": {"passed": 1, "executed": 1}}
        finally:
            active -= 1

    initial = {
        "total": 47,
        "passed": 0,
        "failed": 0,
        "error": 0,
        "inconclusive": 0,
        "executed": 0,
    }
    suites, evaluations, counts = await tasks.execute_batches(
        list(range(47)), save, run, AsyncMock(), initial, 3
    )
    assert peak == 3
    assert len(set(completed)) == len(suites) == len(evaluations) == 47
    assert counts == {**initial, "passed": 47, "executed": 47}
