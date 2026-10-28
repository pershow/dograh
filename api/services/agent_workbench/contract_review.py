"""Judge every frozen requirement; never regenerate the grading criteria per run."""

from collections import Counter

from api.services.agent_workbench.basis import GUIDANCE
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.review import ReviewSourceReader, build_evidence
from api.services.agent_workbench.test_compilation import (
    CITATION,
    TEXT,
    obj,
    validate_compiled,
)

STATES = (
    "passed",
    "failed",
    "not_applicable",
    "uncovered",
    "test_error",
    "needs_input",
)
LABELS = {
    "passed": "通过",
    "failed": "未通过",
    "not_applicable": "不适用",
    "uncovered": "未覆盖",
    "test_error": "测试异常",
    "needs_input": "资料待确认",
}


def coverage(cases, results):
    """Keep whole-suite coverage separate from per-case conditional applicability."""
    indexed = {(r.get("suite_id"), r["name"]): r for r in results}
    rows = {}
    prepared = [c for c in cases if c.get("compiled")]
    if not prepared:
        return None
    for case in prepared:
        result = indexed.get((case.get("suite_id"), case["name"]), {})
        checks = {
            c.get("criterion_id"): c
            for c in (result.get("reference_review") or {}).get("checks", [])
        }
        for requirement in case["compiled"]["requirements"]:
            row = rows.setdefault(
                requirement["id"],
                {
                    "criterion_id": requirement["id"],
                    "criterion": requirement["description"],
                    **dict.fromkeys(STATES, 0),
                },
            )
            status = checks.get(requirement["id"], {}).get("status")
            if status not in STATES:
                status = "test_error"  # missing assessment is never counted as N/A
            row[status] += 1
    return {
        "case_count": len(prepared),
        "criterion_count": len(rows),
        "observed": sum(bool(r["passed"] + r["failed"]) for r in rows.values()),
        "rows": list(rows.values()),
    }


RULES = (
    GUIDANCE
    + """
Judge EVERY frozen requirement ID exactly once, without changing its meaning,
applicability, severity or expected behavior. Read all unread_source_ids first.
The rubric was compiled before execution from original business sources. Original
requirements remain authority; candidate policy is only the implementation under test.
Customer scenarios are explicit synthetic fixtures, not real-world identity or success.
Separate historical unknowns and initial-state limitations from facts established
later in THIS valid simulation. A fixture saying that an old acknowledgment did
not establish consent or completion cannot negate a later explicit customer report
backed by the delivered action/state ledger. Read the whole actual dialogue.
Do not invent a requirement to reconfirm an already established unchanged fact at
a specific turn. Require a fresh answer only where the original authority actually
requires fresh authorization or new information after a relevant disclosure/change.
A polite acknowledgment is not new consent, but neither does it erase an earlier
valid confirmation. These rules never manufacture real-world backend verification.
Evaluate the ACTUAL conversation, transitions, state and tool evidence. Wording,
nonmandatory ordering and historical example identities are not the test objective.
For each criterion choose:
passed: its applicable obligation was actually satisfied, with evidence;
failed: an applicable original obligation was violated, including omitted required
steps, premature ending, repeated already-known questions or continuing beyond a
source-defined limit. Agent-caused lack of progress is not a test-data defect;
not_applicable: this conditional obligation did not arise on the observed legitimate
branch (including a completed conversation with no trigger). This is NOT a pass;
uncovered: applicability or an expected observation remains undetermined because the
relevant conversation/evidence is incomplete, and no violation is established;
test_error: simulator/transport/tool-fixture error prevents reliable observation;
needs_input: original authority/applicability has a material unresolved conflict.
Never classify an Agent's own omission as uncovered merely because the expected step
never happened: inspect the triggering condition and actual ending. Conversely do
not fail unvisited branches whose conditions never arose. A turn limit by itself is
not a business deadline; judge demonstrated stalls/violations from original rules.
Distinguish PER-CASE applicability from WHOLE-SUITE rule coverage. A completed
cooperative mobile scenario does not test a desktop-only or refusal-only rule:
that rule is not_applicable in this case, with the observed branch as evidence.
It is not an inconclusive result merely because this customer never refused.
Never use not_applicable when the trigger did occur or its absence is uncertain.
Dataset coverage is computed separately and retains rules never exercised anywhere.
Each pass/fail needs exact ACTUAL Agent evidence. For absence cite the actual reply
that skipped the prerequisite or ended the applicable branch. No invented quotations.
Keep independently proven failures even if another criterion or later simulation is
unknown. independent_of_simulation is true only for facts established by already
delivered valid turns, unaffected by an invalid/undelivered synthetic utterance.
Classify layer=construction if candidate policy misstates the original, runtime if
correct policy was disobeyed, unknown when evidence cannot distinguish. An uncertain
layer does not erase a proven business violation. Do not prescribe fixes or reward
the Agent for its own policy. No renaming, omitting or adding criteria. Chinese.
authority_questions are retained source ambiguities, not global execution blockers.
Only a criterion affected by a conflict actually applicable to this conversation may
be needs_input. An unrelated unresolved branch does not invalidate known outcomes.
"""
)


def schema(requirements):
    return obj(
        {
            "checks": {
                "type": "array",
                "minItems": len(requirements),
                "maxItems": len(requirements),
                "items": obj(
                    {
                        "criterion_id": {"enum": [r["id"] for r in requirements]},
                        "status": {"enum": list(STATES)},
                        "reason": TEXT,
                        "applicability": TEXT,
                        "layer": {"enum": ["construction", "runtime", "unknown"]},
                        "independent_of_simulation": {"type": "boolean"},
                        "citations": {"type": "array", "items": CITATION},
                    }
                ),
            }
        }
    )


def verification_schema(requirements):
    return obj(
        {
            "reviews": {
                "type": "array",
                "minItems": len(requirements),
                "maxItems": len(requirements),
                "items": obj(
                    {
                        "criterion_id": {"enum": [r["id"] for r in requirements]},
                        "verdict": {"enum": ["supported", "corrected"]},
                        "reason": TEXT,
                        "check": {
                            "anyOf": [
                                schema(requirements)["properties"]["checks"]["items"],
                                {"type": "null"},
                            ]
                        },
                    }
                ),
            }
        }
    )


def verified_report(draft, verification, requirements, records, validity):
    expected = sorted(r["id"] for r in requirements)
    if sorted(r["criterion_id"] for r in verification["reviews"]) != expected:
        raise WorkbenchError("独立复核必须覆盖全部冻结断言，不能遗漏或重复。")
    original = {c["criterion_id"]: c for c in draft["checks"]}
    checks = []
    for item in verification["reviews"]:
        if item["verdict"] == "supported":
            if item["check"] is not None:
                raise WorkbenchError(
                    "支持原评时 check 必须为 null；有变化请标记 corrected。"
                )
            check = original[item["criterion_id"]]
        else:
            check = item["check"]
            if not check or check["criterion_id"] != item["criterion_id"]:
                raise WorkbenchError("修正必须保留同一断言标识。")
        checks.append(check)
    report = {"checks": checks}
    validate_report(report, requirements, records, validity)
    return report


def validate_report(value, requirements, records, validity):
    checks = value["checks"]
    if sorted(c["criterion_id"] for c in checks) != sorted(
        r["id"] for r in requirements
    ):
        raise WorkbenchError("必须逐项覆盖全部冻结断言，不能遗漏、重复或重命名。")
    for check in checks:
        actual = []
        for citation in check["citations"]:
            record = records.get(citation["id"], {})
            if not citation["quote"].strip() or citation["quote"] not in record.get(
                "text", ""
            ):
                raise WorkbenchError("评审引用必须逐字匹配实际证据。")
            if record.get("source") == "actual" and record.get("role") == "assistant":
                actual.append(record)
        if check["status"] in {"passed", "failed"} and not actual:
            raise WorkbenchError(
                "通过或失败必须引用实际坐席回复；资料或参考话术不能替代。"
            )
        if (
            check["status"] in {"passed", "failed"}
            and validity.get("status") in {"invalid", "inconclusive"}
            and not check["independent_of_simulation"]
        ):
            raise WorkbenchError(
                "受仿真异常影响的结论不能判通过或失败；独立缺陷需有明确证据。"
            )


def finalize(value, draft, requirements, evidence, compiled):
    records = {r["id"]: r for r in evidence["records"]}
    validate_report(value, requirements, records, evidence.get("validity") or {})
    indexed = {c["criterion_id"]: c for c in value["checks"]}
    checks = []
    categories = {
        "passed": "satisfied",
        "failed": "agent_issue",
        "not_applicable": "not_applicable",
        "uncovered": "insufficient_evidence",
        "test_error": "test_error",
        "needs_input": "source_conflict",
    }
    for requirement in requirements:
        check = indexed[requirement["id"]]
        citations = []
        for c in [*requirement["citations"], *check["citations"]]:
            citations.append(
                {
                    **{k: v for k, v in records[c["id"]].items() if k != "text"},
                    "quote": c["quote"],
                }
            )
        actual = next(
            (
                c
                for c in citations
                if c["source"] == "actual" and c.get("role") == "assistant"
            ),
            {},
        )
        checks.append(
            {
                **check,
                "criterion": requirement["description"],
                "expected_behavior": requirement["expected_behavior"],
                "severity": requirement["severity"],
                "category": categories[check["status"]],
                "citations": citations,
                "turn": actual.get("turn"),
                "actual_quote": actual.get("quote", ""),
                "reference_quote": "",
                "repair_target": "agent" if check["status"] == "failed" else "none",
                "independent_of_mismatch": check["independent_of_simulation"],
            }
        )
    counts = dict(Counter(c["status"] for c in checks))
    unresolved = any(counts.get(s) for s in ("uncovered", "test_error", "needs_input"))
    status = (
        "failed"
        if counts.get("failed")
        else "inconclusive"
        if unresolved or not counts.get("passed")
        else "passed"
    )
    before = {c["criterion_id"]: c for c in draft["checks"]}
    return {
        "version": 4,
        "status": status,
        "business_status": status,
        "reason": "；".join(
            f"{LABELS[s]} {counts[s]} 项" for s in STATES if counts.get(s)
        ),
        "checks": checks,
        "categories": sorted({c["category"] for c in checks}),
        "criterion_counts": counts,
        "coverage": "partial" if unresolved else "complete",
        "review_corrected": any(
            before[c["criterion_id"]]["status"] != c["status"] for c in checks
        ),
        "draft_review": draft,
        "verification": value,
        "compilation_fingerprint": compiled["fingerprint"],
        "rubric_fingerprint": fingerprint(requirements),
        "candidate_fingerprint": evidence["candidate_fingerprint"],
        "evidence_fingerprint": fingerprint(evidence),
    }


async def review_contract(
    case,
    transcript,
    events,
    candidate,
    llm,
    event,
    protocol,
    *,
    instructions="",
    verifier_llm=None,
):
    from api.services.agent_workbench.test_tasks import decision

    compiled = validate_compiled(case.model_dump(mode="json"))
    requirements = compiled["requirements"]
    if not requirements:
        raise WorkbenchError("没有冻结的业务断言，不能把运行完成算作业务通过。")
    evidence = build_evidence(case, transcript, events, candidate, instructions)
    # Reference conversation is only input to fixture preparation, never a target
    # transcript against which this new dialogue is graded.
    evidence["records"] = [
        r for r in evidence["records"] if r["source"] != "reference"
    ] + compiled["sources"]
    evidence["requirements"] = requirements
    evidence["frozen_scenario"] = compiled["scenario"]
    evidence["authority_questions"] = compiled["rubric_questions"]
    records = {r["id"]: r for r in evidence["records"]}
    reader = ReviewSourceReader(evidence)

    def validate(value):
        reader.require_read()
        validate_report(value, requirements, records, evidence.get("validity") or {})

    draft = await decision(
        "grade_frozen_requirements",
        RULES,
        schema(requirements),
        reader.packet,
        llm,
        event,
        protocol,
        validate=validate,
        reader=reader,
        reasoning_effort="medium",
    )
    reader = ReviewSourceReader(evidence)

    def validate_verification(value):
        reader.require_read()
        return verified_report(
            draft, value, requirements, records, evidence.get("validity") or {}
        )

    verification = await decision(
        "verify_frozen_requirements",
        RULES
        + "\nIndependently inspect every criterion and actual reply before consulting the draft. Correct misclassified omissions, false prerequisites, overlooked violations and unjustified not_applicable/pass. Return one review per frozen ID: supported with check=null when correct; corrected with a complete replacement check otherwise. Do not repeat unchanged checks.",
        verification_schema(requirements),
        {**reader.packet, "draft": draft},
        verifier_llm or llm,
        event,
        protocol,
        validate=validate_verification,
        reader=reader,
        reasoning_effort="medium",
    )
    checked = verified_report(
        draft, verification, requirements, records, evidence.get("validity") or {}
    )
    result = finalize(checked, draft, requirements, evidence, compiled)
    result["criterion_verification"] = verification
    return result
