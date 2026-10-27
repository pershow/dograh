import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {beforeEach, expect, it, vi} from "vitest";

import {AgentMemory} from "../AgentMemory";

const mocks = vi.hoisted(() => ({get: vi.fn(), save: vi.fn(), revoke: vi.fn(), sources: vi.fn(), source: vi.fn(), auth: {user: {id: 1}, loading: false}}));
vi.mock("@/lib/auth", () => ({useAuth: () => mocks.auth}));
vi.mock("../workbench.module.css", () => ({default: {}}));
vi.mock("@/client/sdk.gen", () => ({
  workbenchMemoryApiV1AgentWorkbenchSessionsSessionIdMemoryGet: mocks.get,
  workbenchConfirmMemoryApiV1AgentWorkbenchSessionsSessionIdMemoryPost: mocks.save,
  workbenchRevokeMemoryApiV1AgentWorkbenchSessionsSessionIdMemoryMemoryIdRevokePost: mocks.revoke,
  workbenchMemorySourcesApiV1AgentWorkbenchSessionsSessionIdMemorySourcesGet: mocks.sources,
  workbenchMemorySourceApiV1AgentWorkbenchSessionsSessionIdMemorySourcesSourceIdGet: mocks.source,
}));
const response = (data: unknown) => ({data: {data}});
const entry = {id: "memory", kind: "constraint", content: "保留全部拒绝分支", status: "confirmed", source: {}, confirmed_by: 1, created_at: "2026-10-22T00:00:00Z"};
beforeEach(() => {
  vi.clearAllMocks(); mocks.auth.loading = false;
  mocks.get.mockResolvedValue(response({revision: "revision-1", entries: [entry]}));
  mocks.save.mockResolvedValue(response({})); mocks.revoke.mockResolvedValue(response({}));
  mocks.sources.mockResolvedValue(response({items: [{id: "source", source_type: "request", session_id: "first-session", title: "用户问题"}], total: 1, next_offset: null}));
  mocks.source.mockResolvedValue(response({id: "source", source_type: "request", session_id: "first-session", content: "原始问题：是否要收费？"}));
});

it("waits for auth and does not load the memory archive while closed", async () => {
  mocks.auth.loading = true;
  const view = render(<AgentMemory session="second" onChanged={vi.fn()}/>);
  expect(mocks.get).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Agent 长期记忆"}));
  expect(mocks.get).not.toHaveBeenCalled();
  mocks.auth.loading = false;
  view.rerender(<AgentMemory session="second" onChanged={vi.fn()}/>);
  await screen.findByRole("button", {name: "替换"});
  expect(mocks.get).toHaveBeenCalledWith({path: {session_id: "second"}, query: {include_history: false}});
});

it("replaces memory with its revision and shows conflicts without losing the edit", async () => {
  render(<AgentMemory session="second" onChanged={vi.fn()}/>);
  fireEvent.click(screen.getByRole("button", {name: "Agent 长期记忆"}));
  await screen.findByRole("button", {name: "替换"});
  fireEvent.click(screen.getByRole("button", {name: "替换"}));
  fireEvent.change(screen.getByRole("textbox", {name: "长期要求原文"}), {target: {value: "保留拒绝和人工分支"}});
  mocks.save.mockResolvedValueOnce({error: {detail: "长期记忆已更新，请刷新后确认"}});
  fireEvent.click(screen.getByRole("button", {name: "确认并替换"}));
  expect((await screen.findByRole("alert")).textContent).toContain("长期记忆已更新");
  expect((screen.getByRole("textbox", {name: "长期要求原文"}) as HTMLTextAreaElement).value).toBe("保留拒绝和人工分支");
  expect(mocks.save.mock.calls[0][0].body).toMatchObject({expected_revision: "revision-1", replaces_id: "memory", content: "保留拒绝和人工分支"});
});

it("requires an explicit human confirmation after reading a historical question", async () => {
  const changed = vi.fn();
  render(<AgentMemory session="second" onChanged={changed}/>);
  fireEvent.click(screen.getByRole("button", {name: "Agent 长期记忆"}));
  await screen.findByRole("button", {name: "替换"});
  fireEvent.click(screen.getByText("查找所有会话的用户原文与资料"));
  fireEvent.click(screen.getByRole("button", {name: "查找"}));
  await screen.findByText("用户问题");
  fireEvent.click(screen.getByRole("button", {name: "查看原文并确认要求"}));
  expect(((await screen.findByRole("textbox", {name: "长期要求原文"})) as HTMLTextAreaElement).value).toBe("原始问题：是否要收费？");
  expect(mocks.save).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", {name: "长期要求原文"}), {target: {value: "费用未知，不得编造价格"}});
  fireEvent.click(screen.getByRole("button", {name: "确认并保存"}));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(mocks.save.mock.calls[0][0].body).toMatchObject({source_id: "source", source_quote: "原始问题：是否要收费？", content: "费用未知，不得编造价格"});
});
