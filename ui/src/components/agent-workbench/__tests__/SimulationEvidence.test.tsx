import { render, screen } from "@testing-library/react";
import React from "react";
import { describe, expect, it, vi } from "vitest";

import { SimulationEvidence } from "../SimulationEvidence";
import type { CaseResult } from "../types";

const base: CaseResult = {name: "sample", status: "inconclusive", mode: "dynamic", transcript: [], assertions: [], tool_calls: []};
vi.mock("../workbench.module.css", () => ({default: {}}));
describe("simulation validity", () => {
  it("distinguishes rejected customer replies and excluded cases from Agent failures", () => {
    render(<SimulationEvidence result={{...base, validity: {status: "invalid", stop_reason: "simulation_mismatch", checks: [{turn: 3, verdict: "misaligned", delivered: false, reason: "Question not asked", proposal: {text: "Done"}}]}}} />);
    expect(screen.getByText(/不计入 Agent 正常流程通过率/)).toBeTruthy();
    expect(screen.getByText(/未发送/)).toBeTruthy();
    expect(screen.getByText(/Question not asked/)).toBeTruthy();
  });
  it("labels authentic history and inferred starting state instead of showing them as Agent outputs", () => {
    render(<SimulationEvidence result={{...base, mode: "single_step", history: [{role: "assistant", text: "Which date?"}], validity: {status: "valid", scope: "single_reply", source_turn: 4, context_selection: {node_id: "a", reason: "Relevant question", history_quote: "Which date?", ambiguous: false}}}} />);
    expect(screen.getByText(/不代表完整流程通过/)).toBeTruthy();
    expect(screen.getByText("原坐席：Which date?")).toBeTruthy();
  });
  it("keeps synthetic assumptions visibly distinct from customer-confirmed facts", () => {
    render(<SimulationEvidence result={{...base, validity: {status: "valid", scenario: {fingerprint: "f", facts: [{id: "f1", statement: "has a browser", provenance: "test_assumption", quote: "", reason: "UI task needs a device"}], goals: [], limitations: []}}}} />);
    expect(screen.getByText("测试假设")).toBeTruthy();
    expect(screen.getByText(/不写入 Agent 记忆/)).toBeTruthy();
  });
});
