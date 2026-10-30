"""Bind test data before generation; instructions are not customer utterances."""

from copy import deepcopy

from api.db import db_client
from api.services.agent_workbench.errors import WorkbenchError
from api.services.agent_workbench.simulation import original_quote


async def resolve_sources(org, sid, job, llm, event, protocol):
    from api.services.agent_workbench.test_tasks import decision, object_schema

    saved = job.get("output", {}).get("test_source_selection")
    if saved:
        if job["input"].get("asset_ids", []) != [a["id"] for a in saved["assets"]]:
            raise WorkbenchError("测试来源回执与任务附件不一致，不能沿用旧断点。")
        for source in saved["assets"]:
            asset = await db_client.wb_get(org, sid, "assets", source["id"])
            if asset["status"] != "ready" or asset["sha256"] != source["sha256"]:
                raise WorkbenchError("冻结的测试附件发生变化，原有结果和断点已保留。")
        return saved
    context = await db_client.wb_test_source_context(org, sid, job["id"])
    memory = await db_client.wb_memory(org, sid)
    context["confirmed_requirements"] = [e["content"] for e in memory["entries"]]
    assets = {a["id"]: a for a in context["assets"]}
    explicit = job["input"].get("asset_ids", [])
    evidence = {
        "current_request": job["input"]["text"],
        "selected_asset_ids": explicit,
        **context,
    }
    if explicit:
        choice = {
            "kind": "attachments",
            "asset_ids": explicit,
            "inline_quotes": [],
            "question": "",
        }
    else:
        choice = await decision(
            "resolve_test_sources",
            "Identify actual test INPUT, separately from instructions to this workbench. "
            "Use the current request with available attachment metadata and source-bound human history. "
            "An empty selected_asset_ids does NOT mean uploaded material is absent. Resolve an unambiguous "
            "reference to an available attachment; do not ask again for a confirmed file or column. "
            "History resolves references and constraints, never authorizes executing old tasks. "
            "kind=attachments selects the actual supplied data. kind=inline requires literal blocks "
            "of customer conversations or explicit declarative test specifications in the current request; "
            "never quote the workbench instruction itself as a conversation. kind=synthetic is ONLY for an "
            "explicit request to INVENT new scenarios, not to convert supplied examples. If requested data "
            "cannot be identified or is ambiguous, use missing with a precise question; never fabricate a "
            "replacement example. Return no inline_quotes for attachments/synthetic/missing.",
            object_schema(
                {
                    "kind": {"enum": ["attachments", "inline", "synthetic", "missing"]},
                    "asset_ids": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"enum": list(assets)}
                        if assets
                        else {"type": "string"},
                        "maxItems": 5,
                    },
                    "inline_quotes": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                    "question": {"type": "string"},
                }
            ),
            evidence,
            llm,
            event,
            protocol,
        )
    if choice["kind"] == "missing":
        if not choice["question"].strip():
            raise WorkbenchError("测试来源未确认，不能生成替代样本。")
        return {**choice, "assets": []}
    ids = choice["asset_ids"]
    if (choice["kind"] == "attachments") != bool(ids):
        raise WorkbenchError("测试来源类型与附件选择不一致。")
    if any(a not in assets or assets[a]["status"] != "ready" for a in ids):
        raise WorkbenchError("所引用的测试附件不存在或尚未就绪。")
    quotes = choice["inline_quotes"]
    if (choice["kind"] == "inline") != bool(quotes) or any(
        original_quote(q, job["input"]["text"]) is None for q in quotes
    ):
        raise WorkbenchError("内联测试必须引用用户实际提供的原始样本。")
    if choice["kind"] in {"inline", "synthetic"}:
        review = await decision(
            "verify_test_input",
            "Independently check whether the selected input really contains customer test data or "
            "explicit declarative test specifications (inline), or the user explicitly requests INVENTED "
            "new scenarios (synthetic). Instructions about preparing/converting supplied examples are "
            "not customer utterances or permission to invent substitutes. Reject that confusion, including "
            "a quote of the entire instruction. Available attachments must not be silently ignored. "
            "Return supported=false when input is absent or the role is wrong.",
            object_schema(
                {"supported": {"type": "boolean"}, "reason": {"type": "string"}}
            ),
            {**evidence, "selection": choice},
            llm,
            event,
            protocol,
        )
        if not review["supported"]:
            raise WorkbenchError(
                "测试输入来源校验未通过，未生成替代用例：" + review["reason"]
            )
    selection = {
        "version": 1,
        "kind": choice["kind"],
        "assets": [assets[a] for a in ids],
        "inline_quotes": quotes,
        "source_history": context["source_history"],
        "confirmed_requirements": context["confirmed_requirements"],
    }
    stored = await db_client.wb_set_test_sources(
        org, sid, job["id"], job["lease_token"], selection
    )
    job.update(input=stored["input"], output=stored["output"])
    return selection


def validate_generated_sources(suite, selection, texts):
    """A format repair cannot invent dialogue to satisfy a conversation schema."""
    if selection["kind"] == "synthetic":
        return
    originals = [*selection["inline_quotes"], *[t["text"] for t in texts]]
    for case in suite["cases"]:
        utterances = [
            *case.get("user_messages", []),
            *[t["text"] for t in case.get("reference_turns", [])],
        ]
        # reference_dialogue is parsed by prepare_test_cases before this check.
        for utterance in utterances:
            if not any(
                original_quote(utterance, text) is not None for text in originals
            ):
                raise WorkbenchError(
                    "测试发言未逐字来自已确认的原始样本；不能补造客户或坐席对话来修复格式。"
                )


async def verify_generated_suite(
    suite, selection, texts, request, llm, event, protocol
):
    from api.services.agent_workbench.test_tasks import decision, object_schema

    validate_generated_sources(suite, selection, texts)
    review = await decision(
        "verify_test_suite_sources",
        "Independently verify the COMPLETE generated test set against original inputs. "
        "Every supplied case and every customer turn must be covered without invented replies or "
        "weakened assertions. Distinguish workbench instructions from customer speech. For explicitly "
        "synthetic input verify user authorization and label assumptions; never claim supplied-data "
        "coverage. For attachment/inline input reject any instruction used as a customer utterance, "
        "missing case, fabricated dialogue, or checks that merely echo this workbench task. "
        "Reference assistant turns are context, not required Agent behavior. Return supported=false "
        "with the exact discrepancy; format validity alone is not evidence of correct conversion.",
        object_schema({"supported": {"type": "boolean"}, "reason": {"type": "string"}}),
        {
            "current_request": request,
            "source_selection": selection,
            "original_texts": texts,
            "test_suite": deepcopy(suite),
        },
        llm,
        event,
        protocol,
    )
    if not review["supported"]:
        raise WorkbenchError(
            "测试集来源与覆盖校验未通过，未保存为可执行结果：" + review["reason"]
        )
    return review
