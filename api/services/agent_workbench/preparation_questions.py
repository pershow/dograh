"""Reconcile preparation questions with observed inputs, never frozen assertions."""

import json
from collections import Counter
from copy import deepcopy

import jsonschema

from api.services.agent_workbench.basis import source_records
from api.services.agent_workbench.citations import (
    GUIDANCE,
    CitationSourceReader,
    CitationSubmission,
    validate_literal_citations,
)
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint


def question_id(text):
    return "question_" + fingerprint(text)[:20]


def input_records(basis, cases):
    # Counts and selected columns are observations from validated imports, not
    # model claims. Dialogue contents never become business-rule authority.
    groups = {}
    for case in cases:
        source = case.get("source") or {}
        identity = {k: source.get(k) for k in ("asset_id", "sha256", "sheet", "column")}
        key = fingerprint(identity)
        group = groups.setdefault(
            key, {**identity, "conversations": 0, "customer_turns": 0}
        )
        group["conversations"] += 1
        group["customer_turns"] += sum(
            t["role"] == "user" for t in case["reference_turns"]
        )
    imported = {
        "available_conversations": len(cases),
        "sources": sorted(groups.values(), key=fingerprint),
    }
    text = json.dumps(imported, ensure_ascii=False, sort_keys=True)
    return [r for r in source_records(basis) if r.get("source_kind") != "reference"] + [
        {
            "id": "preparation:available_inputs",
            "source": "basis",
            "source_kind": "preparation_fact",
            "title": "已成功读取的测试输入（系统观测）",
            "text": text,
            "content_hash": fingerprint(imported),
        }
    ]


def validate_review(review, questions, records):
    ids = [question_id(q) for q in questions]
    if Counter(r["question_id"] for r in review["resolutions"]) != Counter(ids):
        raise WorkbenchError("必须逐项核对所有原有问题，不能遗漏、重复或新增问题")
    for row in review["resolutions"]:
        if row["status"] == "resolved" and not row["citations"]:
            raise WorkbenchError("已解决的问题必须引用已确认的信息，不能无依据删除疑问")
    validate_literal_citations(review, "resolutions", records)


async def reconcile_questions(questions, basis, cases, decide, previous=()):
    from api.services.agent_workbench.test_compilation import CITATION, TEXT, obj

    questions = list(dict.fromkeys(questions))
    if not questions:
        return None
    records = input_records(basis, cases)
    scope = fingerprint({"questions": questions, "records": records})
    lookup = {r["id"]: r for r in records}
    schema = obj(
        {
            "resolutions": {
                "type": "array",
                "minItems": len(questions),
                "maxItems": len(questions),
                "items": obj(
                    {
                        "question_id": {"enum": [question_id(q) for q in questions]},
                        "status": {"enum": ["resolved", "unresolved"]},
                        "reason": TEXT,
                        "citations": {"type": "array", "items": CITATION},
                    }
                ),
            }
        }
    )

    def validate(value):
        jsonschema.validate(value, schema)
        validate_review(value, questions, lookup)

    for old in previous:
        if old and old.get("input_fingerprint") == scope:
            validate({"resolutions": old["resolutions"]})
            return deepcopy(old)
    reader = CitationSourceReader(records)
    submission = CitationSubmission(schema, "resolutions", reader, validate)
    result = await decide(
        "reconcile_preparation_questions",
        "Check each EXISTING question against the actual preparation inputs and original human sources. "
        "A question is resolved only when a supplied source directly answers it or an observed fact disproves its missing-input premise. "
        "Input counts and selected columns establish availability, never business policy. Reference conversations are not business authority. "
        "Respect the latest explicit user confirmation; do not ask it again. Absence from one stage's packet is not absence from the task. "
        "Keep genuinely missing business facts, contradictions and ambiguous scope unresolved. Do not invent answers, new questions, or modify any assertion. "
        "Cite the exact evidence for every resolved question. Read all unread sources. Use the user's language.\n"
        + GUIDANCE,
        submission.schema,
        {
            **reader.packet,
            "questions": [
                {"question_id": question_id(q), "text": q} for q in questions
            ],
        },
        validate=submission,
        reader=reader,
        recovery=submission,
    )
    reader.require_read()
    validate(result)
    referenced = {c["id"] for row in result["resolutions"] for c in row["citations"]}
    return {
        "version": 1,
        "input_fingerprint": scope,
        "sources": [r for r in records if r["id"] in referenced],
        **result,
    }


def validate_saved_review(compiled):
    review = compiled.get("question_review")
    if not review:
        return
    if review.get("version") != 1:
        raise WorkbenchError("问题核对记录版本不受支持")
    rows = review["resolutions"]
    ids = [r["question_id"] for r in rows]
    if len(ids) != len(set(ids)) or not {
        question_id(q) for q in compiled.get("rubric_questions", [])
    } <= set(ids):
        raise WorkbenchError("问题核对记录缺失或重复，不能隐藏未解决的问题")
    for row in rows:
        if row["status"] not in {"resolved", "unresolved"} or (
            row["status"] == "resolved" and not row["citations"]
        ):
            raise WorkbenchError("问题核对状态或依据无效")
    validate_literal_citations(
        review, "resolutions", {r["id"]: r for r in review["sources"]}
    )


def remaining_questions(compiled):
    # Original questions and assertions remain immutable; only evidence-backed
    # resolutions change what we still need to ask the user.
    resolved = {
        r["question_id"]
        for r in (compiled.get("question_review") or {}).get("resolutions", [])
        if r["status"] == "resolved"
    }
    return [
        q
        for q in compiled.get("rubric_questions", [])
        if question_id(q) not in resolved
    ]
