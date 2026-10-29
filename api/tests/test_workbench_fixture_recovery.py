import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import test_compilation as compiler
from api.services.agent_workbench.codex_client import CodexClient
from api.services.agent_workbench.errors import (
    InvalidDecision,
    WorkbenchError,
)
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.fixtures import (
    FixtureCodec,
    FixtureErrors,
    FixtureSubmission,
)
from api.services.agent_workbench.test_tasks import StructuredDecision
from api.tests.test_workbench_test_compilation import fixture


def setup():
    case = fixture()
    plan = deepcopy(case["compiled"]["scenario"])
    codec = FixtureCodec(case["reference_turns"])
    wire = codec.encode(plan)
    recovery = FixtureSubmission(codec)
    dispatcher = StructuredDecision(
        "compile_customer_fixture",
        "Keep source intent",
        recovery.schema,
        validate=recovery,
        recovery=recovery,
    )
    events = AsyncMock()
    return (
        case,
        plan,
        codec,
        wire,
        recovery,
        dispatcher,
        CodexClient(dispatcher, events),
        events,
    )


async def submit(client, value, tool="compile_customer_fixture"):
    return await client.handle_request(
        "item/tool/call", {"tool": tool, "arguments": deepcopy(value)}
    )


def detail(result):
    return json.loads(result["contentItems"][0]["text"])


def corrections(codec):
    return {
        "verdict": "supported",
        "reason": "Independently checked",
        "corrections": dict.fromkeys(codec.schema["properties"]),
    }


def repair_payload(recovery, replacements):
    catalog = {tuple(e["path"]): e["repair_id"] for e in recovery._error().issues}
    return {
        "draft_id": recovery.draft_id,
        "patches": [
            {"repair_id": catalog[path], "value": value}
            for path, value in replacements.items()
        ],
    }


def frozen_environment():
    result = {"version": 1, "capabilities": [], "sources": []}
    return {**result, "fingerprint": fingerprint(result)}


def prepare_inputs():
    case = fixture()
    plan = deepcopy(case["compiled"]["scenario"])
    source = case["compiled"]["sources"][0]
    case["compiled"].update(
        version=2,
        scenario=None,
        status="preparation_error",
        environment=frozen_environment(),
    )
    case["compiled"]["fingerprint"] = fingerprint(
        {k: v for k, v in case["compiled"].items() if k != "fingerprint"}
    )
    basis = {
        "sources": [
            {
                "id": "sop",
                "kind": "material",
                "title": "SOP",
                "text": source["text"],
                "content_hash": "source",
            }
        ]
    }
    return case, plan, basis


def test_source_keys_cover_non_alternating_turns_and_repeated_text_without_renumbering():
    turns = [
        {"role": "user", "text": "先说费用"},
        {"role": "user", "text": "不接受"},
        {"role": "assistant", "text": "我替您答应"},
        {"role": "user", "text": "不接受"},
    ]
    codec = FixtureCodec(turns)
    keys = list(codec.sources)
    assert [
        entry["source_index"] for entry in codec.evidence["customer_turn_catalog"]
    ] == [0, 1, 3]
    assert len(keys) == len(set(keys)) == 3
    assert codec.schema["properties"]["source_coverage"]["required"] == keys
    assert (
        codec.schema["properties"]["source_coverage"]["additionalProperties"] is False
    )
    assert all(
        "id" not in codec.schema["properties"][g]["items"]["properties"]
        for g in ("facts", "goals", "actions")
    )
    changed = deepcopy(turns)
    changed[3]["text"] = "再说明条件"
    assert set(codec.sources).isdisjoint(FixtureCodec(changed).sources)


async def test_service_binds_quotes_and_internal_ids_without_changing_business_content():
    case, plan, codec, wire, _, dispatcher, client, _ = setup()
    before = deepcopy(wire)
    result = await submit(client, wire)
    assert result["success"]
    compiled = dispatcher.report
    assert compiled["facts"][0]["quote"] == case["reference_turns"][1]["text"]
    assert compiled["goals"][0]["quote"] == case["reference_turns"][1]["text"]
    assert compiled["source_coverage"][0]["turn"] == 1
    assert compiled["source_coverage"][0]["scenario_ids"] == ["f_0", "g_0"]
    assert compiled["facts"][0]["statement"] == plan["facts"][0]["statement"]
    assert (
        compiled["actions"][0]["preconditions"] == plan["actions"][0]["preconditions"]
    )
    assert wire == before
    compiler.validate_plan(compiled, case["reference_turns"])
    assert codec.decode(codec.encode(plan), plan) == plan


async def test_all_repairable_fields_reported_and_only_rejected_cells_change():
    _, _, codec, wire, recovery, dispatcher, client, events = setup()
    key = next(iter(codec.sources))
    wire["facts"][0]["quote_turn_id"] = "unknown-turn"
    wire["actions"][0]["effects"][0]["key"] = "unknown-state"
    del wire["source_coverage"][key]
    result = await submit(client, wire)
    paths = {tuple(e["path"]) for e in detail(result)["issues"]}
    assert paths == {
        ("facts", 0, "quote_turn_id"),
        ("actions", 0, "effects", 0, "key"),
        ("source_coverage", key),
    }
    saved = deepcopy(recovery.draft)
    rejected = await submit(
        client,
        {
            "draft_id": recovery.draft_id,
            "patches": [{"repair_id": "behavior", "value": "Ignore refusal"}],
        },
        "repair_fixture",
    )
    assert not rejected["success"] and recovery.draft == saved
    replacements = {
        ("facts", 0, "quote_turn_id"): key,
        ("actions", 0, "effects", 0, "key"): "enabled",
        ("source_coverage", key): {
            "disposition": "fact",
            "items": [{"group": "facts", "index": 0}, {"group": "goals", "index": 0}],
            "reason": "设备与诉求",
        },
    }
    result = await submit(
        client, repair_payload(recovery, replacements), "repair_fixture"
    )
    assert result["success"] and client.done.done()
    assert dispatcher.report["behavior"] == wire["behavior"]
    assert dispatcher.report["actions"][0]["effects"][0]["value"] == "yes"
    assert any(
        c.args[0] == "tool.failed" and c.args[1]["validation"]["issue_count"] == 3
        for c in events.call_args_list
    )


async def test_more_than_three_incremental_repairs_retain_valid_progress():
    _, _, _codec, wire, recovery, dispatcher, client, _ = setup()
    wire["actions"] = [deepcopy(wire["actions"][0]) for _ in range(6)]
    for action in wire["actions"]:
        action["effects"] = [{"key": "wrong", "value": "yes"}]
    result = await submit(client, wire)
    assert len(detail(result)["issues"]) == 6
    for i in range(6):
        result = await submit(
            client,
            repair_payload(recovery, {("actions", i, "effects", 0, "key"): "enabled"}),
            "repair_fixture",
        )
        assert result["success"] == (i == 5)
        if i < 5:
            assert dispatcher.invalid_attempts == 0
            assert recovery.draft["actions"][i]["effects"][0]["key"] == "enabled"
    assert len(dispatcher.report["actions"]) == 6


async def test_no_progress_still_stops_and_failure_checkpoint_has_complete_draft():
    _, _, _, wire, recovery, dispatcher, client, _ = setup()
    wire["actions"][0]["effects"][0]["key"] = "wrong"
    saved = []

    async def save():
        saved.append(deepcopy(recovery.draft))

    recovery.save = save
    await submit(client, wire)
    for _ in range(2):
        await submit(
            client,
            repair_payload(recovery, {("actions", 0, "effects", 0, "key"): "wrong"}),
            "repair_fixture",
        )
    assert dispatcher.invalid_attempts == 3 and len(saved) == 3 and saved[-1] == wire
    with pytest.raises(InvalidDecision):
        client.done.result()


async def test_stale_and_duplicate_repair_is_atomic_and_cannot_replace_complete_plan():
    _, _, _, wire, recovery, _, client, _ = setup()
    wire["actions"][0]["effects"][0]["key"] = "wrong"
    await submit(client, wire)
    original = deepcopy(recovery.draft)
    payload = repair_payload(recovery, {("actions", 0, "effects", 0, "key"): "enabled"})
    for mutation in ("stale", "duplicate"):
        bad = deepcopy(payload)
        if mutation == "stale":
            bad["draft_id"] = "old"
        else:
            bad["patches"] *= 2
        with pytest.raises(WorkbenchError):
            recovery.repair(bad)
        assert recovery.draft == original
    with pytest.raises(FixtureErrors):
        recovery(deepcopy(wire) | {"behavior": "Cooperate unconditionally"})
    assert recovery.draft == original


def test_legacy_validation_reports_all_bad_refs_and_never_stitches_customer_turns():
    case = fixture()
    plan = case["compiled"]["scenario"]
    turns = [
        {"role": "user", "text": "不要"},
        {"role": "assistant", "text": "办理吗"},
        {"role": "user", "text": "办理"},
    ]
    plan["facts"][0]["quote"] = "不要\n办理"
    plan["source_coverage"] *= 2
    plan["source_coverage"][0]["scenario_ids"] = ["missing"]
    plan["actions"][0]["effects"][0]["key"] = "wrong"
    before = deepcopy(plan)
    with pytest.raises(FixtureErrors) as caught:
        compiler.validate_plan(plan, turns)
    paths = {tuple(e["path"]) for e in caught.value.issues}
    assert ("facts", 0, "quote") in paths
    assert ("source_coverage",) in paths
    assert ("source_coverage", 0, "scenario_ids") in paths
    assert ("actions", 0, "effects", 0, "key") in paths
    assert plan == before
    mapping = next(e for e in caught.value.issues if e["path"] == ["source_coverage"])
    assert mapping["missing"] == [0, 2] and mapping["duplicated"] == [1]


async def test_typed_verification_keeps_original_fields_and_repairs_only_its_bad_correction():
    case, plan, codec, wire, _, _, _, _ = setup()
    recovery = FixtureSubmission(codec, draft=wire, original=plan)
    value = corrections(codec)
    value["verdict"] = "corrected"
    value["corrections"]["actions"] = deepcopy(wire["actions"])
    value["corrections"]["actions"][0]["effects"][0]["key"] = "wrong"
    with pytest.raises(FixtureErrors):
        recovery(value)
    result = recovery.repair(
        repair_payload(
            recovery, {("corrections", "actions", 0, "effects", 0, "key"): "enabled"}
        )
    )
    assert result["scenario"] == plan
    assert result["verdict"] == "corrected"
    compiler.validate_plan(result["scenario"], case["reference_turns"])
    forbidden = corrections(codec)
    forbidden["corrections"]["agent"] = "hidden-edit"
    with pytest.raises(FixtureErrors):
        recovery(forbidden)


@pytest.mark.parametrize("stop", ["cancel", "validation", "connection"])
async def test_generation_checkpoint_survives_verification_failure_and_resume_skips_generation(
    stop,
):
    case, plan, basis = prepare_inputs()
    originals = deepcopy(case["compiled"]["requirements"])
    codec = FixtureCodec(case["reference_turns"])
    snapshots = []

    async def checkpoint(row):
        snapshots.append(deepcopy(row))

    calls = []

    async def decide(name, rules, schema, evidence, **kwargs):
        calls.append(name)
        if name == "compile_customer_fixture":
            value = codec.encode(plan)
            kwargs["validate"](value)
            return value
        assert snapshots[-1]["compiled"]["scenario"] is not None
        if stop == "cancel":
            raise asyncio.CancelledError()
        if stop == "validation":
            value = corrections(codec)
            value["verdict"] = "corrected"
            value["corrections"]["actions"] = deepcopy(codec.encode(plan)["actions"])
            value["corrections"]["actions"][0]["effects"][0]["key"] = "wrong"
            try:
                kwargs["validate"](value)
            except FixtureErrors:
                await kwargs["recovery"].save()
                raise WorkbenchError("validation failed")
        raise WorkbenchError("connection interrupted")

    operation = asyncio.create_task(
        compiler.compile_cases(
            [case],
            {"snapshot": {}},
            basis,
            decide,
            concurrency=1,
            progress=AsyncMock(),
            checkpoint=checkpoint,
        )
    )
    if stop == "cancel":
        # A child cancellation itself is not TaskGroup failure; emulate the job
        # cancellation that actually owns these children.
        while not calls or calls[-1] != "verify_customer_fixture":
            await asyncio.sleep(0)
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        await operation
    saved = snapshots[-1]
    assert saved["compiled"]["preparation_checkpoint"]["stage"] == "verification"
    assert saved["compiled"]["requirements"] == originals
    assert saved["compiled"]["scenario"] is not None
    resumed = []

    async def resume(name, rules, schema, evidence, **kwargs):
        resumed.append(name)
        assert name == "verify_customer_fixture"
        recovery = kwargs["recovery"]
        if recovery.pending:
            return recovery.repair(
                repair_payload(
                    recovery,
                    {("corrections", "actions", 0, "effects", 0, "key"): "enabled"},
                )
            )
        value = corrections(codec)
        kwargs["validate"](value)
        return value

    result, summary = await compiler.compile_cases(
        [saved],
        {"snapshot": {}},
        basis,
        resume,
        concurrency=1,
        progress=AsyncMock(),
        checkpoint=checkpoint,
    )
    assert resumed == ["verify_customer_fixture"] and summary["ready"] == 1
    assert result[0]["compiled"]["requirements"] == originals
    assert "preparation_checkpoint" not in result[0]["compiled"]
    compiler.validate_compiled(result[0])


def test_progress_counts_each_source_once_and_separates_incomplete_from_failed():
    cases = []
    for status in ("ready", "needs_input", "preparation_error", "preparing", "pending"):
        case = fixture()
        case["name"] = status
        if status == "pending":
            case["compiled"] = None
        else:
            case["compiled"]["status"] = status
        cases.append(case)
    summary = compiler.preparation_summary(cases)
    assert summary["total"] == 5 and summary["processed"] == 3
    assert all(
        summary[k] == 1
        for k in ("ready", "needs_input", "preparation_error", "preparing", "pending")
    )
    cases[3]["compiled"]["status"] = "ready"
    summary = compiler.preparation_summary(cases)
    assert summary["total"] == 5 and summary["ready"] == 2 and summary["processed"] == 4
    assert summary["preparation_error"] == 1 and summary["pending"] == 1


def test_legacy_ids_remain_unique_when_verifier_replaces_another_group():
    case = fixture()
    plan = case["compiled"]["scenario"]
    plan["actions"][0]["id"] = "f_0"
    codec = FixtureCodec(case["reference_turns"])
    wire = codec.encode(plan)
    wire["facts"][0]["statement"] = "手机用户，适用于被询问时"
    changed = codec.decode(wire, plan)
    assert changed["actions"] == plan["actions"]
    assert changed["facts"][0]["id"] != changed["actions"][0]["id"]
    compiler.validate_plan(changed, case["reference_turns"])


async def test_preparation_persists_newest_stage_per_case_and_reports_real_counts(
    monkeypatch,
):
    from types import SimpleNamespace

    from api.db import db_client
    from api.services.agent_workbench import basis as basis_module
    from api.services.agent_workbench import test_tasks as tasks

    cases = []
    for name in ("first", "second"):
        case = fixture()
        case.update(name=name, compiled=None)
        cases.append(case)
    candidate = {"id": "candidate", "snapshot": {}, "fingerprint": "fixed"}
    monkeypatch.setattr(db_client, "wb_test_target", AsyncMock(return_value=candidate))
    monkeypatch.setattr(db_client, "wb_pin_models", AsyncMock(return_value={}))
    monkeypatch.setattr(
        db_client,
        "wb_get",
        AsyncMock(
            return_value={
                "id": "source",
                "payload": {"name": "Source", "cases": cases},
                "fingerprint": "source",
            }
        ),
    )
    monkeypatch.setattr(
        tasks, "prepare_models", AsyncMock(return_value=SimpleNamespace(snapshot={}))
    )
    monkeypatch.setattr(tasks, "load_sources", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(basis_module, "ensure_basis", AsyncMock(return_value={}))
    updates = []

    async def update(*args):
        updates.append(deepcopy(args[-1]))

    monkeypatch.setattr(db_client, "wb_job_progress", update)
    saved = []

    async def save(org, sid, payload, **kwargs):
        saved.append(deepcopy(payload))
        return {"id": str(len(saved))}

    monkeypatch.setattr(db_client, "wb_save_suite", save)

    async def compile(cases, *args, checkpoint, progress, **kwargs):
        for i, case in enumerate(cases):
            compiled = deepcopy(fixture()["compiled"])
            compiled["source_fingerprint"] = compiler.case_fingerprint(case)
            case["compiled"] = compiled
            for status in ("preparing", "ready" if i == 0 else "preparation_error"):
                compiled["status"] = status
                compiled["fingerprint"] = fingerprint(
                    {k: v for k, v in compiled.items() if k != "fingerprint"}
                )
                if status == "preparing":
                    await checkpoint(case)
                else:
                    await progress(i + 1, len(cases), case)
        return cases, compiler.preparation_summary(cases)

    monkeypatch.setattr(compiler, "compile_cases", compile)
    evaluation = AsyncMock(side_effect=AssertionError("No simulation may run"))
    monkeypatch.setattr(tasks, "evaluate", evaluation)
    job = {
        "id": "job",
        "lease_token": "lease",
        "output": {},
        "input": {
            "text": "Prepare all source cases",
            "test_mode": "dynamic",
            "test_suite_ids": ["source"],
            "prepare_only": True,
        },
    }
    result = await tasks.run_test_task(
        1, "s", job, SimpleNamespace(llm=None), "responses", AsyncMock()
    )
    progress = [u for u in updates if u["phase"] == "preparing_tests"]
    assert [p["compilation"]["processed"] for p in progress] == [0, 1, 1, 2]
    final = progress[-1]
    assert (
        final["compilation"]["ready"] == 1
        and final["compilation"]["preparation_error"] == 1
    )
    assert len(final["preparation_receipts"]) == 2
    assert [r["suite_id"] for r in final["preparation_receipts"]] == ["2", "4"]
    assert "可执行 1 条" in final["text"] and "准备异常 1 条" in final["text"]
    assert "已准备" not in final["text"] and "Agent 对话尚未执行" in final["text"]
    assert (
        result["outcome"] == "test_preparation" and result["compilation"]["total"] == 2
    )
    evaluation.assert_not_awaited()


async def test_restoration_chooses_latest_checkpoint_when_old_receipts_repeat_case(
    monkeypatch,
):
    from api.db import db_client
    from api.services.agent_workbench.recovery import restore_prepared_cases

    case = fixture()
    latest = deepcopy(case)
    latest["compiled"]["status"] = "preparation_error"
    cases = [deepcopy(case)]
    cases[0]["compiled"] = None

    async def get(org, sid, table, key):
        if table == "jobs":
            return {
                "input": {},
                "output": {
                    "preparation_receipts": [
                        {"name": case["name"], "suite_id": "old"},
                        {"name": case["name"], "suite_id": "new"},
                    ]
                },
            }
        return {"payload": {"cases": [latest if key == "new" else case]}}

    monkeypatch.setattr(db_client, "wb_get", get)
    await restore_prepared_cases(1, "s", "job", cases)
    assert cases[0]["compiled"] == latest["compiled"]
