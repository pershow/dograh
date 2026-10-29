import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from api.services.agent_workbench import test_compilation as compiler
from api.services.agent_workbench import test_tasks
from api.services.agent_workbench.citations import (
    CitationErrors,
    CitationSourceReader,
    CitationSubmission,
)
from api.services.agent_workbench.codex_client import CodexClient
from api.services.agent_workbench.errors import InvalidDecision, WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint


def sources():
    return [
        {
            "id": "basis:original:0",
            "source": "basis",
            "source_kind": "material",
            "text": "Only disclose the documented fee.\n未经同意，不得办理；失败最多重试两次。\r\n最后确认结果。",
        }
    ]


def requirement(citations):
    return {
        "description": "告知费用并确认结果",
        "applies_when": "客户询问费用或办理操作",
        "expected_behavior": "按原文说明费用，未经同意不得办理，最多重试两次并确认结果",
        "severity": "blocking",
        "citations": citations,
    }


def setup_decision():
    records = sources()
    reader = CitationSourceReader(records)
    recovery = CitationSubmission(
        compiler.RUBRIC,
        "requirements",
        reader,
        lambda value: compiler.validate_rubric(value, {r["id"]: r for r in records}),
    )
    dispatcher = test_tasks.StructuredDecision(
        "compile_business_rubric",
        "Preserve every requirement",
        recovery.schema,
        validate=recovery,
        reader=reader,
        recovery=recovery,
    )
    events = AsyncMock()
    return reader, recovery, dispatcher, CodexClient(dispatcher, events), events


async def submit(client, value, name="compile_business_rubric"):
    return await client.handle_request(
        "item/tool/call", {"tool": name, "arguments": value}
    )


def feedback(result):
    return json.loads(result["contentItems"][0]["text"])


def patch(recovery, index, span_id):
    return {
        "draft_id": recovery.draft_id,
        "patches": [
            {"item_index": 0, "citation_index": index, "span_id": span_id},
        ],
    }


def test_catalog_binds_exact_immutable_spans_without_quoting_generated_policy():
    records = sources()
    records += [
        {**records[0], "id": "reference", "source_kind": "reference"},
        {**records[0], "id": "policy", "source": "policy"},
    ]
    original = deepcopy(records)
    reader = CitationSourceReader(records)
    record = reader.packet["records"][0]
    assert records == original
    assert "".join(span["text"] for span in record["citation_spans"]) == record["text"]
    for span in record["citation_spans"]:
        assert reader.spans[span["span_id"]] == {
            "id": record["id"],
            "quote": record["text"][span["start"] : span["end"]],
        }
    assert {c["id"] for c in reader.spans.values()} == {records[0]["id"]}
    assert reader.spans == CitationSourceReader(records).spans
    changed = deepcopy(records)
    changed[0]["text"] += "不得额外承诺。"
    assert not set(reader.spans) & set(CitationSourceReader(changed).spans)


async def test_large_sources_remain_readable_and_cannot_be_skipped():
    records = [
        {**sources()[0], "id": f"basis:{i}", "text": "限制条款。" * 2000}
        for i in range(4)
    ]
    reader = CitationSourceReader(records)
    assert reader.pending
    recovery = CitationSubmission(
        compiler.RUBRIC, "requirements", reader, lambda _: None
    )
    value = {"requirements": [], "questions": ["缺少业务依据吗？"]}
    with pytest.raises(WorkbenchError, match="未读取"):
        recovery(value)
    for key in list(reader.pending):
        page = await reader.call("read_review_source", {"id": key})
        assert len(page["citation_spans"]) <= 128
        assert "".join(s["text"] for s in page["citation_spans"]) == page["text"]
    recovery(value)


def test_literal_validator_reports_all_errors_without_mutating_valid_fields():
    records = sources()
    value = {
        "requirements": [
            requirement(
                [
                    {"id": records[0]["id"], "quote": "Onlydisclosethedocumentedfee."},
                    {"id": records[0]["id"], "quote": "最多重试五次"},
                    {"id": "wrong-source", "quote": "未经同意，不得办理"},
                    {"id": records[0]["id"], "quote": "未经同意…最后确认结果。"},
                ]
            )
        ],
        "questions": [],
    }
    before = deepcopy(value)
    with pytest.raises(CitationErrors) as caught:
        compiler.validate_rubric(value, {r["id"]: r for r in records})
    assert len(caught.value.issues) == 3
    assert [x["path"] for x in caught.value.issues] == [
        f"requirements[0].citations[{i}]" for i in (1, 2, 3)
    ]
    assert caught.value.issues[0]["quote"] == "最多重试五次"
    assert value == before
    # Existing whitespace recovery remains exact; altered facts never pass.
    value["requirements"][0]["citations"] = before["requirements"][0]["citations"][:1]
    compiler.validate_rubric(value, {r["id"]: r for r in records})
    assert (
        value["requirements"][0]["citations"][0]["quote"]
        == "Only disclose the documented fee."
    )


async def test_repair_all_positions_preserves_rules_and_finishes_validated_receipt():
    reader, recovery, dispatcher, client, events = setup_decision()
    spans = list(reader.spans)
    initial = {
        "requirements": [
            requirement(
                [
                    {"span_id": spans[0]},
                    {"span_id": "typo-one"},
                    {"span_id": "typo-two"},
                ]
            )
        ],
        "questions": ["保留尚未明确的例外"],
    }
    result = await submit(client, deepcopy(initial))
    assert not result["success"] and not client.done.done()
    detail = feedback(result)
    assert len(detail["issues"]) == 2
    assert detail["repair_tool"] == dispatcher.required_tool == "repair_citations"
    assert recovery.draft == initial and dispatcher.report is None
    result = await submit(
        client,
        {
            "draft_id": detail["draft_id"],
            "patches": [
                {"item_index": 0, "citation_index": 1, "span_id": spans[1]},
                {"item_index": 0, "citation_index": 2, "span_id": spans[2]},
            ],
        },
        "repair_citations",
    )
    assert result["success"] and client.done.done()
    expected = deepcopy(initial)
    expected["requirements"][0]["citations"] = [reader.spans[x] for x in spans[:3]]
    assert dispatcher.report == expected
    failed_event = next(
        c.args[1] for c in events.call_args_list if c.args[0] == "tool.failed"
    )
    assert failed_event["validation"]["issue_count"] == 2
    assert failed_event["validation"]["issues"][0]["span_id"] == "typo-one"


@pytest.mark.parametrize(
    "attempt", ["rewrite", "valid_slot", "weaken", "duplicate", "stale"]
)
async def test_repair_cannot_drop_or_weaken_requirements_or_change_accepted_evidence(
    attempt,
):
    reader, recovery, dispatcher, client, _ = setup_decision()
    span = next(iter(reader.spans))
    value = {
        "requirements": [requirement([{"span_id": span}, {"span_id": "bad"}])],
        "questions": [],
    }
    await submit(client, deepcopy(value))
    before = deepcopy(recovery.draft)
    repair = patch(recovery, 1, span)
    name = "repair_citations"
    if attempt == "rewrite":
        name = "compile_business_rubric"
        repair = {"requirements": [], "questions": []}
    elif attempt == "valid_slot":
        repair["patches"][0]["citation_index"] = 0
    elif attempt == "weaken":
        repair["patches"][0]["severity"] = "minor"
    elif attempt == "duplicate":
        repair["patches"] *= 2
    else:
        repair["draft_id"] = "old"
    result = await submit(client, repair, name)
    assert not result["success"] and not client.done.done()
    assert recovery.draft == before and dispatcher.report is None
    result = await submit(client, patch(recovery, 1, span), "repair_citations")
    assert result["success"]
    assert dispatcher.report["requirements"][0]["severity"] == "blocking"
    assert len(dispatcher.report["requirements"]) == 1


async def test_partial_progress_is_retained_and_not_limited_to_three_total_repairs():
    reader, recovery, dispatcher, client, _ = setup_decision()
    span = next(iter(reader.spans))
    value = {
        "requirements": [requirement([{"span_id": f"bad-{i}"} for i in range(6)])],
        "questions": [],
    }
    await submit(client, value)
    for i in range(6):
        result = await submit(client, patch(recovery, i, span), "repair_citations")
        assert result["success"] == (i == 5)
        if i < 5:
            assert len(feedback(result)["issues"]) == 5 - i
            assert dispatcher.invalid_attempts == 0 and not client.done.done()
            assert recovery.draft["requirements"][0]["citations"][i] == {
                "span_id": span
            }
    assert len(dispatcher.report["requirements"][0]["citations"]) == 6


async def test_no_progress_stops_and_terminal_diagnostic_is_persisted_first():
    _, recovery, dispatcher, client, _ = setup_decision()
    observed = []

    async def event(kind, payload):
        if kind == "tool.failed":
            await asyncio.sleep(0)
            observed.append((client.done.done(), payload))

    client.on_event = event
    await submit(
        client, {"requirements": [requirement([{"span_id": "bad"}])], "questions": []}
    )
    for i in range(2):
        result = await submit(
            client, patch(recovery, 0, f"still-bad-{i}"), "repair_citations"
        )
        assert not result["success"]
    assert dispatcher.report is None and dispatcher.invalid_attempts == 3
    assert len(observed) == 3 and all(not done for done, _ in observed)
    assert observed[-1][1]["validation"]["issues"][0]["span_id"] == "still-bad-1"
    with pytest.raises(InvalidDecision, match="连续返回无效结果"):
        client.done.result()


async def test_all_errors_reach_model_while_persisted_diagnostic_is_bounded():
    _, _, _, client, events = setup_decision()
    value = {"requirements": [requirement([{"span_id": "bad"}] * 100)], "questions": []}
    result = await submit(client, value)
    assert len(feedback(result)["issues"]) == 100
    diagnostic = events.call_args.args[1]["validation"]
    assert diagnostic["issue_count"] == 100
    assert len(diagnostic["issues"]) == 20


async def test_compile_and_independent_verify_bind_selections_to_unchanged_sources():
    basis = {
        "sources": [
            {
                "id": "sop",
                "kind": "material",
                "title": "Policy",
                "text": sources()[0]["text"],
                "content_hash": "original",
            }
        ]
    }
    seen = []

    async def decide(name, rules, schema, evidence, **kwargs):
        seen.append(name)
        spans = evidence["records"][0]["citation_spans"]
        assert kwargs["recovery"].schema is schema
        assert schema["properties"]["requirements"]["items"]["properties"]["citations"][
            "items"
        ]["required"] == ["span_id"]
        assert "draft" not in evidence or evidence["draft"]["requirements"][0][
            "citations"
        ] == [{"span_id": spans[0]["span_id"]}]
        value = {
            "requirements": [requirement([{"span_id": spans[0]["span_id"]}])],
            "questions": [],
        }
        kwargs["validate"](value)
        return value

    requirements, records, questions = await compiler.compile_rubric(basis, decide)
    assert seen == ["compile_business_rubric", "verify_business_rubric"]
    assert requirements[0]["citations"] == [
        {"id": records[0]["id"], "quote": "Only disclose the documented fee.\n"}
    ]
    assert (
        requirements[0]["id"]
        == "req_"
        + fingerprint({k: v for k, v in requirements[0].items() if k != "id"})[:20]
    )
    assert questions == [] and records[0]["text"] == basis["sources"][0]["text"]


async def test_new_turn_resumes_locked_citation_repairs_without_losing_rules(
    monkeypatch,
):
    reader, recovery, _, _, _ = setup_decision()
    span = next(iter(reader.spans))
    turns = []

    class Client(CodexClient):
        async def run(self, *_args, **_kwargs):
            turns.append(self.dispatcher)
            if len(turns) == 1:
                await submit(
                    self,
                    {
                        "requirements": [requirement([{"span_id": "bad"}])],
                        "questions": [],
                    },
                )
            else:
                assert self.dispatcher.required_tool == "repair_citations"
                assert recovery.draft_id in self.dispatcher.instructions
                assert "must call repair_citations" in self.dispatcher.instructions
                assert (
                    "must call compile_business_rubric"
                    not in self.dispatcher.instructions
                )
                await submit(self, patch(recovery, 0, span), "repair_citations")

    monkeypatch.setattr(test_tasks, "CodexClient", Client)
    result = await test_tasks.decision(
        "compile_business_rubric",
        "Preserve rules",
        recovery.schema,
        {},
        None,
        AsyncMock(),
        validate=recovery,
        reader=reader,
        recovery=recovery,
    )
    assert len(turns) == 2
    assert (
        result["requirements"][0]["expected_behavior"]
        == requirement([])["expected_behavior"]
    )


async def test_environment_generation_uses_same_binding_and_independent_verification():
    basis = {
        "sources": [
            {
                "id": "instructions",
                "kind": "material",
                "title": "UI operations",
                "text": "点击设置进入管理页。\n仍需用户同意才能保存。",
                "content_hash": "source",
            }
        ]
    }
    seen = []

    async def decide(name, rules, schema, evidence, **kwargs):
        seen.append(name)
        spans = evidence["records"][0]["citation_spans"]
        value = {
            "capabilities": [
                {
                    "operation": "点击设置",
                    "available_when": "已打开设置入口",
                    "outcome": "进入管理页",
                    "test_assumptions": [],
                    "citations": [{"span_id": spans[0]["span_id"]}],
                }
            ]
        }
        if "draft" in evidence:
            assert evidence["draft"] == value
        kwargs["validate"](value)
        return value

    environment = await compiler.compile_environment(basis, decide)
    compiler.validate_environment(environment)
    assert seen == ["compile_test_environment", "verify_test_environment"]
    assert environment["capabilities"][0]["citations"][0] == {
        "id": "basis:instructions:0",
        "quote": "点击设置进入管理页。\n",
    }
    corrupted = deepcopy(environment)
    corrupted["capabilities"][0]["citations"][0]["quote"] = "无需同意即可保存。"
    corrupted["fingerprint"] = fingerprint(
        {k: v for k, v in corrupted.items() if k != "fingerprint"}
    )
    with pytest.raises(WorkbenchError, match="原始业务"):
        compiler.validate_environment(corrupted)


@pytest.mark.parametrize("name", [None, "repair_citations", "save_candidate"])
async def test_repair_capability_is_not_available_on_unrelated_decisions(name):
    dispatcher = test_tasks.StructuredDecision(
        "submit", "Read only", {"type": "object"}
    )
    with pytest.raises(WorkbenchError, match="only permits"):
        await dispatcher.call(name, {})
    assert dispatcher.report is None
