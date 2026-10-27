import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";

import { selectOption, selectOptions } from "@/components/model-connections/test-helpers";

import { BuildModelSettings } from "../BuildModelSettings";

const mocks = vi.hoisted(() => ({
  get: vi.fn(), save: vi.fn(), catalog: vi.fn(), connections: vi.fn(),
  auth: { user: { id: "1" }, loading: false },
  org: { orgContext: { organization_id: 42 }, loading: false },
}));
vi.mock("@/lib/auth", () => ({ useAuth: () => mocks.auth }));
vi.mock("@/context/OrgConfigContext", () => ({ useOrgConfig: () => mocks.org }));
vi.mock("@/client/sdk.gen", () => ({
  getWorkbenchBuildModel: mocks.get, updateWorkbenchBuildModel: mocks.save,
  getModelConnectionCatalog: mocks.catalog, listProviderConnections: mocks.connections,
}));
const selection = { provider_connection_uuid: "openai", settings: { model: "builder" } };
const response = (llm: typeof selection | null) => ({ data: { llm, supported_providers: ["openai", "hopper"] } });
const provider = { title: "OpenAI", credential_fields: {}, credential_required: ["api_key"], connection_fields: {}, settings_schema: { properties: {
  model: { type: "string", title: "Model", default: "builder", examples: ["builder", "builder-large"], allow_custom_input: true },
} } };
beforeEach(() => {
  vi.clearAllMocks();
  mocks.auth.loading = false;
  mocks.org.loading = false;
  mocks.org.orgContext.organization_id = 42;
  mocks.get.mockResolvedValue(response(null));
  mocks.save.mockImplementation(async ({body}) => response(body.llm));
  mocks.catalog.mockResolvedValue({data: { services: { llm: { openai: provider, hopper: {...provider, title: "Hopper"}, google: provider } } }});
  mocks.connections.mockResolvedValue({data: [
    {uuid: "openai", name: "主连接", provider: "openai", is_active: true, configured_credentials: ["api_key"], connection_settings: {}},
    {uuid: "hopper", name: "备用连接", provider: "hopper", is_active: true, configured_credentials: ["api_key"], connection_settings: {}},
    {uuid: "google", name: "不兼容", provider: "google", is_active: true, configured_credentials: ["api_key"], connection_settings: {}},
    {uuid: "archived", name: "已归档", provider: "openai", is_active: false, configured_credentials: ["api_key"], connection_settings: {}},
    {uuid: "no-key", name: "未填密钥", provider: "openai", is_active: true, configured_credentials: [], connection_settings: {}},
  ]});
});

it("waits for authentication and organization setup, and offers only supported active connections", async () => {
  mocks.auth.loading = true;
  const view = render(<BuildModelSettings disabled={false} onSaved={vi.fn()} />);
  expect(mocks.get).not.toHaveBeenCalled();
  mocks.auth.loading = false;
  mocks.org.loading = true;
  view.rerender(<BuildModelSettings disabled={false} onSaved={vi.fn()} />);
  expect(mocks.get).not.toHaveBeenCalled();
  mocks.org.loading = false;
  view.rerender(<BuildModelSettings disabled={false} onSaved={vi.fn()} />);
  await screen.findByLabelText("构建 Provider");
  expect(selectOptions("构建 Provider")).toEqual(["跟随 Agent 模型配置", "主连接 · OpenAI", "备用连接 · Hopper"]);
});

it("saves the dedicated provider and custom model, and restores the selection when reopened", async () => {
  const onSaved = vi.fn();
  const view = render(<BuildModelSettings disabled={false} onSaved={onSaved} />);
  await screen.findByLabelText("构建 Provider");
  selectOption("构建 Provider", "主连接 · OpenAI");
  selectOption("构建模型", "builder-large");
  fireEvent.click(screen.getByRole("checkbox", {name: "Enter Custom Value"}));
  fireEvent.change(screen.getByRole("textbox", {name: "构建模型"}), {target: {value: "qwen-custom"}});
  expect(mocks.save).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "保存构建模型"}));
  await screen.findByText("已保存，下次构建任务生效");
  expect(mocks.save).toHaveBeenCalledWith({body: {llm: {...selection, settings: {model: "qwen-custom"}}}});
  expect(onSaved).toHaveBeenCalledOnce();
  view.unmount();
  mocks.get.mockResolvedValue(response({...selection, settings: {model: "qwen-custom"}}));
  render(<BuildModelSettings disabled={false} onSaved={onSaved} />);
  expect(((await screen.findByRole("textbox", {name: "构建模型"})) as HTMLInputElement).value).toBe("qwen-custom");
  expect(screen.getByLabelText("构建 Provider").textContent).toContain("主连接");
});

it("can clear a stale selection and resume following the Agent without changing its configuration", async () => {
  mocks.get.mockResolvedValue(response({...selection, provider_connection_uuid: "archived"}));
  render(<BuildModelSettings disabled={false} onSaved={vi.fn()} />);
  await screen.findByText("连接已归档、配置不完整或不支持构建助手，请选择其他连接。");
  selectOption("构建 Provider", "跟随 Agent 模型配置");
  fireEvent.click(screen.getByRole("button", {name: "保存构建模型"}));
  await screen.findByText("已保存，下次构建任务生效");
  expect(mocks.save).toHaveBeenCalledWith({body: {llm: null}});
});

it("preserves unsaved edits on an API error and rejects an empty custom model", async () => {
  mocks.get.mockResolvedValue(response(selection));
  mocks.save.mockResolvedValueOnce({error: {detail: [{loc: ["body", "llm"], msg: "连接不可用"}]}});
  const onSaved = vi.fn();
  render(<BuildModelSettings disabled={false} onSaved={onSaved} />);
  await screen.findByLabelText("构建模型");
  fireEvent.click(screen.getByRole("checkbox", {name: "Enter Custom Value"}));
  fireEvent.change(screen.getByRole("textbox", {name: "构建模型"}), {target: {value: "   "}});
  expect((screen.getByRole("button", {name: "保存构建模型"}) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByRole("textbox", {name: "构建模型"}), {target: {value: "custom-model"}});
  fireEvent.click(screen.getByRole("button", {name: "保存构建模型"}));
  expect((await screen.findByRole("alert")).textContent).toContain("连接不可用");
  expect((screen.getByRole("textbox", {name: "构建模型"}) as HTMLInputElement).value).toBe("custom-model");
  expect(onSaved).not.toHaveBeenCalled();
});

it("disables changes while the workbench is busy", async () => {
  mocks.get.mockResolvedValue(response(selection));
  render(<BuildModelSettings disabled onSaved={vi.fn()} />);
  expect(((await screen.findByLabelText("构建 Provider")) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", {name: "保存构建模型"}) as HTMLButtonElement).disabled).toBe(true);
});
