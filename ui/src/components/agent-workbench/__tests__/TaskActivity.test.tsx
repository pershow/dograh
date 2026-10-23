import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { TaskActivity } from "../TaskActivity";

vi.mock('../workbench.module.css', () => ({ default: {} }));

it("shows live work, folds on completion and preserves user expansion across updates", () => {
  const steps = [{ sequence: 1, text: "读取附件内容", status: "running" as const }];
  const { rerender } = render(<TaskActivity steps={steps} workflowId={7} running />);
  expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("list")).toBeTruthy();
  rerender(<TaskActivity steps={[{ ...steps[0], status: "done" }]} workflowId={7} />);
  expect(screen.queryByRole("list")).toBeNull();
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getByRole("list")).toBeTruthy();
  rerender(<TaskActivity steps={[{ ...steps[0], status: "done" }, {sequence: 2, text: "读取节点", status: "recorded"}]} workflowId={7} />);
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(screen.getByLabelText("已记录")).toBeTruthy();
});
