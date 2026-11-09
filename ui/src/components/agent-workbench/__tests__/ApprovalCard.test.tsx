import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { ApprovalCard, type WorkbenchApproval } from "../ApprovalCard";

const approval: WorkbenchApproval = {
  id: "approval-1", revision: "a".repeat(64), kind: "requirements", status: "pending",
  proposal: { brief: { goal: "预约回访", direction: "outbound", information_to_collect: ["日期"],
    business_rules: ["拒绝时结束通话"], success_criteria: ["确认预约时间"], assumptions: [] } },
};

it("shows the exact requirements and approves only after a user click", async () => {
  const onDecision = vi.fn().mockResolvedValue(undefined);
  render(<ApprovalCard approval={approval} disabled={false} onDecision={onDecision} />);
  expect(screen.getByText("拒绝时结束通话")).toBeTruthy();
  expect(onDecision).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "确认并构建" }));
  await waitFor(() => expect(onDecision).toHaveBeenCalledWith({ approval_id: approval.id,
    revision: approval.revision, decision: "approve", feedback: "" }));
});

it("requires feedback to revise and preserves the proposal identity", async () => {
  const onDecision = vi.fn().mockResolvedValue(undefined);
  render(<ApprovalCard approval={approval} disabled={false} onDecision={onDecision} />);
  fireEvent.click(screen.getByRole("button", { name: "提出修改" }));
  expect(screen.getByRole("button", { name: "提交修改意见" }).hasAttribute("disabled")).toBe(true);
  fireEvent.change(screen.getByLabelText("修改意见"), { target: { value: " 改成呼入 " } });
  fireEvent.click(screen.getByRole("button", { name: "提交修改意见" }));
  await waitFor(() => expect(onDecision).toHaveBeenCalledWith({ approval_id: approval.id,
    revision: approval.revision, decision: "revise", feedback: "改成呼入" }));
});

it("shows shared impact, old/new configuration and supports denial", async () => {
  const onDecision = vi.fn().mockResolvedValue(undefined);
  const tool: WorkbenchApproval = { ...approval, kind: "tool_change", proposal: {
    operation: "update", reason: "更换预约地址", before: {name: "旧名称"}, request: {name: "新名称"},
  } };
  render(<ApprovalCard approval={tool} disabled={false} onDecision={onDecision} />);
  expect(screen.getByText("此变更会影响所有使用该工具的 Agent。")).toBeTruthy();
  expect(screen.getByText(/旧名称/)).toBeTruthy();
  expect(screen.getByText(/新名称/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "拒绝此变更" }));
  await waitFor(() => expect(onDecision).toHaveBeenCalledWith(expect.objectContaining({decision: "deny"})));
});

it("disables an obsolete or already decided proposal", () => {
  const onDecision = vi.fn();
  render(<ApprovalCard approval={{...approval, status: "approved"}} disabled={false} onDecision={onDecision} />);
  expect(screen.getByRole("button", { name: "确认并构建" }).hasAttribute("disabled")).toBe(true);
});
