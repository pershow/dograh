import { describe, expect, it } from "vitest";

import { conversationRows } from "../activity";
import type { Job, WorkbenchEvent } from "../types";

const job: Job = { id: "job", kind: "build", status: "running", input: {}, output: {} };
const event = (sequence: number, type: string, payload: Record<string, unknown> = {}): WorkbenchEvent =>
  ({ sequence, type, payload, created_at: "" });
const steps = (events: WorkbenchEvent[], jobs = [job]) => conversationRows(events, jobs).flatMap(row => row.activity || []);

describe("recorded workbench activity", () => {
  it("correlates concurrent identical tools by call ID, across evidence and narration", () => {
    const result = steps([
      event(1, "tool.started", { job_id: "job", name: "read_node", call_id: "a" }),
      event(2, "tool.started", { job_id: "job", name: "read_node", call_id: "b" }),
      event(3, "evidence.read", { source: "attachment", name: "SOP" }),
      event(4, "tool.failed", { job_id: "job", name: "read_node", call_id: "b", error: "Node missing" }),
      event(5, "assistant.message", { text: "继续检查" }),
      event(6, "tool.completed", { job_id: "job", name: "read_node", call_id: "a", elapsed_seconds: 2.5 }),
    ]);
    expect(result).toHaveLength(2);
    expect(result.map(s => s.status)).toEqual(["done", "failed"]);
    expect(result[0].elapsed).toBe(2.5);
    expect(result[1].detail).toBe("Node missing");
  });
  it("merges heartbeats but keeps retries and subsequent requests, including a restarted model client", () => {
    const result = steps([
      event(1, "model.progress", { request: 1, phase: "waiting" }),
      event(2, "model.progress", { request: 1, phase: "generating", output_characters: 10 }),
      event(3, "model.progress", { request: 1, phase: "retrying", next_attempt: 2, max_attempts: 3 }),
      event(4, "model.progress", { request: 1, attempt: 2, phase: "generating" }),
      event(5, "model.progress", { request: 1, attempt: 2, phase: "completed" }),
      event(6, "model.progress", { request: 2, phase: "completed" }),
      event(7, "model.progress", { request: 2, phase: "waiting" }),
    ]);
    expect(result).toHaveLength(5);
    expect(result[1].text).toContain("第 2/3 次尝试");
    expect(result[2].status).toBe("done");
    expect(result.at(-1)?.status).toBe("running");
  });
  it("keeps paginated older tasks separate and does not invent successful tool receipts", () => {
    const rows = conversationRows([
      event(10, "tool.started", { name: "get_context" }),
      event(11, "task.completed", { job_id: "old" }),
      event(12, "task.queued", { job_id: "job" }),
      event(13, "tool.started", { name: "read_node" }),
      event(14, "tool.started", { name: "save_candidate" }),
    ], [job]);
    expect(rows[0].jobId).toBe("old");
    expect(rows[0].running).toBe(false);
    expect(rows[0].activity?.[0].status).toBe("recorded");
    expect(rows.at(-1)?.activity?.map(s => s.status)).toEqual(["recorded", "running"]);
  });
  it("updates each actual test run in place and retains a failed result", () => {
    const result = steps([
      event(1, "evaluation.case.progress", {evaluation_id: "e", run_id: 21, name: "source row 2", phase: "replaying", turn: 1, total_turns: 6}),
      event(2, "evaluation.case.progress", {evaluation_id: "e", run_id: 22, name: "source row 3", phase: "replaying", turn: 1, total_turns: 8}),
      event(3, "evaluation.case.progress", {evaluation_id: "e", run_id: 21, name: "source row 2", phase: "completed", status: "failed", turn: 4, total_turns: 6}),
    ]);
    expect(result).toHaveLength(2);
    expect(result[0].text).toContain("4/6 轮 · 运行 #21");
    expect(result[0].status).toBe("failed");
    expect(result[0].statusLabel).toBe("已执行，未通过");
    expect(result[1].status).toBe("running");
  });
  it("retires reviewer progress when its case completes, including late progress events", () => {
    const progress = {stage: "reference:sample", case_name: "sample", request: 1, phase: "generating"};
    const events = [
      event(1, "model.progress", progress),
      event(2, "evaluation.case.progress", {name: "sample", run_id: 21, phase: "completed", status: "failed"}),
    ];
    for (const input of [events, [...events, event(3, "model.progress", progress)]]) {
      const result = steps(input);
      expect(result[0].status).toBe("recorded");
      expect(result[0].text).not.toContain("正在生成");
      expect(result[1].statusLabel).toBe("已执行，未通过");
    }
  });
  it("replaces partial messages with final text without changing anchors or duplicating output", () => {
    const rows = conversationRows([
      event(1, "assistant.message.progress", { message_id: "a", text: "读取", phase: "commentary" }),
      event(2, "assistant.message.progress", { message_id: "a", text: "读取附件" }),
      event(3, "assistant.message", { message_id: "a", text: "读取附件完成" }),
    ], [job]);
    expect(rows).toHaveLength(1);
    expect(rows[0].sequence).toBe(1);
    expect(rows[0].type).toBe("assistant.message");
    expect(rows[0].payload.text).toBe("读取附件完成");
  });
  it("renders failure, compaction, evaluation and requirement review without exposing raw events", () => {
    const result = steps([
      event(1, "context.compaction", { status: "started" }),
      event(2, "context.compaction", { status: "completed" }),
      event(3, "evaluation.started", { evaluation_id: "e" }),
      event(4, "evaluation.updated", { evaluation_id: "e", status: "completed", summary: { total: 3, passed: 2, failed: 1 } }),
      event(5, "requirements.reviewed", { report: { status: "passed" } }),
      event(6, "tool.failed", { name: "save_suite", error: "Codex request timed out" }),
    ]);
    expect(result).toHaveLength(4);
    expect(result.map(s => s.status)).toEqual(["done", "failed", "done", "failed"]);
    expect(result[1].text).toContain("2 项通过");
    expect(result[1].statusLabel).toBe("测试已完成，存在未通过或异常结果");
    expect(result[3].detail).not.toMatch(/codex/i);
  });
});

it("retains diagnostic process and full evidence separately from final prose", () => {
  const data = {job_id: "job", evidence_id: "probe:1", run_id: 42, turn: 2, hypothesis: "Context", results: [{arm: "baseline", text: "Observed"}]};
  const result = steps([event(1, "diagnosis.probe.started", data), event(2, "diagnosis.probe.completed", data), event(3, "diagnosis.completed", {job_id: "job", status: "inconclusive", findings: []})]);
  expect(result).toHaveLength(3);
  expect(result[1].diagnostic).toEqual(data);
  expect(result[2].text).toContain("仍有证据缺口");
});
