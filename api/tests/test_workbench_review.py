from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.schemas.agent_workbench import SimulationCase
from api.services.agent_workbench import review


async def test_incomplete_review_is_separate_from_semantic_uncertainty():
    counts = review.review_counts(
        [
            {
                "reference_review": {
                    "status": "inconclusive",
                    "reason": "connection interrupted",
                }
            },
            {
                "reference_review": {
                    "status": "inconclusive",
                    "checks": [{"category": "insufficient_evidence"}],
                    "categories": ["insufficient_evidence"],
                }
            },
            {"reference_review": None},
        ]
    )
    assert counts["review_pending"] == counts["insufficient_evidence"] == 1


from api.services.agent_workbench.errors import WorkbenchError

pytestmark = pytest.mark.asyncio


def evidence():
    return review.build_evidence(
        SimulationCase(
            name="fixture",
            user_messages=["done"],
            reference_dialogue="客服：请上传文件\n客户：已上传",
        ),
        [
            {"role": "assistant", "turn": 0, "text": "请先选择文件类型。"},
            {"role": "user", "turn": 1, "text": "已上传"},
            {"role": "assistant", "turn": 1, "text": "已为您上传；所有方案都是免费。"},
        ],
        [],
        {
            "snapshot": {
                "workflow_json": {
                    "nodes": [
                        {
                            "id": "global",
                            "data": {
                                "prompt": "未选择文件类型前，不能上传。收费方案每月20元。"
                            },
                        }
                    ],
                    "edges": [],
                },
                "workflow_configurations": {},
                "template_context_variables": {},
            }
        },
        "只测试，不修改 Agent。",
        {
            "fingerprint": "basis-fixture",
            "memory_revision": "m",
            "target_fingerprint": "c",
            "warnings": [],
            "sources": [
                {
                    "id": "original",
                    "kind": "material",
                    "title": "业务说明",
                    "text": "未选择文件类型前，不能上传。收费方案每月20元。",
                    "content_hash": "original-hash",
                }
            ],
        },
    )


def check(category="agent_issue"):
    policy = next(r for r in evidence()["records"] if r["source"] == "basis")
    return {
        "criterion": "真实价格",
        "category": category,
        "reason": "实际承诺免费，与收费规则冲突。",
        "requirement_type": "fact",
        "citations": [
            {"id": "actual:2", "quote": "所有方案都是免费。"},
            {"id": policy["id"], "quote": "收费方案每月20元。"},
        ],
    }


def verified(checks):
    return {
        "reviews": [
            {"check_index": i, "verdict": "supported", "reason": "依据有效", "check": c}
            for i, c in enumerate(checks)
        ],
        "additional_checks": [],
    }


async def test_agent_issue_requires_actual_speech_and_applicable_rule_not_reference():
    records = {r["id"]: r for r in evidence()["records"]}
    c = check()
    review.validate_check(c, records)
    c["citations"][1] = {"id": "reference:0", "quote": "客服：请上传文件"}
    with pytest.raises(WorkbenchError, match="参考示例不是强制规则"):
        review.validate_check(c, records)
    c["citations"] = [check()["citations"][1]]
    with pytest.raises(WorkbenchError, match="实际坐席回复"):
        review.validate_check(c, records)


async def test_source_ids_cannot_be_invented():
    c = check()
    c["citations"][0]["id"] = "actual:999"
    with pytest.raises(WorkbenchError, match="不能虚构"):
        review.validate_check(c, {r["id"]: r for r in evidence()["records"]})


async def test_script_mismatch_and_true_issue_coexist_without_erasing_issue():
    mismatch = {
        "criterion": "阶段对齐",
        "category": "script_mismatch",
        "reason": "脚本回答上传，实际尚在选类型。",
        "citations": [
            {"id": "actual:0", "quote": "请先选择文件类型。"},
            {"id": "reference:0", "quote": "客服：请上传文件"},
        ],
    }
    checks = [mismatch, check()]
    result = review.finalize({"checks": checks}, verified(checks), evidence())
    assert result["status"] == "failed"
    assert set(result["categories"]) == {"script_mismatch", "agent_issue"}
    mismatch_only = review.finalize(
        {"checks": [mismatch]}, verified([mismatch]), evidence()
    )
    assert mismatch_only["status"] == "inconclusive"


async def test_corrected_absence_claim_keeps_initial_review_and_real_counterevidence():
    original = check()
    original["criterion"] = "未提到方案价格"
    original["reason"] = "完全没有价格信息。"
    corrected = check()
    corrected["reason"] = "实际提到免费；问题是价格错误，不是没提价格。"
    verification = verified([corrected])
    verification["reviews"][0].update(
        verdict="corrected", reason="原来的缺失判断被实际免费承诺推翻。"
    )
    result = review.finalize({"checks": [original]}, verification, evidence())
    assert result["review_corrected"] is True
    assert result["draft_review"]["checks"][0]["reason"] == original["reason"]
    assert result["checks"][0]["actual_quote"] == "所有方案都是免费。"
    assert review.review_counts([{"reference_review": result}])["review_corrected"] == 1


async def test_verification_must_cover_all_findings_and_uncertainty_cannot_fail():
    c = check()
    v = verified([c])
    v["reviews"] *= 2
    with pytest.raises(WorkbenchError, match="不能遗漏或重复"):
        review.finalize({"checks": [c]}, v, evidence())
    v = verified([c])
    v["reviews"][0]["verdict"] = "unresolved"
    with pytest.raises(WorkbenchError, match="证据不足"):
        review.finalize({"checks": [c]}, v, evidence())
    v["reviews"][0]["check"] = {**c, "category": "insufficient_evidence"}
    assert review.finalize({"checks": [c]}, v, evidence())["status"] == "inconclusive"


async def test_review_runs_two_fresh_decisions_with_full_policy_and_transcript(
    monkeypatch,
):
    from api.services.agent_workbench import test_tasks

    e = evidence()
    c = check()
    mock = AsyncMock(side_effect=[{"checks": [c]}, verified([c])])
    monkeypatch.setattr(test_tasks, "decision", mock)
    monkeypatch.setattr(review, "build_evidence", lambda *args: e)
    builder = SimpleNamespace(model="builder")
    verifier = SimpleNamespace(model="verifier")
    result = await review.review_replay(
        None, None, None, None, builder, AsyncMock(), "chat", verifier_llm=verifier
    )
    assert result["reviewer_models"] == ["builder", "verifier"]
    assert mock.await_count == 2
    for call in mock.call_args_list:
        assert call.args[3]["records"] == e["records"]
    assert mock.call_args_list[1].args[4] is verifier
