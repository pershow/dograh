"use client";
import {
  ArrowUp,
  Check,
  ChevronDown,
  ChevronUp,
  FileText,
  FlaskConical,
  Info,
  Loader2,
  Maximize2,
  MessageSquare,
  Mic,
  Minimize2,
  Paperclip,
  Settings2,
  Sparkles,
  X,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  workbenchAdoptApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdAdoptPost as adopt,
  workbenchAgentRunsApiV1AgentWorkbenchSessionsSessionIdAgentRunsGet as getAgentRuns,
  workbenchCancelApiV1AgentWorkbenchSessionsSessionIdJobsJobIdCancelPost as cancel,
  workbenchCandidateApiV1AgentWorkbenchSessionsSessionIdCandidatesCandidateIdGet as getCandidate,
  workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet as getCapabilities,
  workbenchCreateSessionApiV1AgentWorkbenchSessionsPost as createSession,
  workbenchDependenciesApiV1AgentWorkbenchSessionsSessionIdDependenciesGet as getDependencies,
  workbenchEvaluateApiV1AgentWorkbenchSessionsSessionIdEvaluationsPost as evaluate,
  workbenchEventsApiV1AgentWorkbenchSessionsSessionIdEventsGet as getEvents,
  workbenchInstructionApiV1AgentWorkbenchSessionsSessionIdInstructionsPost as instruct,
  workbenchRetryAssetApiV1AgentWorkbenchSessionsSessionIdAssetsAssetIdRetryPost as retryAsset,
  workbenchSessionApiV1AgentWorkbenchSessionsSessionIdGet as getSession,
  workbenchSessionsApiV1AgentWorkbenchSessionsGet as getSessions,
  workbenchSettingsApiV1AgentWorkbenchSessionsSessionIdPatch as settings,
  workbenchSuiteApiV1AgentWorkbenchSessionsSessionIdSuitesPost as saveSuite,
  workbenchTuneApiV1AgentWorkbenchSessionsSessionIdTunePost as tune,
  workbenchUploadApiV1AgentWorkbenchSessionsSessionIdAssetsPost as upload,
} from "@/client/sdk.gen";
import type { SimulationSuite } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";
import { referencedVariables } from "@/lib/workflowVariables";

import { conversationRows } from "./activity";
import { AgentMemory } from "./AgentMemory";
import { BuildModelSettings } from "./BuildModelSettings";
import { EvaluationArchive } from "./EvaluationArchive";
import { EvidenceReads } from "./EvidenceReads";
import { pollRequest } from "./pollRequest";
import { ReviewCounts, ReviewFindings } from "./ReviewFindings";
import { SimulationEvidence } from "./SimulationEvidence";
import { TaskActivity } from "./TaskActivity";
import { type BasisSummary,RequirementCoverage, TaskBasis } from "./TaskBasis";
import { modelProgressText, taskCompletionText, taskProgress, workbenchErrorText } from "./taskProgress";
import type {
  Candidate,
  Evaluation,
  EventPage,
  Snapshot,
  WorkbenchEvent,
  WorkbenchSession,
  WorkbenchSessionListItem,
  WorkbenchState,
} from "./types";
import { useWorkbenchVoice } from "./useWorkbenchVoice";
import styles from "./workbench.module.css";
import { WorkbenchMarkdown } from "./WorkbenchMarkdown";

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

function mergeEvents(previous: WorkbenchEvent[], incoming: WorkbenchEvent[]) {
  const seen = new Set(previous.map(event => event.sequence));
  const added = incoming.filter(event => !seen.has(event.sequence));
  return added.length ? [...previous, ...added].sort((a, b) => a.sequence - b.sequence) : previous;
}

export function AgentWorkbench({
  workflowId,
  sessionId,
  onPreviewChange,
  onSessionReady,
  onAdopted,
  onTargetChange,
  onClose,
  expanded,
  onExpandedChange,
  adoptDisabledReason,
}: {
  workflowId?: number;
  sessionId?: string;
  onPreviewChange?: (candidate: Candidate | undefined) => void;
  onSessionReady?: (session: WorkbenchSession) => void;
  onAdopted?: () => void;
  onTargetChange?: (snapshot: Snapshot, canReload: boolean) => void;
  onClose?: () => void;
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  adoptDisabledReason?: string;
}) {
  const {
    user,
    loading: authLoading,
    getAccessToken,
    redirectToLogin,
  } = useAuth();
  const router = useRouter();
  const [state, setState] = useState<WorkbenchState>();
  const [session, setSession] = useState<string>("");
  const [events, setEvents] = useState<WorkbenchEvent[]>([]);
  const [historyBefore, setHistoryBefore] = useState<number | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState("");
  const [sessionsOpen, setSessionsOpen] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessions, setSessions] = useState<WorkbenchSessionListItem[]>([]);
  const [sessionsError, setSessionsError] = useState("");
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
  const [instructionMode, setInstructionMode] = useState<"auto" | "analyze" | "build" | "test">("auto");
  const [testClarification, setTestClarification] = useState("");
  const [testMode, setTestMode] = useState<"dynamic" | "single_step" | "scripted">("dynamic");
  const followedTestJob = useRef("");
  const [runsOpen, setRunsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [buildModelRevision, setBuildModelRevision] = useState(0);
  const [runsLoading, setRunsLoading] = useState(false);
  const [agentRuns, setAgentRuns] = useState<{
    id: number; created_at: string; state: string; definition_id: number | null;
    has_transcript: boolean;
  }[]>([]);
  const [dependencies, setDependencies] = useState<{
    ready: boolean;
    issues: unknown[];
    services: {
      role: string;
      provider: string;
      model: string;
      endpoint_host?: string;
    }[];
  }>();
  const [suiteEditor, setSuiteEditor] = useState<string | null>(null);
  const [caseName, setCaseName] = useState("");
  const [evaluationId, setEvaluationId] = useState("");
  const [composerOpen, setComposerOpen] = useState(false);
  const [followingMessages, setFollowingMessages] = useState(true);
  const followMessages = useRef(true);
  const fileInput = useRef<HTMLInputElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const resultsPane = useRef<HTMLElement>(null);
  const resultDetail = useRef<HTMLDivElement>(null);
  const openResultRequested = useRef(false);
  const scrollToResult = useCallback(() => {
    const pane = resultsPane.current;
    const detail = resultDetail.current;
    if (pane && detail) {
      pane.scrollTop += detail.getBoundingClientRect().top - pane.getBoundingClientRect().top;
      detail.focus({preventScroll: true});
    }
  }, []);
  const bottom = useRef<HTMLDivElement>(null);
  const messagesPane = useRef<HTMLDivElement>(null);
  const historyInFlight = useRef(false);
  const historyAnchor = useRef<{ sequence: string; top: number } | null>(null);
  const cursor = useRef<number | undefined>(undefined);
  const completedCursor = useRef(0);
  const revision = useRef<string | undefined>(undefined);
  const lastStateSync = useRef(0);
  const init = useRef<Promise<WorkbenchSession> | null>(null);
  const inFlight = useRef(false);
  const active = state?.jobs.find((j) =>
    ["queued", "running"].includes(j.status),
  );
  const locked = busy || !!active;
  const reload = useCallback(async (force = true) => {
    if (!session || inFlight.current) return;
    inFlight.current = true;
    try {
      const initial = cursor.current === undefined;
      const fresh: WorkbenchEvent[] = [];
      let more = false;
      do {
        const previousCursor = cursor.current;
        const page = result<EventPage>(await pollRequest(signal => getEvents({
          signal, path: { session_id: session },
          query: { after: cursor.current, limit: previousCursor === undefined ? 60 : 200 },
        })));
        cursor.current = page.after ?? page.events.at(-1)?.sequence ?? cursor.current ?? 0;
        if (previousCursor === undefined) {
          setHistoryBefore(page.before ?? null);
          completedCursor.current = page.events[0] ? page.events[0].sequence - 1 : cursor.current;
        }
        fresh.push(...page.events);
        if (page.events.length) setEvents((old) => mergeEvents(old, page.events));
        // Drain a background-tab backlog immediately instead of one page per
        // throttled timer tick. Older history still loads only on demand.
        more = !!page.has_more && cursor.current !== previousCursor;
      } while (more);
      // Progress tokens update the conversation, not the entire graph/test archive.
      if (force || initial || Date.now() - lastStateSync.current > 30000 ||
          fresh.some(e => e.type !== "evaluation.case.progress" && /^(task\.|candidate\.|suite\.|evaluation\.|asset\.|memory\.)/.test(e.type))) {
        const snapshot = result<WorkbenchState & { unchanged?: boolean }>(await pollRequest(signal => getSession({
          signal, path: { session_id: session }, query: { if_revision: revision.current },
        })));
        revision.current = snapshot.revision;
        lastStateSync.current = Date.now();
        if (!snapshot.unchanged) setState(snapshot);
      }
      setSyncError("");
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : "无法同步工作台");
    } finally {
      inFlight.current = false;
    }
  }, [session]);
  const loadOlder = useCallback(async () => {
    if (!session || historyBefore === null || historyInFlight.current) return;
    historyInFlight.current = true;
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const page = result<EventPage>(await getEvents({
        path: { session_id: session }, query: { before: historyBefore, limit: 60 },
      }));
      const pane = messagesPane.current;
      const visible = pane && Array.from(pane.querySelectorAll<HTMLElement>("[data-event-sequence]"))
        .find(el => el.getBoundingClientRect().bottom > pane.getBoundingClientRect().top);
      historyAnchor.current = visible ? {
        sequence: visible.dataset.eventSequence!, top: visible.getBoundingClientRect().top,
      } : null;
      followMessages.current = false;
      setFollowingMessages(false);
      setEvents(old => mergeEvents(old, page.events));
      setHistoryBefore(page.before ?? null);
    } catch (error) {
      setHistoryError(error instanceof Error ? error.message : "历史记录加载失败");
    } finally {
      historyInFlight.current = false;
      setHistoryLoading(false);
    }
  }, [session, historyBefore]);
  useLayoutEffect(() => {
    const pane = messagesPane.current;
    const anchor = historyAnchor.current;
    if (pane && anchor) {
      const el = pane.querySelector<HTMLElement>(`[data-event-sequence="${anchor.sequence}"]`);
      if (el) pane.scrollTop += el.getBoundingClientRect().top - anchor.top;
      historyAnchor.current = null;
    }
  }, [events]);
  const voice = useWorkbenchVoice(
    session || undefined,
    getAccessToken,
    selectedAssets,
    candidateId,
    suiteId,
    reload,
  );
  useEffect(() => {
    if (state)
      onTargetChange?.(
        state.target,
        !text.trim() && voice.status === "off" && !busy,
      );
  }, [state, text, voice.status, busy, onTargetChange]);
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
        if (workflowId && value.workflow_id !== workflowId)
          throw new Error("此工作台会话不属于当前 Agent。");
        setSession(value.id);
        if (onPreviewChange) onSessionReady?.(value);
        else
          router.replace(
            `/workflow/${value.workflow_id}?workbench=1&session=${value.id}`,
          );
      })
      .catch((error) => {
        if (!disposed) setError(error.message);
      });
    return () => {
      disposed = true;
    };
  }, [
    authLoading,
    user,
    workflowId,
    sessionId,
    redirectToLogin,
    router,
    onPreviewChange,
    onSessionReady,
  ]);
  useEffect(() => {
    if (authLoading || !user || !session) return;
    void reload();
    const interval = setInterval(() => void reload(false), 2000);
    const resume = () => { if (document.visibilityState === "visible") void reload(false); };
    document.addEventListener("visibilitychange", resume);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [authLoading, user, session, reload]);
  const sessionWorkflowId = state?.session.workflow_id;
  useEffect(() => {
    if (!sessionsOpen || authLoading || !user || !sessionWorkflowId) return;
    let disposed = false;
    let pending = false;
    setSessions([]);
    setSessionsLoading(true);
    setSessionsError("");
    const refresh = async () => {
      if (disposed || pending || document.visibilityState === "hidden") return;
      pending = true;
      try {
        const response = await pollRequest(signal => getSessions({
          signal, query: {workflow_id: sessionWorkflowId},
        }));
        const data = result<{sessions: WorkbenchSessionListItem[]}>(response);
        if (!disposed) {
          setSessions([...data.sessions].sort((a, b) => Number(!!b.active_job_status) - Number(!!a.active_job_status)));
          setSessionsError("");
        }
      } catch {
        if (!disposed) setSessionsError("会话状态更新失败，显示的状态可能已过期，正在重试。");
      } finally {
        pending = false;
        if (!disposed) setSessionsLoading(false);
      }
    };
    void refresh();
    const interval = setInterval(() => void refresh(), 3000);
    const resume = () => { if (document.visibilityState === "visible") void refresh(); };
    document.addEventListener("visibilitychange", resume);
    return () => {
      disposed = true;
      clearInterval(interval);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [sessionsOpen, authLoading, user, sessionWorkflowId]);
  const choiceInitialized = useRef(false);
  useEffect(() => {
    if (authLoading || !user || !session) return;
    let disposed = false;
    getDependencies({
      path: { session_id: session },
      query: { candidate_id: candidateId || undefined, purpose: "build" },
    })
      .then((response) => {
        if (!disposed) setDependencies(result(response));
      })
      .catch((error) => {
        if (!disposed)
          setDependencies({
            ready: false,
            issues: [error.message],
            services: [],
          });
      });
    return () => {
      disposed = true;
    };
  }, [authLoading, user, session, candidateId, settingsOpen, buildModelRevision]);
  useEffect(() => {
    if (!choiceInitialized.current && state) {
      choiceInitialized.current = true;
      if (state.candidates[0]) setCandidateId(state.candidates[0].id);
      // Uploaded materials remain available on demand; old focus is not resent.
    }
  }, [state]);
  useEffect(() => {
    if (!suiteId && state?.suites[0]) setSuiteId(state.suites[0].id);
  }, [state, suiteId]);
  useEffect(() => {
    const active = state?.jobs.find(j => j.status === "running" && Array.isArray(j.output.suite_ids) && j.output.suite_ids.length);
    if (!active || followedTestJob.current === active.id) return;
    followedTestJob.current = active.id;
    setSuiteId((active.output.suite_ids as string[])[0]);
    if (typeof active.output.candidate_id === "string") setCandidateId(active.output.candidate_id);
    setCaseName(""); setEvaluationId("");
  }, [state]);
  useEffect(() => {
    if (!candidateId || !session) {
      setCandidate(undefined);
      return;
    }
    let disposed = false;
    setCandidate(undefined);
    getCandidate({ path: { session_id: session, candidate_id: candidateId } })
      .then((r) => {
        if (!disposed) setCandidate(result<Candidate>(r));
      })
      .catch((e) => {
        if (!disposed) setError(e.message);
      });
    return () => {
      disposed = true;
    };
  }, [candidateId, session]);
  useEffect(() => {
    const completed = events
      .filter(
        (e) =>
          ["task.completed", "task.needs_attention", "task.awaiting_input", "candidate.adopted"].includes(e.type) &&
          e.sequence > completedCursor.current,
      )
      .at(-1);
    if (!completed) return;
    completedCursor.current = completed.sequence;
    if (completed.type === "candidate.adopted") {
      choiceInitialized.current = true;
      setCandidateId("");
      return;
    }
    const output = completed.payload.output as Record<string, unknown>;
    if (typeof output?.candidate_id === "string") {
      choiceInitialized.current = true;
      setCandidateId(output.candidate_id);
    }
    if (typeof output?.suite_id === "string") setSuiteId(output.suite_id);
  }, [events]);
  useEffect(() => {
    if (tab === "canvas" && followMessages.current)
      bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [events.length, tab]);
  useEffect(() => {
    if (resultsPane.current && !openResultRequested.current) resultsPane.current.scrollTop = 0;
  }, [tab, candidateId, suiteId]);
  useEffect(() => {
    if (tab === "tests" && openResultRequested.current && resultDetail.current) {
      scrollToResult();
      openResultRequested.current = false;
    }
  }, [tab, candidateId, suiteId, evaluationId, caseName, scrollToResult]);
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
  useEffect(() => {
    onPreviewChange?.(candidate?.id === candidateId ? candidate : undefined);
  }, [candidate, candidateId, onPreviewChange]);
  const selectedSuite = state?.suites.find((s) => s.id === suiteId);
  const evaluations =
    state?.evaluations.filter(
      (e) => e.candidate_id === candidateId && e.suite_id === suiteId,
    ) || [];
  const evaluation = evaluations.find(e => e.id === evaluationId) || evaluations[0];
  const testTask = state?.jobs.find(j => j.id === evaluation?.job_id) || state?.jobs.find(j =>
    (j.output.intent === "test" || j.output.outcome === "test") &&
    j.output.candidate_id === candidateId &&
    Array.isArray(j.output.suite_ids) && j.output.suite_ids.includes(suiteId));
  const testTaskSummary = testTask?.output.summary as Record<string, number> | undefined;
  const tuningTask = testTask?.output.outcome === "test" && testTask.output.candidate_id === candidateId ? testTask : undefined;
  const tuningSuiteIds = tuningTask?.output.suite_ids as string[] | undefined;
  const tuningCaseCount = tuningTask && typeof testTaskSummary?.total === "number"
    ? testTaskSummary.total - (Array.isArray(tuningTask.output.import_issues) ? tuningTask.output.import_issues.length : 0)
    : tuningSuiteIds?.reduce((total, id) => total + (state?.suites.find(s => s.id === id)?.payload.cases.length || 0), 0);
  const inFlightTests = testTask ? (state?.evaluations || [])
    .filter(e => e.job_id === testTask.id)
    .flatMap(e => e.results).filter(r => r.status === "running").length : 0;
  const currentCase = evaluation?.results.find(result => result.name === caseName) || evaluation?.results[0];
  const openTestResult = (selected: Evaluation, name: string) => {
    if (evaluationId === selected.id && caseName === name && tab === "tests") {
      scrollToResult();
      return;
    }
    choiceInitialized.current = true;
    openResultRequested.current = true;
    setCandidateId(selected.candidate_id);
    setSuiteId(selected.suite_id);
    setEvaluationId(selected.id);
    setCaseName(name);
    setTab("tests");
  };
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
            ...(tuningTask ? {test_job_id: tuningTask.id} : {suite_id: suiteId}),
            idempotency_key: crypto.randomUUID(),
            instruction: text || "修复失败用例并保留已通过行为。",
            asset_ids: selectedAssets,
            max_rounds: 1,
          },
        }),
      );
      setTab("tuning");
    });
  const sendInstruction = (instruction: string, mode = instructionMode) =>
    action(async () => {
      result(await instruct({
        path: { session_id: session },
        body: {
          text: instruction, mode, candidate_id: candidateId || null,
          test_mode: mode === "test" ? testMode : null,
          asset_ids: selectedAssets, idempotency_key: crypto.randomUUID(),
        },
      }));
      setText("");
      setSelectedAssets([]);
      setTab("canvas");
      setRunsOpen(false);
    });
  const openRuns = async (open: boolean) => {
    setRunsOpen(open);
    if (!open || authLoading || !user || !session) return;
    setRunsLoading(true);
    try {
      const data = result<{ runs: typeof agentRuns }>(
        await getAgentRuns({ path: { session_id: session } }),
      );
      setAgentRuns(data.runs);
    } catch (error) {
      setError(error instanceof Error ? error.message : "运行记录读取失败");
    } finally {
      setRunsLoading(false);
    }
  };
  const composerExpanded =
    tab === "canvas" || composerOpen || !!text || voice.status !== "off";
  const messages = conversationRows(events, state?.jobs || []);
  if (!state || !onPreviewChange)
    return (
      <div className={styles.loading}>
        {error ? (
          <p role="alert">{workbenchErrorText(error)}</p>
        ) : (
          <>
            <Loader2 className="animate-spin" />
            <p>正在准备工作台…</p>
          </>
        )}
        {onClose && (
          <Button variant="ghost" onClick={onClose}>
            关闭工作台
          </Button>
        )}
        <Link href="/workflow">返回 Agents</Link>
      </div>
    );
  return (
    <div className={styles.workbench}>
      <header className={styles.header}>
        <Sparkles size={18} />
        <strong>Agent 工作台</strong>
        <div className={styles.headerActions}>
          <Popover open={sessionsOpen} onOpenChange={setSessionsOpen}>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="工作台会话" title="工作台会话">
                <MessageSquare size={16} />
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className={styles.popover} aria-label="工作台会话">
              <h3>此 Agent 的会话</h3>
              <p className={styles.subtle}>共用 Agent 草稿和长期记忆，聊天分别保留，历史用户原文与资料可跨会话查回。切换前请发送或清空输入。</p>
              {sessionsLoading && <p>正在加载会话…</p>}
              {sessionsError && <p role="alert">{sessionsError}</p>}
              {!!sessions.length && <p className={styles.subtle}>运行中 {sessions.filter(item => item.active_job_status === "running").length} · 排队中 {sessions.filter(item => item.active_job_status === "queued").length}</p>}
              <div className={styles.sessionList}>
                {sessions.map(item => <Button key={item.id} variant={item.id === session ? "secondary" : "ghost"}
                  className={styles.sessionRow}
                  disabled={locked || !!text.trim() || voice.status !== "off"}
                  onClick={() => {
                    setSessionsOpen(false);
                    if (item.id !== session) {
                      onPreviewChange?.(undefined);
                      router.replace(`/workflow/${state.session.workflow_id}?workbench=1&session=${item.id}`);
                    }
                  }}>
                  <span className={styles.sessionLabel}>
                    <span>{item.id === session ? "当前 · " : ""}{new Date(item.created_at).toLocaleString("zh-CN")}</span>
                    <span className={styles.sessionMeta}>{item.id.slice(0, 6)}</span>
                  </span>
                  <span className={styles.sessionStatus} data-status={item.active_job_status || "idle"}>
                    {item.active_job_status === "running" && <Loader2 size={12} className="animate-spin" aria-hidden="true" />}
                    {item.active_job_status === "running" ? "运行中" : item.active_job_status === "queued" ? "排队中" : "未运行"}
                  </span>
                </Button>)}
              </div>
              <Button variant="outline" disabled={locked || !!text.trim() || voice.status !== "off"} onClick={() => action(async () => {
                const next = result<WorkbenchSession>(await createSession({body: {
                  workflow_id: state.session.workflow_id, model_protocol: state.session.model_protocol, new_session: true,
                }}));
                onPreviewChange?.(undefined);
                router.replace(`/workflow/${state.session.workflow_id}?workbench=1&session=${next.id}`);
              })}>新建会话</Button>
            </PopoverContent>
          </Popover>
          <AgentMemory key={session} session={session} revision={state.memory_revision} onChanged={() => void reload()} />
          <Popover open={settingsOpen} onOpenChange={setSettingsOpen}>
            <PopoverTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="工作台设置"
                title="工作台设置"
              >
                <Settings2 size={16} />
                {dependencies && !dependencies.ready && (
                  <span className={styles.warningDot} />
                )}
              </Button>
            </PopoverTrigger>
            <PopoverContent
              align="end"
              className={styles.popover}
              aria-label="工作台设置"
            >
              <h3>工作台设置</h3>
              <BuildModelSettings disabled={locked} onSaved={() => setBuildModelRevision(revision => revision + 1)} />
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
                            model_protocol: e.target.value as
                              | "chat"
                              | "responses",
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
              {dependencies && (
                <details className={styles.protocol}>
                  <summary>
                    {dependencies.ready ? "模型连接已检查" : "模型配置需要处理"}
                  </summary>
                  {dependencies.services.map((service) => (
                    <p key={service.role}>
                      {service.role.toUpperCase()} · {service.provider} /{" "}
                      {service.model}
                      {service.endpoint_host
                        ? ` · ${service.endpoint_host}`
                        : ""}
                    </p>
                  ))}
                  {dependencies.issues.map((issue, i) => (
                    <p role="alert" key={i}>
                      {workbenchErrorText(detailFromError(
                        { detail: issue },
                        "模型配置无法解析，请检查 Models",
                      ))}
                    </p>
                  ))}
                  <Link href="/model-configurations">管理模型连接</Link>
                </details>
              )}
            </PopoverContent>
          </Popover>
          {onExpandedChange && (
            <Button
              variant="ghost"
              size="icon"
              aria-label={expanded ? "收起工作台" : "展开工作台"}
              title={expanded ? "收起工作台" : "展开工作台"}
              onClick={() => onExpandedChange(!expanded)}
            >
              {expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            aria-label="关闭工作台"
            title="关闭工作台"
            onClick={onClose}
          >
            <X size={18} />
          </Button>
        </div>
      </header>
      <div className={styles.toolbar}>
        <nav aria-label="工作台功能">
          {[
            ["canvas", "构建"],
            ["tests", "文本仿真"],
            ["tuning", "调优"],
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
      </div>
      <div className={styles.versionbar}>
        <label>
          画布版本
          <select
            value={candidateId}
            aria-label="候选版本"
            onChange={(e) => { setCandidateId(e.target.value); setEvaluationId(""); setCaseName(""); }}
          >
            <option value="">当前草稿</option>
            {state.candidates.map((c, i) => (
              <option key={c.id} value={c.id}>
                {state.candidates.length - i} · {c.name}
              </option>
            ))}
          </select>
        </label>
        {candidate && (
          <Popover>
            <PopoverTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="候选版本说明"
                title="候选版本说明 · 只读预览"
              >
                <Info size={16} />
              </Button>
            </PopoverTrigger>
            <PopoverContent
              align="end"
              className={`${styles.popover} ${styles.candidateSummary}`}
              aria-label="候选版本说明"
            >
              <h3>候选版本说明 · 只读预览</h3>
              <p>{candidate.summary}</p>
              {candidate.snapshot && (
                <details>
                  <summary>Variables · 应用时随候选写入草稿</summary>
                  <p>
                    默认值用于仿真和编辑器测试；空值需要补充，或在每次通话中传入。
                  </p>
                  <dl>
                    {Object.entries({
                      ...Object.fromEntries(
                        referencedVariables(
                          candidate.snapshot.workflow_json.nodes,
                          candidate.snapshot.workflow_json.edges,
                        ).map((name) => [name, ""]),
                      ),
                      ...candidate.snapshot.template_context_variables,
                    }).map(([name, value]) => (
                      <div key={name} className="mb-2">
                        <dt className="font-mono">{name}</dt>
                        <dd className="whitespace-pre-wrap break-words">
                          {String(value ?? "").trim() ||
                            "待填写 / 每次通话传入"}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </details>
              )}
              <details>
                <summary>查看代码</summary>
                <pre>{candidate.code}</pre>
              </details>
            </PopoverContent>
          </Popover>
        )}

        <Button
          size="sm"
          variant="outline"
          disabled={
            !candidate ||
            candidate.id !== candidateId ||
            locked ||
            !!adoptDisabledReason
          }
          title={adoptDisabledReason}
          onClick={() =>
            action(async () => {
              result(
                await adopt({
                  path: { session_id: session, candidate_id: candidateId },
                }),
              );
              setCandidateId("");
              setCandidate(undefined);
              onPreviewChange(undefined);
              setNotice("已应用到草稿，可继续编辑、测试和发布。");
              onAdopted?.();
            })
          }
        >
          <Check size={14} />
          应用到草稿
        </Button>
      </div>
      {adoptDisabledReason && (
        <p className={styles.testHelp}>{adoptDisabledReason}</p>
      )}
      {active && (
        <div role="status" className={styles.taskStatus}>
          <Loader2 size={15} className="animate-spin" />
          <span>{taskProgress(active, events)}</span>
          {!!state.evaluations.length && <button onClick={() => {
            setTab("tests");
            if (resultsPane.current) resultsPane.current.scrollTop = 0;
          }}>查看已保存结果</button>}
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
      <div className={styles.body}>
        {tab !== "canvas" && (
          <main
            ref={resultsPane}
            className={styles.main}
            aria-label={tab === "tests" ? "文本仿真结果" : "调优记录"}
          >
            {tab === "tests" && <EvaluationArchive state={state} onOpen={openTestResult} />}
            <div className={styles.runActions}>
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
                开始调优
              </Button>
              <span className={styles.subtle}>调优范围：{tuningSuiteIds ? `完整测试任务 · ${tuningSuiteIds.length} 批 / ${tuningCaseCount} 条` : `所选测试集 · ${selectedSuite?.payload.cases.length || 0} 条`}。仅生成候选，应用前不会修改 Agent。</span>
              <select aria-label="重跑测试方式" className={styles.intentSelect} value={testMode} disabled={locked} onChange={e => setTestMode(e.target.value as typeof testMode)}>
                <option value="dynamic">动态客户仿真</option>
                <option value="single_step">真实上下文单步</option>
                <option value="scripted">固定脚本与错位检查</option>
              </select>
              <Button size="sm" variant="outline" disabled={locked || !candidateId || !suiteId} onClick={() => void action(async () => {
                result(await instruct({path: {session_id: session}, body: {mode: "test", test_mode: testMode, test_suite_ids: [suiteId], candidate_id: candidateId, text: "使用所选测试方式重新执行此测试集，检查对话有效性，按原始资料评审。保留 Agent 和对话模型。", idempotency_key: crypto.randomUUID()}}));
              })}>按所选方式重跑</Button>
              {(!candidateId || !suiteId) && (
                <p className={styles.subtle}>请先选择候选版本和测试集。</p>
              )}
            </div>
            <div className={styles.suitebar}>
              <label>
                测试集{" "}
                <select
                  aria-label="测试集"
                  value={suiteId}
                  onChange={(e) => {
                    setSuiteId(e.target.value);
                    setCaseName(""); setEvaluationId("");
                  }}
                >
                  {!state.suites.length && (
                    <option value="">等待生成测试集</option>
                  )}
                  {state.suites.map((s, index) => (
                    <option key={s.id} value={s.id}>
                      {state.suites.length - index} · {s.payload.name}
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
            {tab === "tests" && (
              <div className={styles.tests}>
                <p className={styles.testHelp}>
                  真实文本引擎 · 动态客户 / 原始上下文单步 / 固定脚本 · 独立对话有效性检查
                </p>
                {testTaskSummary && <section className={styles.candidateResult} aria-label="本次测试汇总">
                  <strong>{testTask?.output.resumed_from_job_id ? "恢复任务：已汇总" : "本次任务：已汇总实际运行"} {testTaskSummary.executed}/{testTaskSummary.total} 条</strong>
                  {inFlightTests > 0 && <p>另有 {inFlightTests} 条正在对话或评审，完成后纳入上方汇总。</p>}
                  <p>通过 {testTaskSummary.passed} · 未通过 {testTaskSummary.failed} · 执行错误 {testTaskSummary.error} · 无法判定 {testTaskSummary.inconclusive}</p>
                  {testTaskSummary.valid !== undefined && <p>对话有效 {testTaskSummary.valid} · 无效 {testTaskSummary.invalid || 0} · 有效性未确定 {testTaskSummary.validity_inconclusive || 0}。无效测试不计入正常流程通过率。</p>}
                  {Number(testTaskSummary.reused) > 0 && <p>其中 {testTaskSummary.reused} 条保留原实际对话；补评不重复运行 Agent。</p>}
                  <ReviewCounts summary={testTaskSummary} />
                  <p className={styles.subtle}>下方显示当前批次的实际回复与检查依据。参考对话由模型逐项评审，保留原文供核对。</p>
                </section>}
                {evaluation ? (
                  <>
                    <div className={styles.metrics}>
                      <b>
                        {evaluation.summary.passed || 0}
                        <small> / {evaluation.summary.valid ?? evaluation.summary.total ?? 0} {evaluation.summary.valid !== undefined ? "有效测试通过" : "通过"}</small>
                      </b>
                      <span>
                        {statusText[evaluation.status] || evaluation.status}
                      </span>
                      <span>
                        当前批次：失败 {evaluation.summary.failed || 0} · 错误{" "}
                        {evaluation.summary.error || 0} · 无法判定 {evaluation.summary.inconclusive || 0}
                      </span>
                    </div>
                    <div className={styles.results} ref={resultDetail} tabIndex={-1} role="region" aria-label="测试用例详情">
                      <p className={styles.testHelp}>当前详情：{selectedSuite?.payload.name} · 测试记录 {evaluation.id.slice(0, 8)}</p>
                      <label className={styles.caseSelector}>
                        测试场景
                        <select
                          aria-label="测试场景"
                          value={currentCase ? evaluation.results.indexOf(currentCase) : ""}
                          onChange={(e) => setCaseName(evaluation.results[Number(e.target.value)]?.name || "")}
                        >
                          {evaluation.results.map((c, i) => (
                            <option key={c.name} value={i}>
                              {i + 1}. {c.name} ·{" "}
                              {statusText[c.status] || c.status}
                            </option>
                          ))}
                        </select>
                      </label>
                      <div
                        className={styles.transcript}
                        role="region"
                        aria-label="仿真对话与断言"
                        tabIndex={0}
                      >
                        {currentCase?.error && (
                          <p role="alert">{workbenchErrorText(currentCase.error)}</p>
                        )}
                        {currentCase?.run_id && <p className={styles.subtle}>运行记录 #{currentCase.run_id}{currentCase.source?.row ? ` · ${currentCase.source.sheet} 第 ${currentCase.source.row} 行 · ${currentCase.source.column}` : ""}</p>}
                        {currentCase?.trace_url && /^https?:\/\//i.test(currentCase.trace_url) && <p className={styles.subtle}><a href={currentCase.trace_url} target="_blank" rel="noreferrer">查看 Langfuse 调用链</a></p>}
                        {currentCase?.run_id && <p className={styles.subtle}>测试运行单独保存在这里，不计入普通通话记录和业务统计。</p>}
                        {currentCase && <SimulationEvidence result={currentCase} />}
                        {currentCase?.reference_review && <p className={styles.subtle}>参考对话检查：{/no data for|timed out|I\/O timeout/.test(currentCase.reference_review.reason) ? "评审模型响应超时，实际对话已保留，本条暂无法判定。" : /app-server exited|connection is closed/i.test(currentCase.reference_review.reason) ? "评审连接中断，实际对话已保留，本条暂无法判定。" : workbenchErrorText(currentCase.reference_review.reason)}</p>}
                        <ReviewCounts summary={evaluation.summary} />
                        <ReviewFindings review={currentCase?.reference_review} />
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
                              data-status={a.informational ? "inconclusive" : a.passed ? "passed" : "failed"}
                            >
                              {a.informational ? "·" : a.passed ? "✓" : "×"}{" "}
                              {a.kind === "script_consumed"
                                ? `已执行用户轮次 · ${a.actual}/${String(a.value)}`
                                : `${a.kind === "reference" ? "参考对话" : a.kind}${a.turn != null ? ` · ${a.turn === 0 ? "开场" : `第 ${a.turn} 轮回复`}` : ""} · ${String(a.value ?? "")}`}
                              {a.reason && <span> · {workbenchErrorText(a.reason)}</span>}
                              {a.reference_quote && <span> · 参考：“{a.reference_quote}”</span>}
                              {a.actual_quote && <span> · 实际：“{a.actual_quote}”</span>}
                            </p>
                          ))}
                        </div>
                      </div>
                    </div>
                  </>
                ) : (
                  <div className={styles.empty}>
                    <FlaskConical size={32} />
                    <h2>{state.evaluations.length ? "当前版本与测试集没有匹配结果" : "验证 Agent 的实际表现"}</h2>
                    <p>{state.evaluations.length ? "已有结果保留在上方列表，点击“查看对话与评审”打开对应测试记录。" : "选择候选版本和测试集，运行后可查看逐轮对话与断言结果。"}</p>
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
                  .map((j, index, jobs) => (
                    <article key={j.id}>
                      <div>
                        <strong>调优任务 {jobs.length - index}</strong>
                        <span>{statusText[j.status] || j.status}</span>
                      </div>
                      {!!j.output.tuning_scope && <p>完整范围：{String((j.output.tuning_scope as Record<string, unknown>).suite_count)} 批 / {String((j.output.tuning_scope as Record<string, unknown>).case_count)} 条</p>}
                      <p>
                        {(j.error && workbenchErrorText(j.error)) ||
                          (typeof j.output.verification_issue === "string" && j.output.verification_issue) ||
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
                            const ids = Array.isArray(j.output.evaluation_ids) ? j.output.evaluation_ids : [];
                            const saved = state.evaluations.filter(e => ids.includes(e.id));
                            const selected = saved.find(e => e.suite_id === suiteId) || saved[0];
                            if (selected) {
                              setSuiteId(selected.suite_id);
                              setEvaluationId(selected.id);
                            } else {
                              setEvaluationId("");
                            }
                            if (!selected && typeof j.input.suite_id === "string")
                              setSuiteId(j.input.suite_id);
                            setCaseName("");
                            setTab("tests");
                          }}
                        >
                          {j.output.blocked_reason ? "查看基线结果" : "查看推荐版本"}
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
          </main>
        )}
        <aside
          className={`${styles.assistant} ${tab !== "canvas" ? styles.compact : ""}`}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            void addFiles(Array.from(e.dataTransfer.files));
          }}
        >
          <div
            ref={messagesPane}
            className={styles.messages}
            hidden={tab !== "canvas"}
            role="region"
            aria-label="构建对话"
            tabIndex={0}
            onScroll={(e) => {
              const el = e.currentTarget;
              const following =
                el.scrollHeight - el.scrollTop - el.clientHeight < 64;
              followMessages.current = following;
              setFollowingMessages(following);
              if (el.scrollTop < 80 && !following) void loadOlder();
            }}
          >
            {historyBefore !== null && (
              <button className={styles.historyMore} disabled={historyLoading} onClick={() => void loadOlder()}>
                {historyLoading ? "正在加载更早消息…" : "加载更早消息"}
              </button>
            )}
            {historyError && <p role="alert">{workbenchErrorText(historyError)}</p>}
            {messages.length > 0 && (
              <p className={styles.conversationLabel}>
                构建对话
              </p>
            )}
            {!messages.length && historyBefore !== null && <p className={styles.subtle}>当前页是任务进度，向前加载可查看之前的对话。</p>}
            {!messages.length && historyBefore === null && (
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
              if (e.type === "basis.frozen") return <div key={e.sequence} data-event-sequence={e.sequence} className={styles.botBubble}><TaskBasis session={session} jobId={String(e.payload.job_id)} basis={e.payload as unknown as BasisSummary} /></div>;
              if (e.activity)
                return <div key={e.sequence} data-event-sequence={e.sequence}><TaskActivity steps={e.activity} running={e.running} workflowId={state.session.workflow_id} /></div>;
              if (e.evidence)
                return <div key={e.sequence} data-event-sequence={e.sequence}><EvidenceReads events={e.evidence} workflowId={state.session.workflow_id} /></div>;
              const input = e.payload.input as
                | Record<string, unknown>
                | undefined;
              const isUser =
                e.type === "task.queued" || e.payload.role === "user";
              const output = ["task.completed", "task.needs_attention", "task.awaiting_input"].includes(e.type)
                ? e.payload.output as Record<string, unknown> | undefined
                : undefined;
              const associatedJob = state.jobs.find(j => j.id === e.payload.job_id);
              const resumableOutput = associatedJob?.output;
              const resumableBuild = ["task.failed", "task.cancelled"].includes(e.type)
                && associatedJob?.kind === "build" && resumableOutput?.outcome === "build"
                && !!resumableOutput?.candidate_id && !!resumableOutput?.suite_id
                && !state.jobs.some(j => j.input.resume_build_job_id === associatedJob?.id);
              const resumableSummary = resumableOutput?.summary as Record<string, number> | undefined;
              const resumableTest = ["task.cancelled", "task.failed", "task.completed"].includes(e.type)
                && resumableOutput?.intent === "test" && resumableOutput?.outcome !== "review" && Array.isArray(resumableOutput.evaluation_ids)
                && resumableOutput.evaluation_ids.length > 0
                && (e.type !== "task.completed" || !!(resumableSummary?.error || (resumableSummary?.review_pending ?? resumableSummary?.inconclusive)));
              const resultEvaluation = state.evaluations.find(v =>
                v.id === (output?.evaluation_id || output?.id));
              const resultCandidateId = output?.candidate_id || resultEvaluation?.candidate_id;
              const resultIndex = state.candidates.findIndex(c => c.id === resultCandidateId);
              const resultCandidate = state.candidates[resultIndex];
              const resultVersion = state.candidates.length - resultIndex;
              const resultSuiteId = typeof output?.suite_id === "string"
                ? output.suite_id : resultEvaluation?.suite_id;
              const verification = output?.verification as Record<string, unknown> | undefined;
              const testSummary = (["test", "review"].includes(String(output?.outcome)) ? output?.summary as Record<string, unknown> : undefined) || verification || resultEvaluation?.summary;
              const testCounts = typeof testSummary?.passed === "number" && typeof testSummary?.total === "number"
                ? `${testSummary.passed}/${testSummary.total} 项通过`
                : undefined;
              const content =
                e.type === "task.queued"
                  ? String(
                      input?.text ||
                        input?.instruction ||
                        (e.payload.kind === "evaluate"
                          ? "运行文本仿真"
                          : "调优 Agent"),
                    )
                  : e.type === "context.compaction"
                    ? "已整理上下文，原始对话仍可向前翻阅。"
                  : ["task.completed", "task.needs_attention", "task.awaiting_input"].includes(e.type)
                    ? taskCompletionText(e.payload.output as Record<string, unknown>)
                    : e.type === "task.failed"
                      ? workbenchErrorText(String(e.payload.error))
                      : e.type === "task.cancelled"
                        ? "任务已取消。"
                        : e.type === "candidate.adopted"
                          ? "候选版本已应用到草稿。"
                          : e.type === "model.progress"
                            ? modelProgressText(e.payload)
                            : String(e.payload.text || "");
              return (
                <div
                  key={e.sequence}
                  data-event-sequence={e.sequence}
                  className={isUser ? styles.userBubble : styles.botBubble}
                >
                  <small>
                    {isUser ? "你" : "构建助手"}
                    {e.type === "voice.transcript" ? " · 语音" : ""}
                    {e.payload.phase === "commentary" ? " · 进展" : ""}
                    {e.type === "assistant.message.progress" ? (e.running ? " · 正在输出" : " · 输出未完成") : ""}
                  </small>
                  {isUser ? <p>{content}</p> : <WorkbenchMarkdown>{content}</WorkbenchMarkdown>}
                  <RequirementCoverage report={output?.requirement_review as Parameters<typeof RequirementCoverage>[0]["report"]} />
                  {resumableBuild && <Button disabled={locked} variant="outline" size="sm" onClick={() => void action(async () => {
                    result(await instruct({path: {session_id: session}, body: {mode: "build", text: "从已保存的候选和断点继续需求审查与调优，不从头构建，不自动应用到 Agent。", resume_build_job_id: String(e.payload.job_id), idempotency_key: crypto.randomUUID()}}));
                  })}>从断点继续调优</Button>}
                  {output?.task_basis ? <TaskBasis session={session} jobId={String(e.payload.job_id)} basis={output.task_basis as BasisSummary} /> : null}
                  {output && Array.isArray(output.evaluation_ids) && output.evaluation_ids.length > 0 && <Button disabled={locked} variant="outline" size="sm" onClick={() => void action(async () => {
                    result(await instruct({path: {session_id: session}, body: {mode: "test", text: "基于原始业务资料、用户明确要求和已确认约束，重新复核已有实际对话。区分业务问题、配置执行不一致、脚本不匹配、资料冲突和体验建议；不要重新运行对话，不修改 Agent。", review_evaluation_ids: (output.evaluation_ids as string[]), idempotency_key: crypto.randomUUID()}}));
                  })}>仅重新评审已有对话</Button>}
                  {associatedJob?.output.outcome === "review" && ["task.cancelled", "task.failed"].includes(e.type) && <Button disabled={locked} variant="outline" size="sm" onClick={() => void action(async () => {
                    result(await instruct({path: {session_id: session}, body: {mode: "test", text: "继续未完成的评审，保留已有结果，不重跑或修改 Agent。", continue_test_job_id: String(e.payload.job_id), idempotency_key: crypto.randomUUID()}}));
                  })}>继续未完成评审</Button>}
                  {resumableTest && <Button disabled={locked} variant="outline" size="sm" onClick={() => void action(async () => {
                    result(await instruct({path: {session_id: session}, body: {mode: "test", text: "继续原测试，只重试未完成或异常的用例，保留已完成结果。", continue_test_job_id: String(e.payload.job_id), idempotency_key: crypto.randomUUID()}}));
                  })}>继续未完成或异常测试</Button>}
                  {output?.outcome === "needs_input" && output.task_mode === "test" && (
                    <section className={styles.candidateResult} aria-label="补充测试信息">
                      <input aria-label="补充回答" value={testClarification} onChange={event => setTestClarification(event.target.value)} placeholder="填写补充信息，继续本次测试" />
                      <Button disabled={locked || !testClarification.trim()} onClick={() => void action(async () => {
                        result(await instruct({path: {session_id: session}, body: {mode: "test", text: testClarification, continue_test_job_id: String(e.payload.job_id), idempotency_key: crypto.randomUUID()}}));
                        setTestClarification("");
                      })}>补充并继续</Button>
                    </section>
                  )}
                  {["test", "review"].includes(String(output?.outcome)) && output && (
                    <section className={styles.candidateResult} aria-label="只运行测试结果">
                      <strong>{output.outcome === "review" ? "已有对话复核" : "测试结果"} · {testCounts}</strong>
                      <ReviewCounts summary={testSummary} />
                      {output.outcome === "test" && <Button variant="outline" size="sm" disabled={locked} onClick={() => void action(async () => {
                        result(await instruct({path: {session_id: session}, body: {mode: "test", text: "重新执行全部用例，沿用原测试版本和对话模型，评审使用当前工作台模型，不复用运行结果，不修改 Agent。", rerun_test_job_id: String(e.payload.job_id), idempotency_key: crypto.randomUUID()}}));
                      })}>全部重新运行</Button>}
                      <p>使用固定版本，未修改 Agent。可按批次查看实际对话和检查依据。</p>
                      {Number(testSummary?.reused) > 0 && <p>其中 {Number(testSummary?.reused)} 条复用此前已完成的真实运行结果，其余为本次执行。</p>}
                      {Array.isArray(output.source_manifest) && output.source_manifest.map((source: {suite_id?: string; asset_id?: string; sheet?: string; name?: string; dialogue_column?: string; rows: number}, index: number) => <p className={styles.subtle} key={source.suite_id || `${source.asset_id}:${source.sheet}:${index}`}>来源：{[source.name || (source.suite_id ? "保存的测试集" : "附件"), source.sheet, source.dialogue_column].filter(Boolean).join(" · ")} · {source.rows} 条</p>)}
                      <div className={styles.candidateResultActions}>
                        {(Array.isArray(output.suite_ids) ? output.suite_ids : []).map((id, index) => (
                          <Button key={String(id)} variant="outline" size="sm" onClick={() => {
                            setCandidateId(String(resultCandidateId)); setSuiteId(String(id)); setCaseName(""); setEvaluationId(""); setTab("tests");
                          }}>第 {index + 1} 批结果</Button>
                        ))}
                      </div>
                      {Array.isArray(output.import_issues) && output.import_issues.length > 0 && <p>有 {output.import_issues.length} 条样本未能执行：{output.import_issues.map((issue: { name?: string; error?: string }) => `${issue.name}：${issue.error}`).join("；")}</p>}
                    </section>
                  )}
                  {resultCandidate && !["test", "review", "needs_input"].includes(String(output?.outcome)) && (
                    <section
                      className={styles.candidateResult}
                      aria-label={`候选版本 ${resultVersion} · ${resultCandidate.name}`}
                    >
                      <strong>候选版本 {resultVersion} · {resultCandidate.name}</strong>
                      <p>{resultCandidate.summary}</p>
                      <span className={styles.subtle}>
                        {testCounts ? `自动测试：${testCounts}` : "尚未验证"}
                        {verification?.status === "inconclusive" ? " · 调优未完成" : ""}
                      </span>
                      <div className={styles.candidateResultActions}>
                        <Button variant="outline" size="sm" onClick={() => {
                          setCandidateId(resultCandidate.id);
                          setTab("canvas");
                        }}>
                          查看候选
                        </Button>
                        {resultEvaluation && resultSuiteId && (
                          <Button variant="ghost" size="sm" onClick={() => {
                            setCandidateId(resultCandidate.id);
                            setSuiteId(resultSuiteId);
                            setCaseName(""); setEvaluationId("");
                            setTab("tests");
                          }}>
                            查看测试结果
                          </Button>
                        )}
                      </div>
                    </section>
                  )}
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
            <div ref={bottom} />
          </div>
          <div className={styles.composer}>
            {tab === "canvas" && !followingMessages && (
              <button
                className={styles.latestMessage}
                onClick={() => {
                  followMessages.current = true;
                  setFollowingMessages(true);
                  bottom.current?.scrollIntoView({
                    behavior: "smooth",
                    block: "nearest",
                  });
                }}
              >
                <ChevronDown size={14} />
                回到最新消息
              </button>
            )}
            {(error || syncError) && (
              <div role="alert" className={styles.error}>
                {workbenchErrorText(error || syncError)}
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
                {workbenchErrorText(voice.error)}
              </p>
            )}
            <div className={styles.composerContext}>
              <Popover open={runsOpen} onOpenChange={(open) => void openRuns(open)}>
                <PopoverTrigger asChild>
                  <button className={styles.attachmentButton} aria-label="Agent Runs 运行记录">
                    <FileText size={14} /> Agent Runs <ChevronUp size={12} />
                  </button>
                </PopoverTrigger>
                <PopoverContent side="top" align="start" className={styles.popover}>
                  <h3>Agent Runs · 最近运行</h3>
                  <p className={styles.subtle}>读取这个 Agent 的实际对话、变量和日志，分析后可继续提出修改要求。</p>
                  <Button size="sm" disabled={locked || runsLoading || !agentRuns.length}
                    onClick={() => void sendInstruction("请分析 Agent Runs 中最新一次运行，按需查看相关证据，指出问题、依据和调优建议。先只分析，不修改 Agent。", "analyze")}>
                    分析最新运行
                  </Button>
                  {runsLoading ? <p>正在读取…</p> : !agentRuns.length ? <p>这个 Agent 还没有运行记录。</p> :
                    agentRuns.map((run) => (
                      <div className={styles.runRow} key={run.id}>
                        <Link href={`/workflow/${state.session.workflow_id}/run/${run.id}`} target="_blank">
                          <strong>#{run.id}</strong> · {new Date(run.created_at).toLocaleString()}
                          <small>{run.state} · {run.has_transcript ? "有对话记录" : "暂无已保存对话"}</small>
                        </Link>
                        <Button size="sm" variant="outline" disabled={locked}
                          onClick={() => void sendInstruction(`请分析 Agent Runs 运行 #${run.id}，按需查看相关证据，给出问题依据和调优建议。先只分析，不修改 Agent。`, "analyze")}>分析</Button>
                      </div>
                    ))}
                </PopoverContent>
              </Popover>
              <Popover>
                <PopoverTrigger asChild>
                  <button
                    className={styles.attachmentButton}
                    aria-label={`资料附件，已选择 ${selectedAssets.length} 个`}
                  >
                    <Paperclip size={14} />
                    资料 <b>{state.assets.length}</b>
                    <ChevronUp size={12} />
                  </button>
                </PopoverTrigger>
                <PopoverContent
                  side="top"
                  align="start"
                  className={`${styles.popover} ${styles.assets}`}
                  aria-label="资料附件"
                >
                  <h3>会话资料 · 本次重点 {selectedAssets.length}/5</h3>
                  <p className={styles.subtle}>
                    上传后保留在本会话，文字和语音都可按需读取。勾选仅标记本次重点，不会每轮附上全文。单个文件最大 20 MiB。
                  </p>
                  {!state.assets.length && (
                    <p className={styles.subtle}>
                      还没有附件，上传话术或业务资料即可开始。
                    </p>
                  )}
                  {state.assets.map((a) => (
                    <div key={a.id}>
                      <label>
                        <input
                          type="checkbox"
                          checked={selectedAssets.includes(a.id)}
                          disabled={locked || a.status !== "ready"}
                          onChange={(e) => {
                            if (
                              e.target.checked &&
                              selectedAssets.length >= 5
                            ) {
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
                          title={a.error ? workbenchErrorText(a.error) : undefined}
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
                        <small>
                          {a.status === "ready" ? "可引用" : "解析中"}
                        </small>
                      )}
                    </div>
                  ))}
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={locked}
                    onClick={() => fileInput.current?.click()}
                  >
                    <Paperclip size={14} />
                    上传文件
                  </Button>
                </PopoverContent>
              </Popover>
              <span
                className={styles.attachmentNames}
                title={selectedAssets
                  .map((id) => state.assets.find((a) => a.id === id)?.name)
                  .join("、")}
              >
                {selectedAssets.length
                  ? selectedAssets
                      .map((id) => state.assets.find((a) => a.id === id)?.name)
                      .join(" · ")
                  : "会话记录 · 自动整理上下文"}
              </span>
              {tab !== "canvas" &&
                composerExpanded &&
                !text &&
                voice.status === "off" && (
                  <button
                    aria-label="收起输入框"
                    onClick={() => setComposerOpen(false)}
                  >
                    <ChevronDown size={15} />
                  </button>
                )}
            </div>
            {!composerExpanded && (
              <button
                className={styles.openComposer}
                onClick={() => {
                  setComposerOpen(true);
                  requestAnimationFrame(() => composerInput.current?.focus());
                }}
              >
                <MessageSquare size={15} />
                向助手提出修改，或补充调优要求…
              </button>
            )}
            <div className={styles.inputBox} data-collapsed={!composerExpanded}>
              <Textarea
                ref={composerInput}
                aria-label="构建需求"
                hidden={!composerExpanded}
                rows={2}
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
                <select aria-label="助手任务模式" className={styles.intentSelect}
                  value={instructionMode} disabled={locked}
                  onChange={(e) => setInstructionMode(e.target.value as typeof instructionMode)}>
                  <option value="auto">自动判断</option>
                  <option value="analyze">仅分析</option>
                  <option value="test">只运行测试</option>
                  <option value="build">构建修改</option>
                </select>
                {instructionMode === "test" && <select aria-label="测试方式" className={styles.intentSelect} value={testMode} disabled={locked} onChange={e => setTestMode(e.target.value as typeof testMode)}>
                  <option value="dynamic">动态客户仿真</option>
                  <option value="single_step">真实上下文单步</option>
                  <option value="scripted">固定脚本与错位检查</option>
                </select>}
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
                  onClick={() => void sendInstruction(text.trim())}
                >
                  {busy ? (
                    <Loader2 size={17} className="animate-spin" />
                  ) : (
                    <ArrowUp size={17} />
                  )}
                </Button>
              </div>
            </div>
            {voice.status === "connected" && (
              <small role="status" className={styles.composerHint}>
                {voice.contextSynced
                  ? `正在使用麦克风 · 会话资料可按需读取`
                  : "正在同步附件和版本选择，请稍候…"}
              </small>
            )}
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
