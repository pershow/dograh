"""Compile immutable conversations into executable fixtures, before any Agent run.

The rubric only sees human business sources. The customer planner only sees the
source conversation and public test context. Neither sees candidate instructions.
Every source row survives preparation, including rows that need information.
"""

import asyncio
from copy import deepcopy

import jsonschema

from api.services.agent_workbench.basis import GUIDANCE, source_records
from api.services.agent_workbench.citations import (
    GUIDANCE as CITATION_GUIDANCE,
)
from api.services.agent_workbench.citations import (
    CitationSourceReader,
    CitationSubmission,
    validate_literal_citations,
)
from api.services.agent_workbench.errors import LeaseLost, WorkbenchError
from api.services.agent_workbench.fingerprint import fingerprint
from api.services.agent_workbench.simulation import (
    SCENARIO,
    SCENARIO_RULES,
    original_quote,
)


def obj(properties):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


TEXT = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": TEXT}
CITATION = obj({"id": TEXT, "quote": TEXT})
REQUIREMENT = obj(
    {
        "description": TEXT,
        "applies_when": TEXT,
        "expected_behavior": TEXT,
        "severity": {"enum": ["blocking", "minor"]},
        "citations": {"type": "array", "minItems": 1, "items": CITATION},
    }
)
RUBRIC = obj(
    {
        "requirements": {"type": "array", "items": REQUIREMENT, "maxItems": 80},
        "questions": STRINGS,
    }
)
ENVIRONMENT = obj(
    {
        "capabilities": {
            "type": "array",
            "maxItems": 80,
            "items": obj(
                {
                    "operation": TEXT,
                    "available_when": TEXT,
                    "outcome": TEXT,
                    "test_assumptions": STRINGS,
                    "citations": {"type": "array", "minItems": 1, "items": CITATION},
                }
            ),
        },
    }
)
ENVIRONMENT_RULES = (
    GUIDANCE
    + """
Compile the independent SYNTHETIC TEST ENVIRONMENT from original human business
sources. Read every unread source. Extract observable UI controls, navigation,
ordinary operations and their documented results, NOT how the Agent should talk,
which branch it should choose, marketing claims or willingness the customer must have.
Each capability needs a literal original citation supporting the operation.
Where the source describes ordinary operations but omits their mechanical result,
choose a minimal explicit synthetic result in test_assumptions (such as a control
opening its documented next page). Historical silence is not a permanent simulated
failure. Do not infer real authorization, verified identity, payments, backend
verification or actual business success. Unspecified external effects stay unavailable.
Keep intermediate confirmation screens, failed-operation variants and prerequisites.
This environment supplies capabilities only: each customer fixture separately decides
access, willingness, actual operation state and source-supported failure conditions.
Never remove a source customer's refusal or failure to make the environment usable.
Return Chinese descriptions; no instructions that prescribe Agent replies.
"""
)
MISSING = obj(
    {"question": TEXT, "reason": TEXT, "blocks_execution": {"type": "boolean"}}
)
STATE = obj({"key": TEXT, "value": {"type": "string"}})
ACTION = obj(
    {
        "id": TEXT,
        "description": TEXT,
        "preconditions": {"type": "array", "items": STATE},
        "effects": {"type": "array", "items": STATE},
        "result": TEXT,
        "basis": TEXT,
    }
)
PLAN = obj(
    {
        **SCENARIO["properties"],
        "initial_state": {"type": "array", "items": STATE},
        "actions": {"type": "array", "items": ACTION},
        "missing_information": {"type": "array", "items": MISSING},
        "source_coverage": {
            "type": "array",
            "items": obj(
                {
                    "turn": {"type": "integer", "minimum": 0},
                    "disposition": {
                        "enum": [
                            "fact",
                            "goal",
                            "action",
                            "context",
                            "filler",
                            "unresolved",
                        ]
                    },
                    "scenario_ids": STRINGS,
                    "reason": TEXT,
                }
            ),
        },
    }
)
PLAN_VERIFICATION = obj(
    {
        "verdict": {"enum": ["supported", "corrected", "needs_input"]},
        "reason": TEXT,
        "patches": {
            "type": "array",
            "maxItems": 80,
            "items": obj(
                {
                    "path": {"type": "array", "minItems": 1, "items": TEXT},
                    "value": {},
                }
            ),
        },
    }
)
RUBRIC_RULES = (
    GUIDANCE
    + """
Compile a frozen business acceptance rubric from ORIGINAL human business sources.
Read every unread_source_id before submitting. Cover every material fact, branch,
prerequisite, prohibition, retry/retention limit and required ending in the originals.
Each item must have explicit applicability and observable expected behavior, with
literal source citations. Combine duplicate clauses; keep distinct conditional
requirements separate. Preserve exceptions, counts and deadlines exactly. Include
all applicable constraints; do not infer a deadline from example utterance order.
Reference conversations, historical audit labels and generated Agent instructions
are NOT business authority. Test requests without business content add no rules.
Report unresolved business contradictions/missing authority as questions. Never
invent a convenient rule, mandatory field or more restrictive process. Chinese.
"""
)
PLAN_RULES = (
    SCENARIO_RULES
    + """
This is preparation, not execution. Build a COMPLETE, reusable customer fixture.
Your unit of work is exactly the ONE supplied source_conversation. The orchestrator
owns the full dataset and independently processes every other row. A batch-wide
request is background scope, not a request for this decision to receive or audit
other conversations. Missing information can only concern this fixture's facts,
intent or mechanics. Do not request the rest of the dataset or claim it was audited.
The independent test_environment describes available mechanics, not customer intent
or Agent policy. Use it to instantiate ordinary operations and explicit synthetic
results. Source customer failures/objections override generic environment assumptions.
Historical omission of a later confirmation does not mean the synthetic UI must
remain forever unconfirmed. Provide an operation for each ordinary control the
fixture exposes, with a concrete result or source-grounded failure variant. No
operation may silently grant willingness, consent, payment or backend verification.
Customer-private facts stay private until naturally disclosed. Public test context
is fixed: it is the identity/configuration used for this synthetic call. Historical
operator identifiers are not targets to repeat. Represent the source's intended
participant relationship to the called subject; explicitly preserve denial/wrong
number, corrections and uncertainty. A fictional counterpart can have a clearly
labelled ordinary role/device/access assumption; do not require real-world proof.
Do not stop merely because names in the historical example differ from test aliases.
Resolve ordinary missing simulation prerequisites with minimal explicit assumptions
supported by the scenario. Use missing_information only for material conflicts that
cannot be instantiated without changing the test intent; provide a concrete question.
Never promote late completion, consent or account outcomes into initial state.
initial_state and actions form a finite simulated environment. Use concise string
state values. Action preconditions/effects refer to declared state keys; actions
require an actual Agent request or a source-supported proactive action. Distinguish
attempts and their outcomes; repeated failures can have successive state transitions.
Provide actions for ordinary operations supported by the source, not just its last
successful operation. Describe conditional simulated UI results explicitly as test
assumptions, not facts about real systems. Do not give the customer hidden SOP rules
or instructions about how the Agent must behave. Never prescribe Agent replies.
Cover EVERY customer turn (zero-based source index) in source_coverage, once each.
Every material concern/fact/action must map to scenario IDs; fillers can map to none.
Use disjoint identifier prefixes: f_ for facts, g_ for goals, a_ for actions.
For each quote copy ONE short contiguous CUSTOMER substring. Do not concatenate
separate turns, insert ellipses, change punctuation, or quote assistant suggestions.
The description may summarize a concern; its evidence quote must remain literal.
Preserve the source's objections and conditional willingness. Do not make the customer
more cooperative to obtain a passing test. Unresolvable input remains visible.
"""
)


def case_fingerprint(case):
    return fingerprint({k: v for k, v in case.items() if k not in {"compiled", "mode"}})


def validate_rubric(value, records):
    jsonschema.validate(value, RUBRIC)
    validate_literal_citations(value, "requirements", records)


def validate_plan(value, turns):
    jsonschema.validate(value, PLAN)
    customer = "\n".join(t["text"] for t in turns if t["role"] == "user")
    ids = [x["id"] for k in ("facts", "goals", "actions") for x in value[k]]
    if len(ids) != len(set(ids)) or any(not x.strip() for x in ids):
        duplicates = sorted({x for x in ids if ids.count(x) > 1})
        raise WorkbenchError(
            f"场景标识重复或为空：{duplicates}。facts/goals/actions 全局唯一，分别使用 f_/g_/a_ 前缀，并更新 source_coverage 引用。"
        )
    for fi, fact in enumerate(value["facts"]):
        if fact["provenance"] == "customer_quote":
            quote = original_quote(fact["quote"], customer)
            if not fact["quote"].strip() or quote is None:
                raise WorkbenchError(
                    f"facts[{fi}] ({fact['id']}).quote 未匹配客户原话；复制一个短的连续原文片段，推断必须标为测试假设。"
                )
            fact["quote"] = quote
        elif not fact["reason"].strip():
            raise WorkbenchError("测试假设必须说明依据。")
    for gi, goal in enumerate(value["goals"]):
        quote = original_quote(goal["quote"], customer)
        if not goal["quote"].strip() or quote is None:
            raise WorkbenchError(
                f"goals[{gi}] ({goal['id']}).quote 未匹配客户原话；复制一个短的连续客户原句，不要拼接多轮、加省略号或引用坐席。保留诉求，只修正引用。"
            )
        goal["quote"] = quote
    expected = [i for i, t in enumerate(turns) if t["role"] == "user"]
    if sorted(x["turn"] for x in value["source_coverage"]) != expected:
        raise WorkbenchError("转换映射必须覆盖每条客户原话且不能重复。")
    for row in value["source_coverage"]:
        if not set(row["scenario_ids"]) <= set(ids):
            raise WorkbenchError("原文映射引用了不存在的场景项目。")
        if row["disposition"] in {"fact", "goal", "action"} and not row["scenario_ids"]:
            raise WorkbenchError("实质客户信息不能丢失，必须映射到场景项目。")
    keys = [s["key"] for s in value["initial_state"]]
    if len(keys) != len(set(keys)):
        raise WorkbenchError("模拟状态标识重复。")
    for action in value["actions"]:
        for part in ("preconditions", "effects"):
            names = [s["key"] for s in action[part]]
            if len(names) != len(set(names)) or not set(names) <= set(keys):
                raise WorkbenchError("操作条件和结果必须引用已声明的唯一状态。")


def verified_plan(draft, verification, turns):
    """Apply structural corrections only to this generated fixture, never sources."""
    result = deepcopy(draft)
    if verification["verdict"] == "supported" and verification["patches"]:
        raise WorkbenchError("存在修正时必须标记 corrected，不能声称原草案完全正确。")
    for patch in verification["patches"]:
        path = patch["path"]
        if not path or path[0] not in PLAN["properties"]:
            raise WorkbenchError(
                "只能修正本次场景的结构化字段，不能编辑原文、断言或 Agent。"
            )
        parent = result
        try:
            for key in path[:-1]:
                parent = (
                    parent[int(key)]
                    if isinstance(parent, list) and key.isdecimal()
                    else parent[key]
                )
            key = path[-1]
            if isinstance(parent, list):
                if not key.isdecimal() or int(key) >= len(parent):
                    raise KeyError(key)
                parent[int(key)] = deepcopy(patch["value"])
            elif isinstance(parent, dict) and key in parent:
                parent[key] = deepcopy(patch["value"])
            else:
                raise KeyError(key)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise WorkbenchError(
                f"修正路径不存在：{path}；使用已有字段，新增/删除列表项时替换对应完整列表。"
            ) from error
    validate_plan(result, turns)
    if verification["verdict"] == "needs_input" and not any(
        x["blocks_execution"] for x in result["missing_information"]
    ):
        raise WorkbenchError("需要补充信息时必须保留具体问题和阻塞原因。")
    return result


def validate_compiled(case, *, records=None):
    compiled = case.get("compiled")
    if not compiled or compiled.get("version") not in {1, 2}:
        raise WorkbenchError("缺少支持版本的测试准备结果。")
    if compiled.get("source_fingerprint") != case_fingerprint(case):
        raise WorkbenchError("原始用例已变化，需重新准备测试，不能复用旧场景。")
    if compiled.get("fingerprint") != fingerprint(
        {k: v for k, v in compiled.items() if k != "fingerprint"}
    ):
        raise WorkbenchError("冻结的测试准备结果校验失败。")
    if compiled.get("scenario") is not None:
        validate_plan(deepcopy(compiled["scenario"]), case["reference_turns"])
    elif compiled["status"] == "ready":
        raise WorkbenchError("可执行测试缺少客户场景。")
    rubric = {
        "requirements": [
            {k: v for k, v in r.items() if k != "id"} for r in compiled["requirements"]
        ],
        "questions": compiled.get("rubric_questions", []),
    }
    validate_rubric(rubric, records or {r["id"]: r for r in compiled["sources"]})
    if compiled["status"] == "ready" and (
        not rubric["requirements"]
        or any(
            x["blocks_execution"] for x in compiled["scenario"]["missing_information"]
        )
    ):
        raise WorkbenchError("可执行测试仍缺少必要场景信息或验收依据。")
    for r in compiled["requirements"]:
        if (
            r["id"]
            != "req_" + fingerprint({k: v for k, v in r.items() if k != "id"})[:20]
        ):
            raise WorkbenchError("业务断言标识不匹配，不能更换含义后沿用原标识。")
    if compiled["version"] == 2:
        validate_environment(compiled["environment"])
    return compiled


def validate_environment(environment):
    value = {"capabilities": environment["capabilities"]}
    jsonschema.validate(value, ENVIRONMENT)
    if environment["fingerprint"] != fingerprint(
        {k: v for k, v in environment.items() if k != "fingerprint"}
    ):
        raise WorkbenchError("冻结测试环境校验失败。")
    records = {r["id"]: r for r in environment["sources"]}
    for item in value["capabilities"]:
        for citation in item["citations"]:
            source = records.get(citation["id"], {})
            if (
                source.get("source") != "basis"
                or source.get("source_kind") == "reference"
                or not citation["quote"]
                or citation["quote"] not in source.get("text", "")
            ):
                raise WorkbenchError("测试环境必须引用原始业务资料中的实际操作依据。")


async def cited_decision(decide, name, rules, schema, group, records, draft=None):
    reader = CitationSourceReader(records)
    lookup = {r["id"]: r for r in records}

    def validate(value):
        jsonschema.validate(value, schema)
        validate_literal_citations(value, group, lookup)

    submission = CitationSubmission(schema, group, reader, validate)
    evidence = dict(reader.packet)
    if draft is not None:
        evidence["draft"] = reader.encode(draft, group)
    value = await decide(
        name,
        rules + CITATION_GUIDANCE,
        submission.schema,
        evidence,
        validate=submission,
        reader=reader,
        recovery=submission,
    )
    reader.require_read()
    validate(value)
    return value


async def compile_environment(basis, decide):
    records = source_records(basis)

    draft = await cited_decision(
        decide,
        "compile_test_environment",
        ENVIRONMENT_RULES,
        ENVIRONMENT,
        "capabilities",
        records,
    )
    value = await cited_decision(
        decide,
        "verify_test_environment",
        ENVIRONMENT_RULES
        + "\nIndependently verify every capability, missing intermediate operation and source quote. Remove hidden Agent-behavior rules; preserve ordinary supported mechanics. Return the complete corrected environment.",
        ENVIRONMENT,
        "capabilities",
        records,
        draft,
    )
    referenced = {c["id"] for item in value["capabilities"] for c in item["citations"]}
    environment = {
        "version": 1,
        **value,
        "sources": [r for r in records if r["id"] in referenced],
    }
    environment["fingerprint"] = fingerprint(environment)
    validate_environment(environment)
    return environment


async def compile_rubric(basis, decide):
    records = source_records(basis)

    draft = await cited_decision(
        decide,
        "compile_business_rubric",
        RUBRIC_RULES,
        RUBRIC,
        "requirements",
        records,
    )
    rubric = await cited_decision(
        decide,
        "verify_business_rubric",
        RUBRIC_RULES
        + "\nIndependently check the complete originals for omitted requirements, invented constraints and changed conditions. Return the corrected COMPLETE rubric, preserving every source requirement.",
        RUBRIC,
        "requirements",
        records,
        draft,
    )
    requirements = [
        {"id": "req_" + fingerprint(r)[:20], **r} for r in rubric["requirements"]
    ]
    if len({r["id"] for r in requirements}) != len(requirements):
        raise WorkbenchError("业务断言重复，无法冻结。")
    referenced = {c["id"] for r in requirements for c in r["citations"]}
    return (
        requirements,
        [r for r in records if r["id"] in referenced],
        rubric["questions"],
    )


async def compile_cases(
    cases, candidate, basis, decide, *, concurrency, progress, retry_preparation=False
):
    """Return all rows in original order. Failed preparation is a visible receipt."""
    cases = deepcopy(cases)
    selected = [c for c in cases if c["mode"] == "dynamic"]
    if not selected:
        return cases, None
    records = {r["id"]: r for r in source_records(basis)}
    business_fingerprint = fingerprint(
        sorted(
            [
                r
                for r in records.values()
                if r.get("source_kind") not in {"current_request", "reference"}
            ],
            key=lambda r: r["id"],
        )
    )
    for case in selected:
        if case.get("compiled"):
            validate_compiled(case, records=records)
    pending = [
        c
        for c in selected
        if not c.get("compiled")
        or (
            retry_preparation
            and (c["compiled"]["status"] != "ready" or c["compiled"]["version"] < 2)
        )
    ]
    frozen = next(
        (
            c["compiled"]
            for c in selected
            if c.get("compiled") and c["compiled"]["requirements"]
        ),
        None,
    )
    # Preparing an existing test set upgrades its simulation fixture, not its
    # acceptance standard. Source discovery can add duplicate historical requests
    # or test-only instructions; that must never silently regenerate frozen rules.
    # Validate their original citations above, then keep each existing contract.
    # New raw rows can inherit a single established rubric; mixed contracts need
    # an explicit source selection rather than picking an arbitrary first one.
    contracts = {
        fingerprint(c["compiled"]["requirements"])
        for c in selected
        if c.get("compiled") and c["compiled"]["requirements"]
    }
    if any(not c.get("compiled") for c in pending) and len(contracts) > 1:
        raise WorkbenchError(
            "测试集包含不同的冻结业务断言，新增用例需明确其所属测试集。"
        )
    requirements, sources, questions = (
        (frozen["requirements"], frozen["sources"], frozen["rubric_questions"])
        if frozen
        else await compile_rubric(basis, decide)
        if pending
        else ([], [], [])
    )
    environment = next(
        (
            c["compiled"]["environment"]
            for c in selected
            if (c.get("compiled") or {}).get("version") == 2
        ),
        None,
    )
    if pending and environment is None:
        environment = await compile_environment(basis, decide)
    semaphore = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    done = len(selected) - len(pending)

    async def one(case):
        nonlocal done
        prior = case.get("compiled") or frozen or {}
        public = {
            **candidate["snapshot"].get("template_context_variables", {}),
            **case["initial_context"],
        }
        compiled = {
            "version": 2,
            "environment": environment,
            "supersedes": (case.get("compiled") or {}).get("fingerprint"),
            "business_sources_fingerprint": prior.get(
                "business_sources_fingerprint", business_fingerprint
            ),
            "source_fingerprint": case_fingerprint(case),
            "requirements": prior.get("requirements") or requirements,
            "sources": prior.get("sources") or sources,
            "rubric_questions": prior.get("rubric_questions", questions),
            "public_context": deepcopy(case["initial_context"]),
            "scenario": deepcopy((case.get("compiled") or {}).get("scenario")),
            "status": "needs_input",
            "error": None,
        }
        async with semaphore:
            try:

                def validate(value):
                    validate_plan(value, case["reference_turns"])

                evidence = {
                    "preparation_scope": {
                        "unit": "one_source_conversation",
                        "case_name": case["name"],
                        "remaining_rows_owner": "orchestrator",
                    },
                    "source_conversation": case["reference_turns"],
                    "public_test_context": public,
                    "test_environment": [
                        {k: v for k, v in c.items() if k != "citations"}
                        for c in environment["capabilities"]
                    ],
                    "current_preparation_request": next(
                        (
                            s["text"]
                            for s in basis["sources"]
                            if s["kind"] == "current_request"
                        ),
                        "",
                    ),
                }
                plan = deepcopy((case.get("compiled") or {}).get("scenario"))
                if plan is None:
                    plan = await decide(
                        "compile_customer_fixture",
                        PLAN_RULES,
                        PLAN,
                        evidence,
                        validate=validate,
                    )
                verification = await decide(
                    "verify_customer_fixture",
                    PLAN_RULES
                    + "\nAudit independently: each material customer concern must survive, state transitions cannot leak future outcomes, and ordinary questions must be answerable. Do not soften refusal or invent permission. If correct, return supported with no patches. Otherwise return structural replacements ONLY for fields needing correction: path is an array of keys/zero-based array indices, value replaces the existing field. To add/remove array items replace that array. Never edit source material, business assertions or Agent. Do not repeat unchanged fields. A real missing prerequisite must be recorded in missing_information.",
                    PLAN_VERIFICATION,
                    {**evidence, "draft": plan, "target_schema": PLAN},
                    validate=lambda value: verified_plan(
                        plan, value, case["reference_turns"]
                    ),
                )
                plan = verified_plan(plan, verification, case["reference_turns"])
                validate(plan)
                compiled["scenario"] = plan
                compiled["scenario_verification"] = verification
                compiled["status"] = (
                    "ready"
                    if compiled["requirements"]
                    and not any(
                        x["blocks_execution"] for x in plan["missing_information"]
                    )
                    else "needs_input"
                )
            except LeaseLost:
                raise
            except WorkbenchError as error:
                compiled["status"], compiled["error"] = "preparation_error", str(error)
        compiled["fingerprint"] = fingerprint(compiled)
        case["compiled"] = compiled
        async with lock:
            done += 1
            await progress(done, len(selected), case)

    async with asyncio.TaskGroup() as group:
        for case in pending:
            group.create_task(one(case))
    counts = {
        s: sum(c["compiled"]["status"] == s for c in selected)
        for s in ("ready", "needs_input", "preparation_error")
    }
    return cases, {
        "version": 1,
        "total": len(selected),
        **counts,
        "rows": [
            {
                "name": c["name"],
                "source": c["source"],
                "status": c["compiled"]["status"],
                "fingerprint": c["compiled"]["fingerprint"],
                "requirement_count": len(c["compiled"]["requirements"]),
                "questions": c["compiled"]["rubric_questions"]
                + [
                    x["question"]
                    for x in (c["compiled"]["scenario"] or {}).get(
                        "missing_information", []
                    )
                ],
                "error": c["compiled"]["error"],
            }
            for c in selected
        ],
    }
