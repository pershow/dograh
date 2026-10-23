"use client";

import { Brain } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import {
  workbenchConfirmMemoryApiV1AgentWorkbenchSessionsSessionIdMemoryPost as confirmMemory,
  workbenchMemoryApiV1AgentWorkbenchSessionsSessionIdMemoryGet as getMemory,
  workbenchMemorySourceApiV1AgentWorkbenchSessionsSessionIdMemorySourcesSourceIdGet as getSource,
  workbenchMemorySourcesApiV1AgentWorkbenchSessionsSessionIdMemorySourcesGet as getSources,
  workbenchRevokeMemoryApiV1AgentWorkbenchSessionsSessionIdMemoryMemoryIdRevokePost as revokeMemory,
} from "@/client/sdk.gen";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Textarea } from "@/components/ui/textarea";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

import styles from "./workbench.module.css";

type MemoryKind = "requirement" | "constraint" | "sop";
type Entry = {id: string; kind: MemoryKind; content: string; status: string; confirmed_by: number; created_at: string; replaces_id?: string; source: {type: string; id?: string; quote?: string; session_id?: string}};
type Memory = {revision: string; entries: Entry[]};
type Source = {id: string; source_type: "request" | "attachment"; title: string; session_id: string; content?: string};
type Sources = {items: Source[]; total: number; next_offset: number | null};
const labels = {requirement: "长期需求", constraint: "约束", sop: "SOP 原文"};
function data<T>(r: {data?: {data: Record<string, unknown>}; error?: unknown}): T {
  if (r.error) throw new Error(detailFromError(r.error, "记忆操作失败"));
  if (!r.data) throw new Error("服务未返回记忆数据");
  return r.data.data as T;
}

export function AgentMemory({session, revision, onChanged}: {session: string; revision?: string; onChanged: () => void}) {
  const {user, loading: authLoading} = useAuth();
  const [open, setOpen] = useState(false);
  const [memory, setMemory] = useState<Memory>();
  const [sources, setSources] = useState<Sources>();
  const [sourceType, setSourceType] = useState<"request" | "attachment">("request");
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<{kind: MemoryKind; content: string; replaces?: string; source?: Source}>();
  const load = useCallback(async () => {
    const value = data<Memory>(await getMemory({path: {session_id: session}, query: {include_history: history}}));
    setMemory(value);
  }, [session, history]);
  useEffect(() => {
    if (!open || authLoading || !user) return;
    let live = true;
    getMemory({path: {session_id: session}, query: {include_history: history}})
      .then(r => {const value = data<Memory>(r); if (live) setMemory(value);})
      .catch(e => {if (live) setError(String(e.message));});
    return () => {live = false;};
  }, [open, authLoading, user, session, revision, history]);
  const action = async (fn: () => Promise<void>) => {
    setBusy(true); setError("");
    try {await fn();} catch (e) {setError(e instanceof Error ? e.message : "操作失败");}
    finally {setBusy(false);}
  };
  const search = async (offset = 0) => {
    const page = data<Sources>(await getSources({path: {session_id: session}, query: {source_type: sourceType, query, offset}}));
    setSources(previous => offset && previous ? {...page, items: [...previous.items, ...page.items]} : page);
  };
  return <Popover open={open} onOpenChange={setOpen}>
    <PopoverTrigger asChild><Button variant="ghost" size="icon" aria-label="Agent 长期记忆" title="Agent 长期记忆"><Brain size={16}/></Button></PopoverTrigger>
    <PopoverContent align="end" className={styles.memoryPanel} aria-label="Agent 长期记忆">
      <h3>此 Agent 的长期记忆</h3>
      <p className={styles.subtle}>同一 Agent 的会话共用已确认要求。用户原文和附件可查回；历史问题和模型推测不会自动变成已确认事实。</p>
      {error && <p role="alert">{error}</p>}
      {!memory && !error && <p>正在加载…</p>}
      <div className={styles.memoryActions}>
        <Button size="sm" variant="outline" disabled={busy || !memory} onClick={() => setForm({kind: "requirement", content: ""})}>新增长期要求</Button>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => action(load)}>刷新记忆</Button>
        <label><input type="checkbox" checked={history} onChange={e => setHistory(e.target.checked)}/> 显示替换和撤销记录</label>
      </div>
      {memory && !memory.entries.length && <p>还没有单独确认的长期要求。可从下方历史原文中选取，或直接填写。</p>}
      {memory?.entries.map(entry => <article className={styles.memoryEntry} key={entry.id}>
        <strong>{labels[entry.kind]} · {entry.status === "confirmed" ? "已确认" : entry.status === "superseded" ? "已被替换" : "已撤销"}</strong>
        <details><summary>{entry.content.slice(0, 100)}{entry.content.length > 100 ? "…" : ""}</summary><pre>{entry.content}</pre></details>
        <small>用户 {entry.confirmed_by} 确认 · {new Date(entry.created_at).toLocaleString("zh-CN")}{entry.replaces_id ? " · 替换旧版本" : ""}</small>
        {entry.source.quote && <details><summary>查看引用原文 · 会话 {entry.source.session_id?.slice(0, 6)}</summary><pre>{entry.source.quote}</pre></details>}
        {entry.status === "confirmed" && <div className={styles.memoryActions}>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setForm({kind: entry.kind, content: entry.content, replaces: entry.id})}>替换</Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => action(async () => {
            data(await revokeMemory({path: {session_id: session, memory_id: entry.id}, body: {expected_revision: memory!.revision}}));
            await load(); onChanged();
          })}>撤销</Button>
        </div>}
      </article>)}
      {form && <form className={styles.memoryEntry} onSubmit={e => {e.preventDefault(); void action(async () => {
        data(await confirmMemory({path: {session_id: session}, body: {kind: form.kind, content: form.content, expected_revision: memory!.revision, replaces_id: form.replaces, source_id: form.source?.id, source_type: form.source?.source_type, source_quote: form.source?.content}}));
        setForm(undefined); await load(); onChanged();
      });}}>
        <label>类型 <select aria-label="长期记忆类型" value={form.kind} onChange={e => setForm({...form, kind: e.target.value as MemoryKind})}>{Object.entries(labels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
        <Textarea aria-label="长期要求原文" value={form.content} onChange={e => setForm({...form, content: e.target.value})} rows={6}/>
        <p className={styles.subtle}>确认后将用于此 Agent 的后续构建。SOP 会保留原文；修改内容不会自动更改当前草稿。</p>
        <div className={styles.memoryActions}><Button size="sm" disabled={busy || !form.content.trim()}>确认并{form.replaces ? "替换" : "保存"}</Button><Button type="button" variant="ghost" size="sm" onClick={() => setForm(undefined)}>取消</Button></div>
      </form>}
      <details><summary>查找所有会话的用户原文与资料</summary>
        <form className={styles.memoryActions} onSubmit={e => {e.preventDefault(); void action(() => search());}}>
          <select aria-label="记忆来源类型" value={sourceType} onChange={e => {setSourceType(e.target.value as typeof sourceType); setSources(undefined);}}><option value="request">用户原文</option><option value="attachment">附件文本</option></select>
          <input aria-label="搜索历史原文" placeholder="关键词，可留空" value={query} onChange={e => setQuery(e.target.value)}/>
          <Button size="sm" disabled={busy}>查找</Button>
        </form>
        {sources && <p>共 {sources.total} 条来源 · 原文不代表已确认决策</p>}
        {sources?.items.map(source => <div key={source.id} className={styles.memoryEntry}>
          <p>{source.title}</p><small>会话 {source.session_id.slice(0, 6)}</small>
          <Button variant="ghost" size="sm" disabled={busy} onClick={() => action(async () => {
            const original = data<Source>(await getSource({path: {session_id: session, source_id: source.id}, query: {source_type: source.source_type}}));
            setForm({kind: "requirement", content: original.content || "", source: original});
          })}>查看原文并确认要求</Button>
        </div>)}
        {sources?.next_offset != null && <Button variant="ghost" disabled={busy} onClick={() => action(() => search(sources.next_offset!))}>加载更多来源</Button>}
      </details>
    </PopoverContent>
  </Popover>;
}
