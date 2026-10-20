import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowEditorHeader } from "@/app/workflow/[workflowId]/components/WorkflowEditorHeader";
import { WorkflowTable } from "@/components/workflow/WorkflowTable";

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  capabilities: vi.fn(),
  confirmNavigate: vi.fn(),
  auth: { loading: false, user: { id: "1" } },
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/lib/auth", () => ({ useAuth: () => mocks.auth }));
vi.mock("@/client/sdk.gen", () => ({
  workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet: mocks.capabilities,
  duplicateWorkflowEndpointApiV1WorkflowWorkflowIdDuplicatePost: vi.fn(),
  moveWorkflowToFolderApiV1WorkflowWorkflowIdFolderPut: vi.fn(),
  updateWorkflowStatusApiV1WorkflowWorkflowIdStatusPut: vi.fn(),
}));
vi.mock("@/hooks/useOrganizationTimezone", () => ({ useOrganizationTimezone: () => "UTC" }));
vi.mock("@/components/ui/sidebar", () => ({ useSidebar: () => ({ toggleSidebar: vi.fn() }) }));
vi.mock("@/context/UnsavedChangesContext", () => ({
  useUnsavedChangesContext: () => ({ confirmNavigate: mocks.confirmNavigate }),
}));
vi.mock("@/components/layout/GitHubStarBadge", () => ({ GitHubStarBadge: () => null }));
vi.mock("@/app/workflow/[workflowId]/components/PublishWorkflowPopover", () => ({
  PublishWorkflowPopover: () => null,
}));

const workflows = [{ id: 3, name: "正在构建的 Agent", status: "active", created_at: "2026-10-20T00:00:00Z" }];
beforeEach(() => {
  vi.clearAllMocks();
  mocks.auth.loading = false;
  mocks.capabilities.mockResolvedValue({ data: { data: { enabled: true } } });
  mocks.confirmNavigate.mockImplementation(navigate => navigate());
});

describe("return to the Agent's workbench", () => {
  it("opens the existing workflow from the list without bubbling to Edit", async () => {
    render(<WorkflowTable workflows={workflows} showArchived={false} />);
    fireEvent.click(await screen.findByRole("button", { name: "在工作台中继续 正在构建的 Agent" }));
    expect(mocks.push.mock.calls).toEqual([["/workflow/3?workbench=1"]]);
    expect(mocks.capabilities).toHaveBeenCalledOnce();
  });

  it("waits for auth and respects deployments without the workbench", async () => {
    mocks.auth.loading = true;
    const view = render(<WorkflowTable workflows={workflows} showArchived={false} />);
    expect(mocks.capabilities).not.toHaveBeenCalled();
    mocks.capabilities.mockResolvedValue({ data: { data: { enabled: false } } });
    mocks.auth.loading = false;
    view.rerender(<WorkflowTable workflows={workflows} showArchived={false} />);
    await waitFor(() => expect(mocks.capabilities).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: /在工作台中继续/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(mocks.push).toHaveBeenCalledWith("/workflow/3");
  });

  it("keeps archived agents restore-only", async () => {
    render(<WorkflowTable workflows={workflows} showArchived />);
    await waitFor(() => expect(mocks.capabilities).toHaveBeenCalledOnce());
    expect(screen.queryByRole("button", { name: /在工作台中继续/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Restore" })).toBeTruthy();
  });

  it("opens the panel in place despite graph errors, preserving unsaved edits", async () => {
    const openWorkbench = vi.fn();
    mocks.confirmNavigate.mockImplementation(() => {});
    render(<WorkflowEditorHeader
      workflowId={3}
      workflowName="正在构建的 Agent"
      isDirty
      workflowValidationErrors={[{ kind: "node", id: "1", field: null, message: "Missing end node" }]}
      rfInstance={{ current: null }}
      saveWorkflow={vi.fn()}
      user={{ id: "1" }}
      onPhoneCallClick={vi.fn()}
      onTestAgentClick={vi.fn()}
      onHistoryClick={vi.fn()}
      onWorkbenchClick={openWorkbench}
      isViewingHistoricalVersion={false}
      onBackToDraft={vi.fn()}
      hasDraft
      onPublished={vi.fn()}
      renameWorkflow={vi.fn()}
    />);
    fireEvent.click(await screen.findByRole("button", { name: "工作台" }));
    expect(openWorkbench).toHaveBeenCalledOnce();
    expect(mocks.confirmNavigate).not.toHaveBeenCalled();
    expect(mocks.push).not.toHaveBeenCalled();
  });
});
