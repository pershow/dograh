import {fireEvent, render, screen} from "@testing-library/react";
import {beforeEach, expect, it, vi} from "vitest";

import {RequirementCoverage,TaskBasis} from "../TaskBasis";

const mocks = vi.hoisted(() => ({read: vi.fn(), auth: {user: {id: 1}, loading: false}}));
vi.mock("@/lib/auth", () => ({useAuth: () => mocks.auth}));
vi.mock("@/client/sdk.gen", () => ({workbenchTaskBasisApiV1AgentWorkbenchSessionsSessionIdJobsJobIdBasisGet: mocks.read}));
const basis = {fingerprint: "v1", sources: [{id: "source", title: "业务说明", kind: "material", characters: 30000}]};
beforeEach(() => {vi.clearAllMocks(); mocks.auth.loading = false;});

it("loads original text on demand and appends the next page", async () => {
  mocks.read.mockResolvedValueOnce({data: {data: {id: "source", text: "原文前半", offset: 0, next_offset: 12000}}});
  mocks.read.mockResolvedValueOnce({data: {data: {id: "source", text: "原文后半", offset: 12000, next_offset: null}}});
  render(<TaskBasis session="s" jobId="j" basis={basis} />);
  expect(mocks.read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("查看原文"));
  await screen.findByText("原文前半");
  fireEvent.click(screen.getByText("继续加载原文"));
  await screen.findByText("原文前半原文后半");
  expect(mocks.read.mock.calls[1][0]).toEqual({path: {session_id: "s", job_id: "j"}, query: {source_id: "source", offset: 12000}});
});

it("guards auth and renders API errors as text", async () => {
  mocks.auth.loading = true;
  const view = render(<TaskBasis session="s" jobId="j" basis={basis} />);
  fireEvent.click(screen.getByText("查看原文"));
  expect(mocks.read).not.toHaveBeenCalled();
  mocks.auth.loading = false;
  mocks.read.mockResolvedValue({error: {detail: "该资料不可访问"}});
  view.rerender(<TaskBasis session="s" jobId="j" basis={basis} />);
  fireEvent.click(screen.getByText("查看原文"));
  expect((await screen.findByRole("alert")).textContent).toContain("该资料不可访问");
});

it("shows whole-agent scope and missing coverage without presenting it as a pass", () => {
  render(<RequirementCoverage report={{requirements: [{requirement: "整体自然表达", status: "gap", scope: "agent", source_quote: "整个 Agent 都要自然", source_ids: ["source"], nodes: ["引导"], cases: [], evidence: "只有单节点修改", correction: "检查全部适用节点"}]}} />);
  expect(screen.getByText(/0\/1 项已验证/)).toBeTruthy();
  expect(screen.getByText("范围：整个 Agent")).toBeTruthy();
  expect(screen.getByText(/测试：尚未覆盖/)).toBeTruthy();
});
