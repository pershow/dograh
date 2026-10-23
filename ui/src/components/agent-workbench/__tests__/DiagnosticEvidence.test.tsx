import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { DiagnosticEvidence } from "../DiagnosticEvidence";

vi.mock("../workbench.module.css", () => ({ default: {} }));

it("distinguishes reconstructed evidence, predictions, failed calls and actual observations", () => {
  render(<DiagnosticEvidence data={{run_id: 42, evidence_id: "probe:1", provenance: "reconstructed", hypothesis: "Wrong context", prediction: "Expected difference", agent_modified: false,
    results: [{arm: "baseline", repeat: 1, text: "Observed reply"}, {arm: "variant", repeat: 1, error: "TimeoutError"}, {arm: "variant", repeat: 2, tool_calls: [{name: "proceed", arguments: "{}"}]}],
    limitations: ["No historical capture"], baseline: {messages: []}, next_offset: 100, characters: 500}} />);
  fireEvent.click(screen.getByText("查看排查证据 · 运行 #42"));
  expect(screen.getByText(/基于历史证据重建/)).toBeTruthy();
  expect(screen.getByText(/Expected difference/)).toBeTruthy();
  expect(screen.getByText("Observed reply")).toBeTruthy();
  expect(screen.getByText("调用失败：TimeoutError")).toBeTruthy();
  expect(screen.getByText(/工具：proceed.*未执行/)).toBeTruthy();
  expect(screen.getByText(/后续内容尚未包含在本页/)).toBeTruthy();
  expect(screen.getByText("原始条件：提示词、历史与工具")).toBeTruthy();
});

it("opens each conclusion's cited original evidence and preserves an incomplete-read warning", () => {
  render(<DiagnosticEvidence data={{findings: [{claim: "Observed routing", status: "supported", evidence_ids: ["run:42:execution"], reason: "Recorded trace"}], reads: [{evidence_id: "run:42:execution", run_id: 42, complete: false, content: {events: [{type: "node_transition"}]}}]}} />);
  fireEvent.click(screen.getByText("查看排查证据"));
  fireEvent.click(screen.getByText("查看引用：run:42:execution"));
  expect(screen.getByText("分析器尚未读完此证据的所有分页。")).toBeTruthy();
  fireEvent.click(screen.getByText("本次读取的原文"));
  expect(screen.getByText("查看引用：run:42:execution").parentElement?.querySelector("pre")?.textContent).toContain("node_transition");
});
