import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReviewCounts, ReviewFindings } from "../ReviewFindings";

const check = { criterion: "价格说明", category: "agent_issue", reason: "实际回答与规则冲突", citations: [{id:"actual:2", source:"actual", role:"assistant", turn:1, quote:"所有方案免费"}, {id:"policy:1",source:"policy",path:"/nodes/1/data/prompt",quote:"方案按月收费"}] };
describe("evidence review", () => {
  it("distinguishes source-grounded business findings from configuration and suggestions", () => {
    render(<ReviewFindings review={{version: 3, status: "inconclusive", reason: "done", warnings: ["缺少一项原始资料"], checks: [{...check, category: "configuration_issue", severity: "unknown", layer: "runtime", applicability: "该阶段已经到达", citations: [{id: "basis:original", source: "basis", title: "用户业务说明", quote: "先确认授权", offset: 12000}]}]}} />);
    expect(screen.getByText(/原始依据 · 用户业务说明 · 位置 12000/)).toBeTruthy();
    expect(screen.getByText(/影响：待核实 · 归因：运行执行/)).toBeTruthy();
    expect(screen.getByText("缺少一项原始资料")).toBeTruthy();
  });
  it("shows both real issues and script mismatches without conflating them", () => {
    render(<ReviewCounts summary={{agent_issue:1,script_mismatch:2,review_corrected:1,insufficient_evidence:0}} />);
    expect(screen.getByText(/Agent 问题 1 条 · 脚本不匹配 2 条/)).toBeTruthy();
    expect(screen.getByText(/同一条对话可包含多类发现/)).toBeTruthy();
  });
  it("preserves original judgment, correction and exact source quotes", () => {
    render(<ReviewFindings review={{version:2,status:"failed",reason:"复核完成",checks:[check],draft_review:{checks:[{...check,reason:"从未提及价格"}]},verification:{reviews:[{check_index:0,verdict:"corrected",reason:"实际回复中有价格承诺",check}]}}} />);
    expect(screen.getByText("所有方案免费")).toBeTruthy();
    expect(screen.getByText("方案按月收费")).toBeTruthy();
    expect(screen.getByText("初评：从未提及价格")).toBeTruthy();
    expect(screen.getByText("已纠正初评：实际回复中有价格承诺")).toBeTruthy();
  });
  it("does not relabel legacy results as verified", () => {
    const {container}=render(<ReviewFindings review={{status:"failed",reason:"old"}} />);
    expect(container.innerHTML).toBe("");
  });
  it("matches frozen-criterion corrections by ID and retains the original judgment", () => {
    render(<ReviewFindings review={{
      version: 4, status: "failed", reason: "逐项复核完成", checks: [],
      draft_review: {checks: [
        {...check, criterion_id: "unchanged", reason: "无关项已满足"},
        {...check, criterion_id: "price", reason: "初评遗漏价格冲突"},
      ]},
      criterion_verification: {reviews: [
        {criterion_id: "price", verdict: "corrected", reason: "原始依据要求收费", check: {reason: "实际承诺免费，违反原始价格要求"}},
        {criterion_id: "unchanged", verdict: "supported", reason: "已有充分证据", check: null},
      ]},
    }} />);
    expect(screen.getByText("初评：初评遗漏价格冲突")).toBeTruthy();
    expect(screen.getByText("复核：实际承诺免费，违反原始价格要求")).toBeTruthy();
    expect(screen.queryByText("初评：无关项已满足")).toBeNull();
  });
});
