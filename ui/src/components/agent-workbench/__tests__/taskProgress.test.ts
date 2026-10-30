import { describe, expect, it } from "vitest";

import {
  modelProgressText,
  taskCompletionText,
  taskProgress,
  workbenchErrorText,
} from "../taskProgress";
import type { Job, WorkbenchEvent } from "../types";

const job: Job = {
  id: "new",
  kind: "build",
  status: "running",
  input: {},
  output: {},
};
const event = (
  sequence: number,
  type: string,
  payload: Record<string, unknown>,
): WorkbenchEvent => ({ sequence, type, payload, created_at: "" });

describe("workbench progress", () => {
  it("asks creation questions without claiming a candidate or simulation exists", () => {
    const message = taskCompletionText({outcome: "needs_input", task_mode: "build", question: "由客户打入，还是由 Agent 主动拨出？"});
    expect(message).toContain("由客户打入，还是由 Agent 主动拨出？");
    expect(message).toContain("确认后继续创建 Agent");
    expect(message).not.toMatch(/已保留候选|测试|调优/);
    expect(taskCompletionText({outcome: "needs_input", task_mode: "test", question: "用哪一列？"})).toContain("测试尚未执行");
  });
  it("distinguishes unresolved work and concrete user questions from completion", () => {
    expect(taskCompletionText({outcome: "build", candidate_id: "saved"})).toContain("调优未完成");
    const question = taskCompletionText({outcome: "build", pending_questions: [{question: "两份政策以哪份为准？"}]});
    expect(question).toContain("两份政策以哪份为准");
    expect(question).not.toContain("任务已完成");
    expect(taskProgress(job, [event(1, "build.progress", {job_id: "new", phase: "requirements_repair", gaps: 3})])).toContain("3 项");
  });
  it("explains provider client restrictions without masking them as runtime errors", () => {
    const message = "Model provider rejected the Codex app-server client (HTTP 403): This account only allows Codex official clients.";
    expect(workbenchErrorText(message)).toContain("检查连接协议与服务接入权限");
    expect(workbenchErrorText(message).toLowerCase()).not.toContain("codex");
    expect(workbenchErrorText(message)).toContain("工作台设置");
    expect(modelProgressText({phase: "failed", message})).toBe(workbenchErrorText(message));
    expect(workbenchErrorText("Configured LLM responses endpoint returned HTTP 403; verify provider credentials and access policy")).toContain("API Key");
  });
  it("reports test-only execution separately from passing and building", () => {
    const text = taskCompletionText({outcome: "test", summary: {total: 266, executed: 265, passed: 200, failed: 60, error: 5, inconclusive: 1}});
    expect(text).toContain("265/266 条已实际运行");
    expect(text).toContain("60 条未通过");
    expect(text).toContain("本次测试未修改 Agent");
    expect(text).not.toContain("候选版本已生成");
    expect(taskProgress(job, [event(5, "test.progress", {job_id: "new", phase: "cases", text: "已执行 20/266 条"})])).toBe("已执行 20/266 条");
  });
  it("does not call a passing small suite complete when requirement coverage is missing", () => {
    const text = taskCompletionText({
      verification: {status: "passed", passed: 3, total: 3},
      requirement_review: {status: "needs_attention", reason: "Missing whole-agent coverage"},
    });
    expect(text).toContain("Missing whole-agent coverage");
    expect(text).not.toContain("3/3");
  });
  it("shows automatic evaluation and repair without claiming generation passed", () => {
    const started = event(1, "task.started", { job_id: "new" });
    expect(taskProgress(job, [started, event(2, "build.progress", { job_id: "new", phase: "evaluating" })])).toContain("实际通话模型");
    expect(taskProgress(job, [started, event(3, "evaluation.updated", { summary: { passed: 2, failed: 1, error: 1, total: 6 } })])).toContain("4/6");
    expect(taskProgress(job, [started, event(4, "build.progress", { job_id: "new", phase: "repairing", round: 2 })])).toContain("第 2 轮");
    expect(taskCompletionText({ verification: { status: "failed", passed: 4, total: 6 } })).toContain("仍有未通过或未完成");
  });
  it("does not display undefined verification counts on older job records", () => {
    for (const verification of ["legacy description", {}, { status: "passed" }]) {
      expect(taskCompletionText({ verification })).toBe("任务已完成，可查看候选版本或测试结果。");
    }
    expect(taskCompletionText({ verification: { status: "passed", passed: 4, total: 4 } })).toContain("4/4 项测试通过");
  });
  it("does not present a previous job's last tool or retries as current work", () => {
    expect(
      taskProgress(job, [
        event(1, "task.started", { job_id: "old" }),
        event(2, "tool.started", { name: "save_candidate" }),
        event(3, "model.progress", { job_id: "old", phase: "retrying" }),
      ]),
    ).toBe("正在准备任务");
  });
  it("replaces stale tool names with actual stream progress and subsequent tool work", () => {
    const events = [
      event(1, "task.started", { job_id: "new" }),
      event(2, "tool.started", { name: "set_task_intent" }),
      event(3, "model.progress", {
        job_id: "new",
        phase: "generating",
        elapsed_seconds: 104,
        output_characters: 17000,
      }),
    ];
    expect(taskProgress(job, events)).toContain("17,000 字符 · 104 秒");
    expect(
      taskProgress(job, [
        ...events,
        event(4, "tool.started", { name: "save_candidate" }),
      ]),
    ).toBe("校验并保存候选版本");
  });
  it("reports bounded retries and distinct timeout, rate limit and terminal states", () => {
    expect(
      modelProgressText({
        phase: "retrying",
        reason: "model_timeout",
        next_attempt: 2,
        max_attempts: 3,
        elapsed_seconds: 90,
      }),
    ).toContain("模型响应超时，准备第 2/3 次尝试");
    expect(
      modelProgressText({
        phase: "waiting",
        attempt: 2,
        max_attempts: 3,
        elapsed_seconds: 92,
      }),
    ).toContain("第 2/3 次尝试");
    expect(
      modelProgressText({
        phase: "retrying",
        reason: "rate_limited",
        next_attempt: 2,
        max_attempts: 3,
      }),
    ).toContain("模型服务限流");
    expect(
      workbenchErrorText("Model output reached the 32768 token limit"),
    ).toContain("未执行不完整的工具调用");
    expect(
      workbenchErrorText("Workbench task exceeded its time budget"),
    ).toContain("当时配置的时间上限");
    expect(
      workbenchErrorText("Model connection timed out after 15s."),
    ).toContain("检查服务地址和网络");
    expect(
      workbenchErrorText(
        "Workbench model token budget exceeded: 15/40 requests, 300001/300000 cumulative tokens.",
      ),
    ).toContain("15/40 次请求，300,001/300,000 token");
    const historical = workbenchErrorText("Workbench model token budget exceeded: 11/unlimited requests, 673335/600000 cumulative tokens.");
    expect(historical).toContain("当时配置");
    expect(historical).toContain("11/不限 次请求");
    expect(historical).not.toContain("缩小");
  });
});

it('explains SDK stream failures and shows the actual bounded retry count', () => {
  for (const message of [
    'Model stream interrupted before completion; incomplete output was not accepted.',
    "Codex turn failed: {'message': 'stream disconnected before completion: Transport error: network error: error decoding response body', 'codexErrorInfo': 'other'}",
  ]) {
    expect(workbenchErrorText(message)).toContain('模型响应流中断');
    expect(workbenchErrorText(message)).toContain('准备断点仍保留');
    expect(workbenchErrorText(message)).not.toContain('构建服务异常');
  }
  expect(modelProgressText({phase: 'retrying', next_attempt: 2, max_attempts: 2})).toContain('2/2');
});
