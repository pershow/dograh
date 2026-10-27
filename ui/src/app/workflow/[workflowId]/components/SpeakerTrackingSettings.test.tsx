import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { UnsavedChangesProvider } from "@/context/UnsavedChangesContext";
import { resolveWorkflowConfigurations } from "@/types/workflow-configurations";

import { WorkflowSettingsSections } from "../settings/WorkflowSettingsSections";
import { ConfigurationsDialog } from "./ConfigurationsDialog";

vi.mock("@/context/OrgConfigContext", () => ({ useOrgConfig: () => ({ externalPbxIntegrationsEnabled: false }) }));
vi.mock("@/components/model-connections/WorkflowModelConfiguration", () => ({ WorkflowModelConfiguration: () => null }));
vi.mock("@/components/LLMConfigSelector", () => ({ LLMConfigSelector: () => null }));
vi.mock("./EmbedDialog", () => ({ EmbedDialog: () => null }));

describe("speaker tracking configuration", () => {
    it("defaults off, respects explicit false, and resolves older null values", () => {
        expect(resolveWorkflowConfigurations().speaker_tracking_enabled).toBe(false);
        expect(resolveWorkflowConfigurations({ speaker_tracking_enabled: false }, { speaker_tracking_enabled: true }).speaker_tracking_enabled).toBe(false);
        expect(resolveWorkflowConfigurations(JSON.parse('{"speaker_tracking_enabled":null}')).speaker_tracking_enabled).toBe(false);
    });

    it("saves the toggle from the dialog without changing interruption settings", async () => {
        const save = vi.fn(async () => undefined);
        const configuration = resolveWorkflowConfigurations({ turn_start_min_words: 4 });
        render(<ConfigurationsDialog open onOpenChange={vi.fn()} workflowConfigurations={configuration} workflowName="Calls" onSave={save} />);
        const toggle = screen.getByRole("switch", { name: "Speaker Tracking (Preview)" });
        expect(toggle.getAttribute("aria-checked")).toBe("false");
        fireEvent.click(toggle);
        fireEvent.click(screen.getByRole("button", { name: /save/i }));
        await waitFor(() => expect(save).toHaveBeenCalledWith({ ...configuration, speaker_tracking_enabled: true }, "Calls"));
    });

    it("marks General settings dirty, saves, and can disable the preview", async () => {
        vi.stubGlobal("IntersectionObserver", class {
            observe() {}
            disconnect() {}
        });
        const save = vi.fn(async () => undefined);
        const configuration = resolveWorkflowConfigurations({ speaker_tracking_enabled: true });
        render(<UnsavedChangesProvider><WorkflowSettingsSections
            workflowId={1} workflowName="Calls" workflowConfigurations={configuration}
            defaultCallDispositions={[]} defaultAnswerClassifierPrompt=""
            textChatInactivityTimeoutConstraints={null} widgetTextDefaults={null}
            templateContextVariables={{}} dictionary="" saveWorkflowConfigurations={save}
            saveTemplateContextVariables={vi.fn()} saveDictionary={vi.fn()}
            hide={["models", "variables", "dictionary", "voicemail", "deployment", "report", "identity"]}
        /></UnsavedChangesProvider>);
        const toggle = screen.getByRole("switch", { name: "Speaker Tracking (Preview)" });
        expect(toggle.getAttribute("aria-checked")).toBe("true");
        fireEvent.click(toggle);
        expect(screen.getAllByText("Unsaved changes").length).toBeGreaterThan(0);
        fireEvent.click(screen.getByRole("button", { name: "Save General Settings" }));
        await waitFor(() => expect(save).toHaveBeenCalledWith(expect.objectContaining({ speaker_tracking_enabled: false }), "Calls"));
        vi.unstubAllGlobals();
    });
});
