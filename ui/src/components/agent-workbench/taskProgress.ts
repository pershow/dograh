import type { Job, WorkbenchEvent } from "./types";

export function toolProgressText(name: string): string {
  const labels: Record<string, string> = {
    get_context: "读取 Agent 配置", list_node_types: "读取节点类型",
    get_node_type: "读取节点规则", read_node: "检查节点配置",
    set_task_intent: "确认本次任务类型", validate_candidate: "校验工作流",
    save_candidate: "校验并保存候选版本", save_prompt_agent: "保留完整 SOP 并生成候选版本",
    save_suite: "保存测试用例", save_scenarios: "编译并保存逐轮测试场景",
    report_verification_issue: "记录测试预期或资料冲突", finish_analysis: "整理分析结果",
    list_attachments: "查看附件目录", read_attachment: "读取附件内容",
    read_conversation: "查阅历史需求与会话", list_agent_runs: "查找 Agent 运行记录",
    read_agent_run: "读取 Agent 运行资料", read_evaluation: "读取实际测试结果", verify_analysis: "核对分析结论与原始证据", submit_review: "提交需求覆盖检查结果",
    classify_request: "识别本次任务类型", select_test_source: "确认测试数据列与角色", submit_test_suite: "准备本次测试用例",
    select_business_sources: "检索相关原始业务资料", read_task_basis: "查阅本次任务依据", read_review_source: "读取原始依据页",
  };
  return labels[name] || "执行处理步骤";
}

export function modelProgressText(payload: Record<string, unknown>): string {
  if (payload.case_name) {
    const { case_name, ...rest } = payload;
    return `评审 ${String(case_name)} · ${modelProgressText(rest)}`;
  }
  const seconds = Number(payload.elapsed_seconds || 0);
  const received = Number(payload.output_characters || 0).toLocaleString();
  const attempt = Number(payload.attempt || 1);
  const maximum = Number(payload.max_attempts || 1);
  const retry = attempt > 1 ? ` · 第 ${attempt}/${maximum} 次尝试` : "";
  const reason =
    payload.reason === "model_timeout"
      ? "模型响应超时"
      : payload.reason === "rate_limited"
        ? "模型服务限流"
        : "模型连接异常";
  switch (payload.phase) {
    case "waiting":
      return `等待模型响应 · ${seconds} 秒${retry}`;
    case "generating":
      return `正在生成 · 已收到 ${received} 字符 · ${seconds} 秒${retry}`;
    case "retrying":
      return `${reason}，准备第 ${Number(payload.next_attempt)}/${maximum} 次尝试 · ${seconds} 秒`;
    case "repairing":
      return `模型生成的工具参数格式有误，正在自动纠正（${attempt}/${maximum}）`;
    case "failed":
      return workbenchErrorText(String(payload.message || reason));
    case "completed":
      return "模型生成完成，正在处理结果";
    default:
      return "正在准备构建";
  }
}

export function workbenchErrorText(message: string): string {
  if (message.includes("This account only allows Codex official clients"))
    return "模型服务拒绝了工作台客户端（HTTP 403）。请检查连接协议与服务接入权限，或在工作台设置中选择允许接入的构建模型连接。";
  if (/HTTP (401|403); verify provider credentials and access policy/.test(message))
    return "模型服务拒绝访问，请检查构建模型连接的 API Key、账号权限和客户端接入限制。";
  if (message.includes("Model connection interrupted"))
    return "模型连接中断，未执行本次不完整的工具调用。已保存的候选版本仍保留，请重试。";
  if (message.includes("Model tool arguments"))
    return "模型生成的工具参数格式有误，自动纠正后仍未完成。该次错误调用未执行，已保存的候选版本可继续使用，请重试。";
  if (message.includes("invalid streaming response") || message.includes("invalid JSON event"))
    return "模型返回的数据格式异常，该次不完整调用未执行，请重试。";
  if (message.includes("connection timed out"))
    return "连接模型服务超时，任务已停止。请检查服务地址和网络后重试。";
  const budget = message.match(
    /Workbench model (request|token) budget exceeded: (\d+)\/(\d+|unlimited) requests, (\d+)\/(\d+|unlimited) cumulative tokens/,
  );
  if (budget)
    return `该任务触发了当时配置的模型${budget[1] === "request" ? "请求次数" : "累计 token"}上限：${budget[2]}/${budget[3] === "unlimited" ? "不限" : budget[3]} 次请求，${Number(budget[4]).toLocaleString()}/${budget[5] === "unlimited" ? "不限" : Number(budget[5]).toLocaleString()} token。已保存的候选版本仍保留；取消或调整该限制后可重新发起任务。`;
  const limit = message.match(/output reached the (\d+) token limit/);
  if (limit)
    return `本次输出达到 ${Number(limit[1]).toLocaleString()} token 上限，未执行不完整的工具调用。请提高输出预算或分步构建。`;
  if (message.includes("no data for"))
    return `本次模型调用长时间没有返回有效数据。已保存的结果仍保留，可重试任务或继续未完成的测试。`;
  if (
    message.includes("total time budget") ||
    message.includes("exceeded its time budget")
  )
    return "该任务触发了当时配置的时间上限。已保存的候选版本仍保留；取消该限制后可重新发起任务。";
  if (message.includes("without a complete response"))
    return "模型输出意外中断，未执行不完整的工具调用，请重试。";
  // Old task records may contain internal runtime names. Translate these at
  // the display boundary without rewriting original conversations or logs.
  if (/codex/i.test(message)) {
    if (message.includes("无法恢复"))
      return "无法恢复此工作台会话。请联系管理员检查存储或恢复备份，原始历史未被替换。";
    if (message.includes("without a valid candidate"))
      return "本次构建未生成有效的候选版本，请查看构建助手的回复后重试。";
    if (message.includes("did not save a valid simulation suite"))
      return "候选版本已保存，但测试用例未能完整保存，请重试。";
    if (message.includes("OpenAI-compatible"))
      return "构建助手需要兼容 OpenAI 协议的模型连接（支持 Qwen），请检查模型配置。";
    if (message.includes("timed out"))
      return "构建服务响应超时。已保存的候选版本仍保留，请稍后重试。";
    return "构建服务异常，任务未完成。已保存的候选版本仍保留，请重试；若持续失败，请联系管理员检查服务日志。";
  }
  return message;
}

export function simulationPhase(p: Record<string, unknown>): string {
  const phase = ({preparing: "准备测试上下文", scenario_ready: "场景事实与假设已冻结", customer: "模拟客户思考中", checking_alignment: "生成回应并检查对话承接", alignment_checked: "对话承接检查已保存", reviewing: "核对原始依据与实际回复"} as Record<string, string>)[String(p.phase)];
  return phase || (p.mode === "dynamic" ? `已执行 ${p.turn || 0} 轮` : `${p.turn || 0}/${p.total_turns || 0} 轮`);
}

export function taskProgress(job: Job, events: WorkbenchEvent[]): string {
  if (job.status === "queued") return "排队中，等待构建服务";
  const start = events.findLast(
    (e) => e.type === "task.started" && e.payload.job_id === job.id,
  )?.sequence;
  const latest = events.findLast(
    (e) =>
      (e.payload.job_id === job.id ||
        (start !== undefined && e.sequence > start && !e.payload.job_id)) &&
      ["model.progress", "tool.started", "build.progress", "test.progress", "evaluation.updated", "evaluation.case.progress", "context.compaction"].includes(e.type),
  );
  if (latest?.type === "test.progress") return String(latest.payload.text || "正在执行测试");
  if (latest?.type === "evaluation.case.progress") return `正在测试 ${latest.payload.name} · ${simulationPhase(latest.payload)}`;
  if (latest?.type === "context.compaction") return latest.payload.status === "started"
    ? "正在整理上下文，保留需求与当前进展" : "上下文已整理，继续处理需求";
  if (latest?.type === "evaluation.updated") {
    const summary = latest.payload.summary as Record<string, number> | undefined;
    return `正在自动测试 · 已完成 ${Number(summary?.passed || 0) + Number(summary?.failed || 0) + Number(summary?.error || 0) + Number(summary?.inconclusive || 0)}/${summary?.total || 0} 项`;
  }
  if (latest?.type === "build.progress") {
    if (latest.payload.phase === "requirements_review") return "正在检查需求范围、改动与测试覆盖";
    if (latest.payload.phase === "requirements_repair") return `正在补齐需求与测试覆盖 · ${latest.payload.gaps} 项`;
    if (latest.payload.phase === "repairing") return `正在修复未通过的测试 · 第 ${latest.payload.round} 轮`;
    if (latest.payload.phase === "verified") return "自动测试通过，正在整理结果";
    if (latest.payload.phase === "needs_attention") return "自动验证完成，仍有问题需要处理";
    return "正在使用实际通话模型自动测试";
  }
  if (latest?.type === "model.progress")
    return modelProgressText(latest.payload);
  if (latest) return toolProgressText(String(latest.payload.name));
  return "正在准备任务";
}

export function taskCompletionText(output: Record<string, unknown> = {}): string {
  if (Array.isArray(output.pending_questions) && output.pending_questions.length)
    return `调优未完成，已保留候选。需要补充：${output.pending_questions.map((q: {question: string}) => q.question).join("；")}`;
  if (output.outcome === "needs_input") return `需要补充信息：${String(output.question || "请明确测试资料与要求。")} 测试尚未执行。`;
  if (output.outcome === "review") {
    const s = output.summary as Record<string, number> | undefined;
    return s ? `已有对话复核完成：${s.total} 条，${s.passed} 条通过，${s.failed} 条未通过，${s.error} 条原执行错误，${s.inconclusive} 条无法判定。未重新运行对话，未修改 Agent；旧评审保留。` : "复核结束，请查看证据与结果。";
  }
  if (output.outcome === "test") {
    const s = output.summary as Record<string, number> | undefined;
    return s ? `测试执行完成：${s.executed}/${s.total} 条已实际运行，${s.passed} 条通过，${s.failed} 条未通过，${s.error} 条执行错误，${s.inconclusive} 条无法判定。本次测试未修改 Agent。` : "测试任务结束，请查看实际运行记录与结果。";
  }
  const review = output.requirement_review as {status?: string; reason?: string; requirements?: {status: string; requirement: string; correction: string}[]} | undefined;
  if (review && review.status !== "passed") {
    const gaps = review.requirements?.filter(r => r.status !== "covered") || [];
    return `${output.outcome === "analysis" ? "" : "调优未完成，候选版本已保存。"}需求完整性检查仍有待处理项。${review.reason || gaps.map(r => `${r.requirement}：${r.correction}`).join("；") || "审查未取得足够证据。"}`;
  }
  if (typeof output.verification_issue === "string") return `验证需要处理：${output.verification_issue}`;
  if (output.outcome === "analysis") return "分析已完成。可以继续追问，或提出修改要求。";
  if (output.outcome === "build" && !(output.verification as {confirmed?: boolean} | undefined)?.confirmed)
    return `调优未完成，候选版本已保存。${String((output.verification as {reason?: string} | undefined)?.reason || "尚未取得完整的回归通过记录。")}`;
  const verification = output.verification as Record<string, unknown> | undefined;
  if (verification && typeof verification === "object" &&
      typeof verification.passed === "number" && typeof verification.total === "number") {
    const counts = `${verification.passed}/${verification.total}`;
    return verification.status === "passed"
      ? `构建与自动验证完成，${counts} 项测试通过。可查看候选版本和测试结果。`
      : `候选版本已生成，自动验证 ${counts} 项通过，仍有未通过或未完成的测试。${typeof verification.reason === "string" ? verification.reason : "请查看测试结果。"}`;
  }
  return "任务已完成，可查看候选版本或测试结果。";
}
