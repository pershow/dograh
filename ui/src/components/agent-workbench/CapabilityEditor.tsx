"use client";

import { useState } from "react";

import { type AgentBehavior,createWorkbenchCapabilityCandidate } from "@/client";
import { Button } from "@/components/ui/button";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

import type { Snapshot } from "./types";
import styles from "./workbench.module.css";

export function CapabilityEditor({session, snapshot, candidateId, fingerprint, disabled, onCreated}: {
  session: string; snapshot: Snapshot; candidateId?: string; fingerprint: string;
  disabled: boolean; onCreated: (id: string) => void;
}) {
  const {user, loading: authLoading} = useAuth();
  const config = snapshot.workflow_configurations;
  const initial = (config.agent_behavior || {}) as AgentBehavior;
  const [enabled, setEnabled] = useState(initial.enabled || false);
  const [goalType, setGoalType] = useState(initial.goal_type || "custom");
  const [goal, setGoal] = useState(initial.goal || "");
  const [success, setSuccess] = useState((initial.success_criteria || []).join("\n"));
  const [stop, setStop] = useState((initial.stop_criteria || []).join("\n"));
  const [listen, setListen] = useState(initial.listen_during_tools || false);
  const [compaction, setCompaction] = useState(config.context_compaction_enabled === true);
  const [rules, setRules] = useState(JSON.stringify({facts: initial.facts || [], stages: initial.stages || [], tool_policies: initial.tool_policies || {}, knowledge_bindings: initial.knowledge_bindings || {}}, null, 2));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function save() {
    if (disabled || saving || authLoading || !user || !fingerprint) return;
    setSaving(true); setError("");
    try {
      const advanced = JSON.parse(rules) as Pick<AgentBehavior, "facts" | "stages" | "tool_policies" | "knowledge_bindings">;
      const response = await createWorkbenchCapabilityCandidate({path: {session_id: session}, body: {
        expected_fingerprint: fingerprint, candidate_id: candidateId || null,
        agent_behavior: {...initial, ...advanced, enabled, goal_type: goalType, goal,
          success_criteria: success.split("\n").map(s => s.trim()).filter(Boolean),
          stop_criteria: stop.split("\n").map(s => s.trim()).filter(Boolean), listen_during_tools: listen},
        context_compaction_enabled: compaction,
        context_compaction_trigger_chars: Number(config.context_compaction_trigger_chars || 32000),
        context_compaction_keep_messages: Number(config.context_compaction_keep_messages || 8),
      }});
      if (response.error) throw new Error(detailFromError(response.error, "能力配置保存失败"));
      const id = response.data?.data?.id;
      if (typeof id !== "string") throw new Error("服务器未返回候选版本");
      onCreated(id);
    } catch (e) { setError(e instanceof Error ? e.message : "能力配置保存失败"); }
    finally { setSaving(false); }
  }

  return <details open className={styles.candidateResult}>
    <summary>业务目标与运行能力</summary>
    <p>保存生成新的候选版本。测试通过后，可应用到草稿并发布。</p>
    <fieldset className={styles.capabilityForm} disabled={disabled || saving || authLoading || !user}>
      <label><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}/> 启用结构化任务与动作策略</label>
      <label>目标类型<select value={goalType} onChange={e => setGoalType(e.target.value as typeof goalType)}><option value="service">客服与支持</option><option value="sales">销售与营销</option><option value="custom">自定义</option></select></label>
      <label>任务目标<textarea value={goal} onChange={e => setGoal(e.target.value)} maxLength={2000}/></label>
      <label>成功标准（每行一项）<textarea value={success} onChange={e => setSuccess(e.target.value)}/></label>
      <label>退出或接管条件（每行一项）<textarea value={stop} onChange={e => setStop(e.target.value)}/></label>
      <label><input type="checkbox" checked={listen} onChange={e => setListen(e.target.checked)}/> 工具执行期间接收用户补充和更正</label>
      <label><input type="checkbox" checked={compaction} onChange={e => setCompaction(e.target.checked)}/> 长对话后台整理上下文</label>
      <details><summary>事实、阶段、工具策略与知识适用性（高级）</summary>
        <p>工具策略使用 Agent 已绑定的工具 UUID。写操作须指定 data 下的业务成功字段；详细配置会在保存时校验。</p>
        <textarea aria-label="结构化业务规则" value={rules} onChange={e => setRules(e.target.value)} rows={14} spellCheck={false}/>
      </details>
      <Button onClick={() => void save()} disabled={!fingerprint || saving}>{saving ? "正在保存…" : "生成能力配置候选版本"}</Button>
    </fieldset>
    {error && <p role="alert">{error}</p>}
  </details>;
}
