"use client";

import { useState } from "react";

import { workbenchTaskBasisApiV1AgentWorkbenchSessionsSessionIdJobsJobIdBasisGet as readBasis } from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

export type BasisSummary = {
  fingerprint: string;
  sources: {id: string; title: string; kind: string; characters: number; available?: boolean; reason?: string}[];
  warnings?: string[];
};
type SourcePage = {id: string; text: string; offset: number; next_offset: number | null};
const labels: Record<string, string> = {current_request: "本次要求", user_request: "用户原始要求", material: "业务资料", memory: "已确认约束", reference: "测试示例"};

export function TaskBasis({session, jobId, basis}: {session: string; jobId: string; basis?: BasisSummary}) {
  const {user, loading} = useAuth();
  const [page, setPage] = useState<SourcePage>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  if (!basis) return null;
  const read = async (sourceId: string, offset = 0) => {
    if (loading || !user || busy) return;
    setBusy(true); setError("");
    try {
      const response = await readBasis({path: {session_id: session, job_id: jobId}, query: {source_id: sourceId, offset}});
      if (response.error) throw new Error(detailFromError(response.error, "读取原始依据失败"));
      if (!response.data) throw new Error("服务未返回资料原文");
      const next = response.data.data as SourcePage;
      setPage(previous => offset && previous?.id === next.id ? {...next, text: previous.text + next.text} : next);
    } catch (e) {setError(e instanceof Error ? e.message : "读取失败");}
    finally {setBusy(false);}
  };
  return <details aria-label="本次任务依据">
    <summary>任务依据 · {basis.sources.length} 项来源</summary>
    <p>保留原始资料与版本。历史原文需要核对适用范围；模型推测不会自动记为已确认约束。</p>
    {basis.warnings?.map((warning, i) => <p key={i}>{warning}</p>)}
    {basis.sources.map(source => <div key={source.id}>
      <p>{labels[source.kind] || source.kind} · {source.title} · {source.characters} 字</p>
      {source.reason && <p>{source.reason}</p>}
      {source.characters > 0 && <Button variant="ghost" size="sm" disabled={busy || loading || !user} onClick={() => void read(source.id)}>查看原文</Button>}
      {page?.id === source.id && <>
        <blockquote style={{whiteSpace: "pre-wrap", maxHeight: 360, overflowY: "auto"}}>{page.text}</blockquote>
        {page.next_offset !== null && <Button variant="ghost" size="sm" disabled={busy} onClick={() => void read(source.id, page.next_offset!)}>继续加载原文</Button>}
      </>}
    </div>)}
    {error && <p role="alert">{error}</p>}
  </details>;
}

type Requirement = {requirement: string; status: string; scope: string; source_quote: string; source_ids?: string[]; nodes: string[]; cases: string[]; evidence: string; correction: string};
export function RequirementCoverage({report}: {report?: {requirements?: Requirement[]}}) {
  if (!report?.requirements?.length) return null;
  return <details aria-label="需求覆盖明细">
    <summary>需求覆盖 · {report.requirements.filter(r => r.status === "covered").length}/{report.requirements.length} 项已验证</summary>
    {report.requirements.map((r, i) => <details key={i}>
      <summary>{r.status === "covered" ? "已覆盖" : r.status === "needs_input" ? "需要业务补充" : "有缺口"} · {r.requirement}</summary>
      <p>范围：{r.scope === "agent" ? "整个 Agent" : "局部流程"}</p>
      <blockquote>{r.source_quote}</blockquote>
      <p>来源：{r.source_ids?.join("、") || "历史记录未保存来源编号"}</p>
      <p>实现：{r.nodes.join("、") || "尚未定位"}；测试：{r.cases.join("、") || "尚未覆盖"}</p>
      <p>{r.evidence}</p>
      {r.correction && <p>待处理：{r.correction}</p>}
    </details>)}
  </details>;
}
