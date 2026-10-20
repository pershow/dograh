import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { UnsavedChangesProvider } from "@/context/UnsavedChangesContext";

import { TemplateVariablesSection } from "./WorkflowSettingsSections";

vi.mock("@/components/model-connections/WorkflowModelConfiguration", () => ({ WorkflowModelConfiguration: () => null }));
vi.mock("@/components/LLMConfigSelector", () => ({ LLMConfigSelector: () => null }));
vi.mock("../components/EmbedDialog", () => ({ EmbedDialog: () => null }));

it("makes unregistered prompt variables editable without inventing values or saving on load", async () => {
    const save = vi.fn(async () => undefined);
    render(<UnsavedChangesProvider><TemplateVariablesSection
        templateContextVariables={{ company: "Original", retained: "Keep me" }}
        referencedNames={["company", "customer"]} onSave={save}
    /></UnsavedChangesProvider>);
    expect((screen.getByLabelText("customer") as HTMLInputElement).value).toBe("");
    expect(screen.queryByText("Unsaved changes")).toBeNull();
    expect(save).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("company"), { target: { value: "Provided company" } });
    fireEvent.click(screen.getByRole("button", { name: "Save Variables" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({
        company: "Provided company", customer: "", retained: "Keep me",
    }));
});

it("can register unknown variables as empty defaults and still remove unused defaults", async () => {
    const save = vi.fn(async () => undefined);
    render(<UnsavedChangesProvider><TemplateVariablesSection
        templateContextVariables={{ unused: "Old" }} referencedNames={["customer"]} onSave={save}
    /></UnsavedChangesProvider>);
    expect(screen.queryByRole("button", { name: "Remove variable customer" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove variable unused" }));
    fireEvent.click(screen.getByRole("button", { name: "Save Variables" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith({ customer: "" }));
});
