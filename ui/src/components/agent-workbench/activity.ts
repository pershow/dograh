import { modelProgressText, simulationPhase, taskProgress, toolProgressText, workbenchErrorText } from "./taskProgress";
import type { Job, WorkbenchEvent } from "./types";

export type ActivityStep = {
  sequence: number;
  text: string;
  status: "running" | "done" | "failed" | "recorded" | "cancelled";
  detail?: string;
  elapsed?: number;
  evidence?: WorkbenchEvent[];
  statusLabel?: string;
  diagnostic?: Record<string, unknown>;
  modelError?: Record<string, unknown>;
};
export type ConversationRow = WorkbenchEvent & {
  activity?: ActivityStep[];
  evidence?: WorkbenchEvent[];
  jobId?: string;
  running?: boolean;
};

const visibleMessages = new Set([
  "task.queued", "assistant.message", "assistant.message.progress", "voice.transcript",
  "task.completed", "task.failed", "task.cancelled", "candidate.adopted", "evidence.read",
  "task.needs_attention", "task.awaiting_input", "basis.frozen",
]);
const terminal = new Set(["task.completed", "task.failed", "task.cancelled", "task.needs_attention", "task.awaiting_input"]);

/** Render recorded actions, never infer tool success from a subsequent action. */
export function conversationRows(events: WorkbenchEvent[], jobs: Job[]): ConversationRow[] {
  const rows: ConversationRow[] = [];
  const statuses = new Map(jobs.map(job => [job.id, job.status]));
  for (const e of events) if (terminal.has(e.type) && e.payload.job_id)
    statuses.set(String(e.payload.job_id), e.type.slice(5));
  const isRunning = (id?: string) => !!id && ["queued", "running"].includes(statuses.get(id) || "");
  // Older records lack job_id. Recover it only inside task boundaries, including
  // a paginated tail whose terminal event is present but whose start is not.
  const owners = new Map<number, string | undefined>();
  let nextJob: string | undefined;
  for (const e of [...events].reverse()) {
    if (terminal.has(e.type)) nextJob = String(e.payload.job_id || "") || undefined;
    owners.set(e.sequence, nextJob);
    if (e.type === "task.queued" || e.type === "task.started") nextJob = undefined;
  }
  let currentJob: string | undefined;
  const activeId = jobs.find(job => ["queued", "running"].includes(job.status))?.id;
  const steps = new Map<string, ActivityStep>();
  const messages = new Map<string, ConversationRow>();
  const pendingLegacy = new Map<string, ActivityStep>();
  const modelSteps = new Map<string, { key: string; step: ActivityStep }>();
  const completedReviews = new Set<string>();
  const recordedProgress = (text: string) => text.replace("正在生成", "模型生成记录").replace("等待模型响应", "模型等待记录");
  let block: ConversationRow | undefined;
  for (const e of events) {
    if (e.type === "task.queued" || e.type === "task.started") currentJob = String(e.payload.job_id || "") || undefined;
    const jobId = String(e.payload.job_id || currentJob || owners.get(e.sequence) || activeId || "") || undefined;
    const running = isRunning(jobId);
    const reviewOnly = !!(jobs.find(j => j.id === jobId)?.input.review_evaluation_ids as string[] | undefined)?.length;
    const scope = `${jobId || "unknown"}:${e.payload.stage || "main"}`;
    const add = (step: ActivityStep) => {
      if (!block || block.jobId !== jobId) {
        block = { ...e, activity: [], jobId, running };
        rows.push(block);
      }
      block.activity!.push(step);
      return step;
    };
    const activityStatus = (): ActivityStep["status"] => running ? "running"
      : statuses.get(jobId || "") === "cancelled" ? "cancelled" : "recorded";
    if (e.type === "model.error") {
      add({sequence: e.sequence, text: `模型连接异常 · 第 ${e.payload.attempt}/${e.payload.max_attempts} 次请求`,
        status: "failed", modelError: e.payload,
        elapsed: typeof e.payload.elapsed_seconds === "number" ? e.payload.elapsed_seconds : undefined});
      continue;
    }
    if (e.type === "build.resumed") {
      add({sequence: e.sequence, text: String(e.payload.text), status: "done",
        detail: `候选 ${e.payload.candidate_id} · 来源任务 ${e.payload.source_job_id}`});
      continue;
    }
    if (e.type.startsWith("diagnosis.")) {
      const p = e.payload;
      const labels: Record<string, string> = {
        "diagnosis.started": "开始根因排查",
        "diagnosis.evidence": `读取运行 #${p.run_id} 的${({overview: "执行概览", request: "生效请求", execution: "节点轨迹", evaluation: "评测与仿真记录", definition: "冻结配置", transcript: "实际对话"} as Record<string, string>)[String(p.section)] || "证据"}`,
        "diagnosis.probe.started": `开始隔离对照 · 运行 #${p.run_id} 第 ${p.turn} 轮`,
        "diagnosis.probe.progress": `${p.arm === "baseline" ? "原始条件" : "临时对照"} · 第 ${p.repeat} 次调用${p.error ? "失败" : "完成"}`,
        "diagnosis.probe.reused": `恢复已有对照证据 · 运行 #${p.run_id} 第 ${p.turn} 轮`,
        "diagnosis.probe.completed": `隔离对照完成 · 运行 #${p.run_id} 第 ${p.turn} 轮`,
        "diagnosis.completed": p.status === "concluded" ? "根因排查完成 · 结论与证据已核验" : "根因排查完成 · 仍有证据缺口",
      };
      add({ sequence: e.sequence, text: labels[e.type] || "排查记录", status: p.error ? "failed" : "done",
        detail: String(p.hypothesis || p.question || "") || undefined, diagnostic: p });
      continue;
    }
    if (e.type.startsWith("tool.")) {
      const key = `${scope}:tool:${e.payload.call_id || e.payload.name}`;
      let step = steps.get(key);
      if (e.type === "tool.started") {
        const previous = pendingLegacy.get(scope);
        if (previous?.status === "running") previous.status = "recorded";
        step = add({ sequence: e.sequence, text: toolProgressText(String(e.payload.name)), status: activityStatus() });
        steps.set(key, step);
        if (!e.payload.call_id) pendingLegacy.set(scope, step);
      } else if (e.type === "tool.completed" || e.type === "tool.failed") {
        step ||= add({ sequence: e.sequence, text: toolProgressText(String(e.payload.name)), status: "recorded" });
        step.status = e.type === "tool.failed" ? "failed" : "done";
        step.elapsed = typeof e.payload.elapsed_seconds === "number" ? e.payload.elapsed_seconds : undefined;
        if (e.payload.error) step.detail = workbenchErrorText(String(e.payload.error));
      }
      continue;
    }
    if (["model.progress", "context.compaction", "evaluation.started", "evaluation.updated", "evaluation.case.progress", "build.progress", "test.progress", "requirements.reviewed"].includes(e.type)) {
      const p = e.payload;
      let key = `${scope}:${e.type}`;
      let text: string;
      let status = activityStatus();
      let statusLabel: string | undefined;
      if (e.type === "evaluation.case.progress") {
        key += `:${p.evaluation_id}:${p.name}`;
        text = `${reviewOnly ? "复核" : "测试"} ${p.name} · ${p.phase === "completed" && reviewOnly ? "复核已完成" : simulationPhase(p)}${p.run_id ? ` · 运行 #${p.run_id}` : ""}`;
        status = p.phase === "completed" ? (p.status === "passed" ? "done" : "failed") : activityStatus();
        if (p.phase === "completed") statusLabel = p.status === "passed" ? "测试通过" : p.status === "error" ? "执行错误" : p.status === "inconclusive" ? "已执行，无法判定" : "已执行，未通过";
        if (p.phase === "completed") {
          const reviewScope = `${jobId || "unknown"}:reference:${p.name}`;
          completedReviews.add(reviewScope);
          const review = modelSteps.get(reviewScope)?.step;
          if (review?.status === "running") {
            review.status = "recorded";
            review.text = recordedProgress(review.text);
          }
        }
      } else if (e.type === "test.progress") {
        key += `:${p.phase}`;
        text = String(p.text || "正在执行测试");
        status = p.phase === "cases" && running ? "running" : "recorded";
      } else if (e.type === "model.progress") {
        key += `:${p.request_id || p.request || "unknown"}:${p.attempt || 1}:${["retrying", "repairing", "failed"].includes(String(p.phase)) ? p.phase : "response"}`;
        text = modelProgressText(p);
        if (!running || completedReviews.has(scope)) text = recordedProgress(text);
        const previous = modelSteps.get(scope);
        if (previous && previous.key !== key && previous.step.status === "running") previous.step.status = "recorded";
        if (steps.get(key)?.status === "done" && ["waiting", "generating"].includes(String(p.phase))) steps.delete(key);
        if (p.phase === "failed") status = "failed";
        else if (p.phase === "completed") status = "done";
        else if (p.phase === "retrying" || p.phase === "repairing") status = "recorded";
        else if (completedReviews.has(scope)) status = "recorded";
      } else if (e.type === "context.compaction") {
        text = p.status === "started" ? "正在整理上下文，保留需求与当前进展" : "已整理上下文，原始对话仍可向前翻阅。";
        if (p.status === "completed") status = "done";
        // A later compaction is a new action, not an update of an old cycle.
        if (p.status === "started") steps.delete(key);
      } else if (e.type === "evaluation.started" || e.type === "evaluation.updated") {
        key = `${scope}:evaluation:${p.evaluation_id || "current"}`;
        const s = p.summary as Record<string, number> | undefined;
        text = s ? `${reviewOnly ? "已有对话复核" : "自动测试"} · 已完成 ${(s.passed || 0) + (s.failed || 0) + (s.error || 0) + (s.inconclusive || 0)}/${s.total || 0} 项 · ${s.passed || 0} 项通过` : reviewOnly ? "开始复核已有对话，未重新运行" : "开始自动测试";
        if (p.status && p.status !== "running") status = p.status === "cancelled" ? "cancelled" : s && (s.failed || s.error || s.inconclusive) ? "failed" : "done";
        if (p.status === "completed") statusLabel = s && (s.failed || s.error || s.inconclusive) ? "测试已完成，存在未通过或异常结果" : "测试通过";
      } else if (e.type === "requirements.reviewed") {
        key += `:${p.candidate_id}:${p.phase}`;
        const report = p.report as Record<string, unknown> | undefined;
        text = report?.status === "passed" ? "需求覆盖检查通过" : "需求覆盖检查仍有待处理项";
        status = report?.status === "passed" ? "done" : "failed";
      } else {
        key += `:${p.phase}:${p.round || 0}`;
        text = taskProgress({ id: jobId || "activity", kind: "build", status: "running", input: {}, output: {} }, [
          { ...e, payload: { ...p, job_id: jobId || "activity" } },
        ]);
        status = p.phase === "verified" ? "done" : p.phase === "needs_attention" ? "failed" : "recorded";
      }
      const step = steps.get(key);
      if (step) Object.assign(step, { text, status, statusLabel });
      else steps.set(key, add({ sequence: e.sequence, text, status, statusLabel }));
      if (e.type === "model.progress") modelSteps.set(scope, { key, step: steps.get(key)! });
      continue;
    }
    if (!visibleMessages.has(e.type)) continue;
    if (e.type === "assistant.message" || e.type === "assistant.message.progress") {
      const key = e.payload.message_id ? `${scope}:${e.payload.message_id}` : undefined;
      const previous = key ? messages.get(key) : undefined;
      if (previous) {
        // Final text replaces the streaming snapshot, with its original scroll anchor.
        Object.assign(previous, { type: e.type, payload: e.payload, running });
        continue;
      }
      const row = { ...e, jobId, running };
      rows.push(row);
      if (key) messages.set(key, row);
    } else if (e.type === "evidence.read") {
      // Keep source links beside the operation that read them, so repeated reads
      // do not fragment one task into dozens of separate process cards.
      const step = block?.activity?.at(-1);
      if (step && block?.jobId === jobId) {
        (step.evidence ||= []).push(e);
        continue;
      }
      const previous = rows.at(-1);
      if (previous?.evidence && previous.jobId === jobId) previous.evidence.push(e);
      else rows.push({ ...e, jobId, evidence: [e] });
    } else rows.push({ ...e, jobId });
    block = undefined;
    if (terminal.has(e.type)) currentJob = undefined;
  }
  return rows;
}
