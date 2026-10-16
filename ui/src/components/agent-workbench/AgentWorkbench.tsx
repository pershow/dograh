"use client";
import "@xyflow/react/dist/style.css";

import {
  Background,
  Controls,
  Handle,
  type NodeProps,
  Position,
  ReactFlow,
} from "@xyflow/react";
import {
  ArrowLeft,
  ArrowUp,
  Bot,
  Check,
  FileText,
  FlaskConical,
  Loader2,
  Mic,
  Paperclip,
  Settings2,
  Sparkles,
  X,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  workbenchAdoptApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdAdoptPost as adopt,
  workbenchCancelApiV1AgentWorkbenchSessionsSessionIdJobsJobIdCancelPost as cancel,
  workbenchCandidateApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdGet as getCandidate,
  workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet as getCapabilities,
  workbenchCreateSessionApiV1AgentWorkbenchSessionsPost as createSession,
  workbenchEvaluateApiV1AgentWorkbenchSessionsSessionIdEvaluationsPost as evaluate,
  workbenchEventsApiV1AgentWorkbenchSessionsSessionIdEventsGet as getEvents,
  workbenchInstructionApiV1AgentWorkbenchSessionsSessionIdInstructionsPost as instruct,
  workbenchRetryAssetApiV1AgentWorkbenchSessionsSessionIdAssetsAssetIdRetryPost as retryAsset,
  workbenchSessionApiV1AgentWorkbenchSessionsSessionIdGet as getSession,
  workbenchSettingsApiV1AgentWorkbenchSessionsSessionIdPatch as settings,
  workbenchSuiteApiV1AgentWorkbenchSessionsSessionIdSuitesPost as saveSuite,
  workbenchTuneApiV1AgentWorkbenchSessionsSessionIdTunePost as tune,
  workbenchUploadApiV1AgentWorkbenchSessionsSessionIdAssetsPost as upload,
} from "@/client/sdk.gen";
import type { SimulationSuite } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

import type {
  Candidate,
  WorkbenchEvent,
  WorkbenchSession,
  WorkbenchState,
} from "./types";
import { useWorkbenchVoice } from "./useWorkbenchVoice";
import styles from "./workbench.module.css";

function result<T>(response: {
  data?: { data: Record<string, unknown> };
  error?: unknown;
}): T {
  if (response.error)
    throw new Error(
      detailFromError(
        response.error,
        response.error instanceof Error
          ? response.error.message
          : "操作失败，请重试",
      ),
    );
  if (!response.data) throw new Error("服务未返回数据");
  return response.data.data as T;
}
function PreviewNode({ data }: NodeProps) {
  return (
    <div className={styles.node}>
      <Handle type="target" position={Position.Top} />
      <div className={styles.nodeType}>{String(data.nodeType)}</div>
      <strong>{String(data.name || "Node")}</strong>
      <p>{String(data.prompt || data.greeting || "").slice(0, 180)}</p>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}
const nodeTypes = { preview: PreviewNode };
const statusText: Record<string, string> = {
  queued: "排队中",
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  passed: "通过",
  error: "执行错误",
  inconclusive: "无法判定",
};

export function AgentWorkbench({
  workflowId,
  sessionId,
}: {
  workflowId?: number;
  sessionId?: string;
}) {
  const {
    user,
    loading: authLoading,
    getAccessToken,
    redirectToLogin,
  } = useAuth();
  const router = useRouter();
  const [state, setState] = useState<WorkbenchState>();
  const [session, setSession] = useState<string>(sessionId || "");
  const [events, setEvents] = useState<WorkbenchEvent[]>([]);
  const [error, setError] = useState("");
  const [syncError, setSyncError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [text, setText] = useState("");
  const [tab, setTab] = useState<"canvas" | "tests" | "tuning">("canvas");
  const [candidateId, setCandidateId] = useState("");
  const [candidate, setCandidate] = useState<Candidate>();
  const [suiteId, setSuiteId] = useState("");
  const [selectedAssets, setSelectedAssets] = useState<string[]>([]);
  const [suiteEditor, setSuiteEditor] = useState<string | null>(null);
  const [caseIndex, setCaseIndex] = useState(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const cursor = useRef(0);
  const init = useRef<Promise<WorkbenchSession> | null>(null);
  const inFlight = useRef(false);
  const active = state?.jobs.find((j) =>
    ["queued", "running"].includes(j.status),
  );
  const locked = busy || !!active;
  const reload = useCallback(async () => {
    if (!session || inFlight.current) return;
    inFlight.current = true;
    try {
      const [snapshot, updates] = await Promise.all([
        getSession({ path: { session_id: session } }),
        getEvents({
          path: { session_id: session },
          query: { after: cursor.current },
        }),
      ]);
      setState(result<WorkbenchState>(snapshot));
      setSyncError("");
      const fresh = result<{ events: WorkbenchEvent[] }>(updates).events;
      if (fresh.length) {
        cursor.current = fresh[fresh.length - 1].sequence;
        setEvents((old) => [...old, ...fresh].slice(-400));
      }
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : "无法同步工作台");
    } finally {
      inFlight.current = false;
    }
  }, [session]);
  const voice = useWorkbenchVoice(
    session || undefined,
    getAccessToken,
    selectedAssets,
    candidateId,
    suiteId,
    reload,
  );
  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      redirectToLogin();
      return;
    }
    let disposed = false;
    if (!init.current)
      init.current = (async () => {
        const capabilities = result<{ enabled: boolean }>(
          await getCapabilities(),
        );
        if (!capabilities.enabled)
          throw new Error(
            "工作台尚未启用，请设置 AGENT_WORKBENCH_ENABLED=true。",
          );
        return sessionId
          ? result<WorkbenchState>(
              await getSession({ path: { session_id: sessionId } }),
            ).session
          : result<WorkbenchSession>(
              await createSession({
                body: {
                  workflow_id: workflowId,
                  name: "New Agent",
                  model_protocol: "chat",
                },
              }),
            );
      })();
    init.current
      .then((value) => {
        if (disposed) return;
        setSession(value.id);
        if (!sessionId)
          router.replace(`/workflow/workbench?session=${value.id}`);
      })
      .catch((error) => {
        if (!disposed) setError(error.message);
      });
    return () => {
      disposed = true;
    };
  }, [authLoading, user, workflowId, sessionId, redirectToLogin, router]);
  useEffect(() => {
    if (authLoading || !user || !session) return;
    void reload();
    const interval = setInterval(reload, 2000);
    return () => clearInterval(interval);
  }, [authLoading, user, session, reload]);
  const choiceInitialized = useRef(false);
  useEffect(() => {
    if (!choiceInitialized.current && state) {
      choiceInitialized.current = true;
      if (state.candidates[0]) setCandidateId(state.candidates[0].id);
    }
  }, [state]);
  useEffect(() => {
    if (!suiteId && state?.suites[0]) setSuiteId(state.suites[0].id);
  }, [state, suiteId]);
  useEffect(() => {
    if (!candidateId || !session) {
      setCandidate(undefined);
      return;
    }
    let disposed = false;
    getCandidate({ path: { session_id: session, candidate_id: candidateId } })
      .then((r) => {
        if (!disposed) setCandidate(result<Candidate>(r));
      })
      .catch((e) => setError(e.message));
    return () => {
      disposed = true;
    };
  }, [candidateId, session]);
  const completedCursor = useRef(0);
  useEffect(() => {
    const completed = events
      .filter(
        (e) =>
          e.type === "task.completed" && e.sequence > completedCursor.current,
      )
      .at(-1);
    if (!completed) return;
    completedCursor.current = completed.sequence;
    const output = completed.payload.output as Record<string, unknown>;
    if (typeof output?.candidate_id === "string")
      setCandidateId(output.candidate_id);
    if (typeof output?.suite_id === "string") setSuiteId(output.suite_id);
  }, [events]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [events.length]);
  const action = async (operation: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      await reload();
    } catch (error) {
      setError(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };
  const graph =
    candidate?.snapshot?.workflow_json || state?.target.workflow_json;
  const nodes = useMemo(
    () =>
      (graph?.nodes || []).map((n) => ({
        ...n,
        type: "preview",
        data: { ...n.data, nodeType: n.type },
        draggable: false,
      })),
    [graph],
  );
  const edges = useMemo(
    () =>
      (graph?.edges || []).map((e) => ({
        ...e,
        type: "smoothstep",
        label: String(
          (e.data as Record<string, unknown>)?.label || e.label || "",
        ),
      })),
    [graph],
  );
  const selectedSuite = state?.suites.find((s) => s.id === suiteId);
  const evaluations =
    state?.evaluations.filter(
      (e) => e.candidate_id === candidateId && e.suite_id === suiteId,
    ) || [];
  const evaluation = evaluations[0];
  const currentCase =
    evaluation?.results[
      Math.min(caseIndex, (evaluation?.results.length || 1) - 1)
    ];
  const addFiles = async (files: File[]) => {
    if (!session || locked) return;
    if (files.length + selectedAssets.length > 5) {
      setError("每条指令最多引用 5 个附件。");
      return;
    }
    await action(async () => {
      for (const file of files) {
        if (file.size > 20 * 1024 * 1024)
          throw new Error(`${file.name} 超过 20 MiB 限制`);
        const asset = result<{ id: string; status: string; error?: string }>(
          await upload({ path: { session_id: session }, body: { file } }),
        );
        if (asset.status === "ready")
          setSelectedAssets((old) => [...old, asset.id]);
        else setError(asset.error || "附件解析失败，可重试或替换文件");
      }
    });
  };
  const runTests = () =>
    action(async () => {
      result(
        await evaluate({
          path: { session_id: session },
          body: {
            candidate_id: candidateId,
            suite_id: suiteId,
            idempotency_key: crypto.randomUUID(),
          },
        }),
      );
      setTab("tests");
    });
  const runTune = () =>
    action(async () => {
      result(
        await tune({
          path: { session_id: session },
          body: {
            candidate_id: candidateId,
            suite_id: suiteId,
            idempotency_key: crypto.randomUUID(),
            instruction: text || "修复失败用例并保留已通过行为。",
            asset_ids: selectedAssets,
            max_rounds: 1,
          },
        }),
      );
      setTab("tuning");
    });
  const messages = events.filter((e) =>
    [
      "task.queued",
      "assistant.message",
      "voice.transcript",
      "task.completed",
      "task.failed",
      "task.cancelled",
      "candidate.adopted",
    ].includes(e.type),
  );
  if (!state)
    return (
      <div className={styles.loading}>
        {error ? (
          <p role="alert">{error}</p>
        ) : (
          <>
            <Loader2 className="animate-spin" />
            <p>正在准备工作台…</p>
          </>
        )}
        <Link href="/workflow">返回 Agents</Link>
      </div>
    );
  return (
    <div className={styles.workbench}>
      <header className={styles.header}>
        <Link href="/workflow" aria-label="返回 Agents">
          <ArrowLeft size={20} />
        </Link>
        <strong>{candidate?.name || state.target.name}</strong>
        <span className={styles.pill}>Agent Workbench</span>
        <div className={styles.headerActions}>
          <Link href={`/workflow/${state.session.workflow_id}`}>
            打开原编辑器
          </Link>
          <Button
            disabled={!candidateId || locked}
            onClick={() =>
              action(async () => {
                result(
                  await adopt({
                    path: { session_id: session, candidate_id: candidateId },
                  }),
                );
                setNotice("已应用到草稿。可在原编辑器中继续编辑、测试和发布。");
              })
            }
          >
            <Check size={15} />
            应用到草稿
          </Button>
        </div>
      </header>
      <div className={styles.toolbar}>
        <nav>
          {[
            ["canvas", "流程画布"],
            ["tests", "文本仿真"],
            ["tuning", "调优记录"],
          ].map(([key, label]) => (
            <button
              key={key}
              onClick={() => setTab(key as typeof tab)}
              aria-pressed={tab === key}
            >
              {label}
            </button>
          ))}
        </nav>
        <span className={styles.subtle}>候选版本 · 应用后进入草稿</span>
        <Link href={`/workflow/${state.session.workflow_id}`}>
          <Settings2 size={15} />
          Agent 配置
        </Link>
      </div>
      <div className={styles.body}>
        <main className={styles.main}>
          <div className={styles.versionbar}>
            <label>
              候选版本{" "}
              <select
                value={candidateId}
                aria-label="候选版本"
                onChange={(e) => setCandidateId(e.target.value)}
              >
                <option value="">当前草稿</option>
                {state.candidates.map((c, i) => (
                  <option key={c.id} value={c.id}>
                    {state.candidates.length - i} · {c.name}
                  </option>
                ))}
              </select>
            </label>
            <div>
              <Button
                size="sm"
                variant="outline"
                disabled={locked || !candidateId || !suiteId}
                onClick={runTests}
              >
                <FlaskConical size={14} />
                运行文本仿真
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={locked || !candidateId || !suiteId}
                onClick={runTune}
              >
                <Sparkles size={14} />
                调优
              </Button>
            </div>
          </div>
          {tab === "canvas" && (
            <div className={styles.canvas}>
              {nodes.length ? (
                <ReactFlow
                  key={candidateId || "draft"}
                  nodes={nodes}
                  edges={edges}
                  nodeTypes={nodeTypes}
                  nodesConnectable={false}
                  nodesDraggable={false}
                  fitView
                  fitViewOptions={{ padding: 0.3 }}
                >
                  <Background color="#ddd" gap={18} />
                  <Controls showInteractive={false} />
                </ReactFlow>
              ) : (
                <div className={styles.empty}>
                  <Bot size={34} />
                  <h2>从一个需求，开始构建</h2>
                  <p>
                    描述业务目标，或上传话术、流程和测试资料。
                    <br />
                    Codex 会生成可检查的流程与测试用例。
                  </p>
                  <div className={styles.steps}>
                    <span>01 构建</span>
                    <span>02 仿真</span>
                    <span>03 调优</span>
                  </div>
                </div>
              )}
            </div>
          )}
          {tab === "tests" && (
            <div className={styles.tests}>
              <div className={styles.suitebar}>
                <label>
                  测试集{" "}
                  <select
                    aria-label="测试集"
                    value={suiteId}
                    onChange={(e) => {
                      setSuiteId(e.target.value);
                      setCaseIndex(0);
                    }}
                  >
                    {!state.suites.length && (
                      <option value="">等待生成测试集</option>
                    )}
                    {state.suites.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.payload.name}
                      </option>
                    ))}
                  </select>
                </label>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={locked}
                  onClick={() =>
                    setSuiteEditor(
                      JSON.stringify(
                        selectedSuite?.payload || {
                          name: "新测试集",
                          cases: [
                            {
                              name: "价格咨询",
                              user_messages: ["请问价格是多少？"],
                              assertions: [
                                { kind: "contains", value: "咨询免费" },
                              ],
                            },
                          ],
                        },
                        null,
                        2,
                      ),
                    )
                  }
                >
                  编辑测试集
                </Button>
              </div>
              <p className={styles.testHelp}>
                固定用户脚本 · 真实文本引擎 · 业务工具使用测试夹具
              </p>
              {evaluation ? (
                <>
                  <div className={styles.metrics}>
                    <b>
                      {evaluation.summary.passed || 0}
                      <small> / {evaluation.summary.total || 0} 通过</small>
                    </b>
                    <span>
                      {statusText[evaluation.status] || evaluation.status}
                    </span>
                    <span>
                      失败 {evaluation.summary.failed || 0} · 错误{" "}
                      {evaluation.summary.error || 0}
                    </span>
                  </div>
                  <div className={styles.results}>
                    <div className={styles.caseList}>
                      {evaluation.results.map((c, i) => (
                        <button
                          key={i}
                          className={caseIndex === i ? styles.selected : ""}
                          onClick={() => setCaseIndex(i)}
                        >
                          <span>{c.name}</span>
                          <small data-status={c.status}>
                            {statusText[c.status] || c.status}
                          </small>
                        </button>
                      ))}
                    </div>
                    <div className={styles.transcript}>
                      {currentCase?.error && (
                        <p role="alert">{currentCase.error}</p>
                      )}
                      {currentCase?.transcript.map((m, i) => (
                        <div
                          key={i}
                          className={
                            m.role === "user"
                              ? styles.userBubble
                              : styles.botBubble
                          }
                        >
                          <small>
                            {m.role === "user" ? "模拟用户" : "Agent"}
                          </small>
                          <p>{m.text || "（无文本回复）"}</p>
                        </div>
                      ))}
                      <div className={styles.assertions}>
                        {currentCase?.assertions.map((a, i) => (
                          <p
                            key={i}
                            data-status={a.passed ? "passed" : "failed"}
                          >
                            {a.passed ? "✓" : "×"} {a.kind} ·{" "}
                            {String(a.value ?? "")}
                          </p>
                        ))}
                      </div>
                    </div>
                  </div>
                </>
              ) : (
                <div className={styles.empty}>
                  <FlaskConical size={32} />
                  <h2>验证 Agent 的实际表现</h2>
                  <p>选择候选版本和测试集，运行后可查看逐轮对话与断言结果。</p>
                </div>
              )}
            </div>
          )}
          {tab === "tuning" && (
            <div className={styles.tuning}>
              <h2>每次调优都有依据</h2>
              <p className={styles.subtle}>
                保留测试集，用相同场景比较修改前后；不会自动发布。
              </p>
              {state.jobs
                .filter((j) => j.kind === "tune")
                .map((j) => (
                  <article key={j.id}>
                    <div>
                      <strong>调优任务</strong>
                      <span>{statusText[j.status] || j.status}</span>
                    </div>
                    <p>
                      {j.error ||
                        (j.output.improved
                          ? "已找到通过更多用例且无回退的候选版本。"
                          : j.status === "completed"
                            ? "本轮没有找到更好的候选版本，保留原结果。"
                            : "正在分析失败案例并验证修改。")}
                    </p>
                    {typeof j.output.candidate_id === "string" && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setCandidateId(String(j.output.candidate_id));
                          setTab("tests");
                        }}
                      >
                        查看推荐版本
                      </Button>
                    )}
                  </article>
                ))}
              {!state.jobs.some((j) => j.kind === "tune") && (
                <div className={styles.empty}>
                  <Sparkles size={30} />
                  <p>先完成一次文本仿真，再根据失败案例调优。</p>
                </div>
              )}
            </div>
          )}
          {candidate && (
            <div className={styles.candidateSummary}>
              <Sparkles size={15} />
              <p>{candidate.summary}</p>
              <details>
                <summary>查看代码</summary>
                <pre>{candidate.code}</pre>
              </details>
            </div>
          )}
        </main>
        <aside
          className={styles.assistant}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            void addFiles(Array.from(e.dataTransfer.files));
          }}
        >
          <div className={styles.assistantTitle}>
            <span className={styles.botIcon}>
              <Bot size={20} />
            </span>
            <div>
              <strong>构建助手</strong>
              <small>Codex · 使用 Dograh 模型连接</small>
            </div>
            <span className={styles.online}>服务已连接</span>
          </div>
          <div className={styles.protocol}>
            <label htmlFor="model-protocol">构建模型协议</label>
            <select
              id="model-protocol"
              value={state.session.model_protocol}
              disabled={locked}
              onChange={(e) =>
                action(async () => {
                  result(
                    await settings({
                      path: { session_id: session },
                      body: {
                        model_protocol: e.target.value as "chat" | "responses",
                      },
                    }),
                  );
                })
              }
            >
              <option value="chat">Chat Completions</option>
              <option value="responses">Responses</option>
            </select>
          </div>
          <div className={styles.messages}>
            {!messages.length && (
              <div className={styles.welcome}>
                <h2>你想构建什么 Agent？</h2>
                <p>
                  告诉我它服务谁、需要完成什么任务。已有资料也可以直接添加。
                </p>
                <button
                  onClick={() =>
                    setText(
                      "帮我构建一个中文预约助手，先了解需求，再确认方便的时间。",
                    )
                  }
                >
                  构建预约助手 <ArrowUp size={14} />
                </button>
                <button onClick={() => fileInput.current?.click()}>
                  根据附件中的话术构建 <Paperclip size={14} />
                </button>
              </div>
            )}
            {messages.map((e) => {
              const input = e.payload.input as
                | Record<string, unknown>
                | undefined;
              const isUser =
                e.type === "task.queued" || e.payload.role === "user";
              const content =
                e.type === "task.queued"
                  ? String(
                      input?.text ||
                        input?.instruction ||
                        (e.payload.kind === "evaluate"
                          ? "运行文本仿真"
                          : "调优 Agent"),
                    )
                  : e.type === "task.completed"
                    ? "任务已完成，可查看候选版本或测试结果。"
                    : e.type === "task.failed"
                      ? String(e.payload.error)
                      : e.type === "task.cancelled"
                        ? "任务已取消。"
                        : e.type === "candidate.adopted"
                          ? "候选版本已应用到草稿。"
                          : String(e.payload.text || "");
              return (
                <div
                  key={e.sequence}
                  className={isUser ? styles.userBubble : styles.botBubble}
                >
                  <small>
                    {isUser ? "你" : "构建助手"}
                    {e.type === "voice.transcript" ? " · 语音" : ""}
                  </small>
                  <p>{content}</p>
                  {Array.isArray(input?.asset_ids) &&
                    input.asset_ids.map((id) => (
                      <span key={String(id)} className={styles.fileTag}>
                        <Paperclip size={12} />
                        {state.assets.find((a) => a.id === id)?.name || "附件"}
                      </span>
                    ))}
                </div>
              );
            })}
            {active && (
              <div className={styles.taskStatus}>
                <Loader2 size={15} className="animate-spin" />
                <span>
                  {statusText[active.status]} ·{" "}
                  {String(
                    events.filter((e) => e.type === "tool.started").at(-1)
                      ?.payload.name || active.kind,
                  )}
                </span>
                <button
                  onClick={() =>
                    action(async () => {
                      result(
                        await cancel({
                          path: { session_id: session, job_id: active.id },
                        }),
                      );
                    })
                  }
                >
                  取消任务
                </button>
              </div>
            )}
            <div ref={bottom} />
          </div>
          <div className={styles.composer}>
            {(error || syncError) && (
              <div role="alert" className={styles.error}>
                {error || syncError}
                <button
                  onClick={() => {
                    setError("");
                    setSyncError("");
                  }}
                  aria-label="关闭错误"
                >
                  <X size={14} />
                </button>
              </div>
            )}
            {notice && (
              <p role="status" className={styles.notice}>
                {notice}
              </p>
            )}
            {voice.error && (
              <p role="alert" className={styles.error}>
                {voice.error}
              </p>
            )}
            {state.assets.length > 0 && (
              <details className={styles.assets}>
                <summary>资料附件 · 已选择 {selectedAssets.length}/5</summary>
                {state.assets.map((a) => (
                  <div key={a.id}>
                    <label>
                      <input
                        type="checkbox"
                        checked={selectedAssets.includes(a.id)}
                        disabled={locked || a.status !== "ready"}
                        onChange={(e) => {
                          if (e.target.checked && selectedAssets.length >= 5) {
                            setError("最多引用 5 个附件");
                            return;
                          }
                          setSelectedAssets((old) =>
                            e.target.checked
                              ? [...old, a.id]
                              : old.filter((id) => id !== a.id),
                          );
                        }}
                      />
                      <FileText size={13} />
                      {a.name}
                    </label>
                    {a.status === "failed" ? (
                      <button
                        disabled={busy}
                        title={a.error}
                        onClick={() =>
                          action(async () => {
                            result(
                              await retryAsset({
                                path: { session_id: session, asset_id: a.id },
                              }),
                            );
                          })
                        }
                      >
                        解析失败 · 重试
                      </button>
                    ) : (
                      <small>可引用</small>
                    )}
                  </div>
                ))}
              </details>
            )}
            <div className={styles.selectedFiles}>
              {selectedAssets.map((id) => (
                <span key={id}>
                  <Paperclip size={12} />
                  {state.assets.find((a) => a.id === id)?.name}
                  <button
                    aria-label="移除附件引用"
                    onClick={() =>
                      setSelectedAssets((old) => old.filter((i) => i !== id))
                    }
                  >
                    <X size={12} />
                  </button>
                </span>
              ))}
            </div>
            <Textarea
              aria-label="构建需求"
              placeholder="描述需求、修改意见，或拖入文件…"
              value={text}
              disabled={busy}
              onChange={(e) => setText(e.target.value)}
              onPaste={(e) => {
                if (e.clipboardData.files.length) {
                  e.preventDefault();
                  void addFiles(Array.from(e.clipboardData.files));
                }
              }}
            />
            <div className={styles.composerActions}>
              <input
                ref={fileInput}
                type="file"
                hidden
                multiple
                accept=".txt,.md,.csv,.json,.pdf,.docx,.xlsx,.png,.jpg,.jpeg,.webp"
                onChange={(e) => {
                  void addFiles(Array.from(e.target.files || []));
                  e.target.value = "";
                }}
              />
              <button
                aria-label="添加附件"
                disabled={locked}
                onClick={() => fileInput.current?.click()}
              >
                <Paperclip size={18} />
              </button>
              <button
                className={voice.status !== "off" ? styles.voiceActive : ""}
                onClick={() =>
                  voice.status === "off" ? void voice.start() : voice.stop()
                }
              >
                <Mic size={16} />
                {voice.status === "off"
                  ? "实时语音"
                  : voice.status === "connecting"
                    ? "连接中…"
                    : "结束语音"}
              </button>
              <Button
                size="sm"
                disabled={locked || !text.trim()}
                aria-label="发送构建需求"
                onClick={() =>
                  action(async () => {
                    result(
                      await instruct({
                        path: { session_id: session },
                        body: {
                          text: text.trim(),
                          candidate_id: candidateId || null,
                          asset_ids: selectedAssets,
                          idempotency_key: crypto.randomUUID(),
                        },
                      }),
                    );
                    setText("");
                    setSelectedAssets([]);
                    setCandidateId("");
                  })
                }
              >
                {busy ? (
                  <Loader2 size={17} className="animate-spin" />
                ) : (
                  <ArrowUp size={17} />
                )}
              </Button>
            </div>
            <small className={styles.composerHint}>
              {voice.status === "connected"
                ? "正在使用麦克风 · 可以随时打断讲话"
                : "支持文件、拖放与粘贴 · 每个文件最大 20 MiB"}
            </small>
          </div>
        </aside>
      </div>
      {suiteEditor !== null && (
        <div className={styles.modalBackdrop}>
          <section
            role="dialog"
            aria-modal="true"
            aria-label="编辑测试集"
            className={styles.modal}
          >
            <h2>编辑文本仿真测试集</h2>
            <p>保存为新版本；历史评测使用的测试集保持不变。</p>
            <Textarea
              aria-label="测试集 JSON"
              value={suiteEditor}
              onChange={(e) => setSuiteEditor(e.target.value)}
            />
            <div>
              <Button variant="outline" onClick={() => setSuiteEditor(null)}>
                取消
              </Button>
              <Button
                disabled={busy}
                onClick={() =>
                  action(async () => {
                    const saved = result<{ id: string }>(
                      await saveSuite({
                        path: { session_id: session },
                        body: JSON.parse(suiteEditor) as SimulationSuite,
                      }),
                    );
                    setSuiteId(saved.id);
                    setSuiteEditor(null);
                  })
                }
              >
                保存新版本
              </Button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
