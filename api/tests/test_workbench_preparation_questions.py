from copy import deepcopy
from unittest.mock import AsyncMock

import jsonschema
import pytest

from api.services.agent_workbench import test_compilation as compiler
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.fixtures import FixtureCodec
from api.services.agent_workbench.preparation_questions import (
    question_id,
    reconcile_questions,
    remaining_questions,
    validate_saved_review,
)
from api.tests.test_workbench_fixture_recovery import (
    corrections,
    prepare_inputs,
)


def inputs():
    case, plan, basis = prepare_inputs()
    case["source"] = {
        "asset_id": "source-book",
        "sha256": "source-hash",
        "sheet": "Conversations",
        "column": "dialogue_v2",
        "row": 2,
    }
    basis["sources"].append(
        {
            "id": "confirmed-choice",
            "kind": "current_request",
            "title": "Confirmed import",
            "content_hash": "confirmation",
            "text": "Use dialogue_v2. Only customer messages define the scenario; operator messages are context.",
        }
    )
    questions = [
        "Where are the uploaded conversations?",
        "Which dialogue column should be used?",
        "What is the retry limit on a separate device?",
    ]
    case["compiled"]["rubric_questions"] = questions
    case["compiled"]["source_fingerprint"] = compiler.case_fingerprint(case)
    case["compiled"]["fingerprint"] = fingerprint(
        {k: v for k, v in case["compiled"].items() if k != "fingerprint"}
    )
    return case, plan, basis, questions


def answer(evidence):
    by_id = {r["id"]: r for r in evidence["records"]}
    rows = []
    for index, q in enumerate(evidence["questions"]):
        source = (
            by_id["preparation:available_inputs"]
            if index == 0
            else by_id["basis:confirmed-choice:0"]
            if index == 1
            else None
        )
        rows.append(
            {
                "question_id": q["question_id"],
                "status": "resolved" if source else "unresolved",
                "reason": "The input is already confirmed"
                if source
                else "The original business material leaves this limit unspecified",
                "citations": [
                    {"span_id": span["span_id"]} for span in source["citation_spans"]
                ]
                if source
                else [],
            }
        )
    return {"resolutions": rows}


async def test_confirmed_import_and_choice_resolve_questions_without_changing_assertions():
    case, plan, basis, questions = inputs()
    original = deepcopy(case["compiled"]["requirements"])
    calls, saved = [], []

    async def decide(name, rules, schema, evidence, **kwargs):
        calls.append(name)
        if name == "reconcile_preparation_questions":
            assert "candidate" not in evidence
            value = answer(evidence)
        elif name == "compile_customer_fixture":
            value = FixtureCodec(case["reference_turns"]).encode(plan)
        else:
            assert name == "verify_customer_fixture"
            assert set(evidence["submission_template"]) == {
                "verdict",
                "reason",
                "corrections",
            }
            jsonschema.validate(evidence["submission_template"], schema)
            value = corrections(FixtureCodec(case["reference_turns"]))
        kwargs["validate"](value)
        return value

    async def checkpoint(row):
        saved.append(deepcopy(row))

    cases, summary = await compiler.compile_cases(
        [case],
        {"snapshot": {}},
        basis,
        decide,
        concurrency=1,
        progress=AsyncMock(),
        checkpoint=checkpoint,
    )
    assert summary["ready"] == 1
    assert summary["rows"][0]["questions"] == questions[2:]
    compiled = cases[0]["compiled"]
    assert compiled["rubric_questions"] == questions
    assert compiled["requirements"] == original
    assert calls.count("reconcile_preparation_questions") == 1
    assert saved[0]["compiled"]["question_review"] == compiled["question_review"]
    compiler.validate_compiled(cases[0])

    no_model = AsyncMock(
        side_effect=AssertionError("Known questions must reuse their evidence receipt")
    )
    reused = await reconcile_questions(
        questions, basis, cases, no_model, previous=[compiled["question_review"]]
    )
    assert reused == compiled["question_review"]
    no_model.assert_not_called()

    resumed_calls = []

    async def resume(name, rules, schema, evidence, **kwargs):
        resumed_calls.append(name)
        assert name == "verify_customer_fixture"
        value = corrections(FixtureCodec(case["reference_turns"]))
        kwargs["validate"](value)
        return value

    # Resume the actual stage checkpoint, not just the question helper's cache.
    restored, restored_summary = await compiler.compile_cases(
        [saved[-1]],
        {"snapshot": {}},
        basis,
        resume,
        concurrency=1,
        progress=AsyncMock(),
    )
    assert resumed_calls == ["verify_customer_fixture"]
    assert restored_summary["rows"][0]["questions"] == questions[2:]
    assert restored[0]["compiled"]["requirements"] == original
    assert restored[0]["compiled"]["scenario"] == cases[0]["compiled"]["scenario"]


@pytest.mark.parametrize(
    "mutation", ["omit", "duplicate", "no_evidence", "unknown_evidence"]
)
async def test_question_resolution_requires_complete_coverage_and_original_evidence(
    mutation,
):
    case, _, basis, questions = inputs()

    async def decide(name, rules, schema, evidence, **kwargs):
        value = answer(evidence)
        if mutation == "omit":
            value["resolutions"].pop()
        elif mutation == "duplicate":
            value["resolutions"][-1] = deepcopy(value["resolutions"][0])
        elif mutation == "no_evidence":
            value["resolutions"][-1]["status"] = "resolved"
        else:
            value["resolutions"][0]["citations"] = [{"span_id": "unknown"}]
        kwargs["validate"](value)
        return value

    with pytest.raises((WorkbenchError, jsonschema.ValidationError)):
        await reconcile_questions(questions, basis, [case], decide)


async def test_changed_observed_inputs_invalidate_only_question_review():
    case, _, basis, questions = inputs()
    calls = []

    async def decide(name, rules, schema, evidence, **kwargs):
        calls.append(evidence)
        value = answer(evidence)
        kwargs["validate"](value)
        return value

    first = await reconcile_questions(questions, basis, [case], decide)
    changed = deepcopy(case)
    changed["source"]["column"] = "new_selection"
    second = await reconcile_questions(
        questions, basis, [changed], decide, previous=[first]
    )
    assert len(calls) == 2
    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert case["compiled"]["requirements"] == changed["compiled"]["requirements"]


async def test_saved_review_cannot_hide_unreviewed_or_uncited_questions():
    case, _, basis, questions = inputs()

    async def decide(name, rules, schema, evidence, **kwargs):
        value = answer(evidence)
        kwargs["validate"](value)
        return value

    review = await reconcile_questions(questions, basis, [case], decide)
    case["compiled"]["question_review"] = review
    validate_saved_review(case["compiled"])
    assert remaining_questions(case["compiled"]) == questions[2:]
    assert review["resolutions"][2]["question_id"] == question_id(questions[2])
    review["resolutions"][2]["status"] = "resolved"
    with pytest.raises(WorkbenchError, match="状态或依据"):
        validate_saved_review(case["compiled"])
