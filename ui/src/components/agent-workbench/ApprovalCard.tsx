"use client";

import { useId, useState } from "react";

import type { CallBrief, WorkbenchDecision } from "@/client/types.gen";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { T, useI18n } from "@/lib/i18n/LocaleProvider";

export type WorkbenchApproval = {
  id: string;
  revision: string;
  kind: "requirements" | "tool_change";
  status: "pending" | "approved" | "rejected";
  proposal: {
    brief?: CallBrief;
    operation?: "create" | "update";
    reason?: string;
    tool_uuid?: string;
    request?: Record<string, unknown>;
    before?: Record<string, unknown>;
  };
};

export function ApprovalCard({ approval, disabled, onDecision }: {
  approval: WorkbenchApproval;
  disabled: boolean;
  onDecision: (decision: WorkbenchDecision) => Promise<void>;
}) {
  const { t } = useI18n();
  const [feedback, setFeedback] = useState("");
  const [editing, setEditing] = useState(false);
  const [sending, setSending] = useState(false);
  const feedbackId = useId();
  const brief = approval.proposal.brief;
  const locked = disabled || sending || approval.status !== "pending";
  const decide = async (decision: WorkbenchDecision["decision"]) => {
    setSending(true);
    try { await onDecision({ approval_id: approval.id, revision: approval.revision,
      decision, feedback: decision === "approve" ? "" : feedback.trim() }); }
    finally { setSending(false); }
  };
  const sections: [string, string[] | undefined][] = brief ? [
    ["需要收集的信息", brief.information_to_collect], ["业务规则", brief.business_rules],
    ["成功标准", brief.success_criteria], ["待确认的假设", brief.assumptions],
  ] : [];
  return <article className="space-y-4 rounded-xl border bg-background p-4 text-sm" aria-label={t(brief ? "确认 Agent 需求" : "批准工具变更")}>
    <h3 className="font-semibold"><T text={brief ? "确认 Agent 需求" : "批准工具变更"} /></h3>
    {brief ? <>
      <p className="whitespace-pre-wrap">{brief.goal}</p>
      <p>{t("呼叫方向")}：{t({ inbound: "呼入", outbound: "呼出", both: "呼入和呼出" }[brief.direction])}</p>
      {sections.map(([title, items]) => !!items?.length && <section key={title}>
        <h4 className="font-medium">{t(title)}</h4>
        <ul className="list-disc space-y-1 pl-5">{items.map((text, i) => <li className="whitespace-pre-wrap break-words" key={i}>{text}</li>)}</ul>
      </section>)}
      <p className="text-xs text-muted-foreground"><T text="确认后开始构建候选并验证；应用到草稿仍由你决定。" /></p>
    </> : <>
      <p>{t(approval.proposal.operation === "create" ? "创建组织工具" : "修改组织工具")}</p>
      <p className="whitespace-pre-wrap">{approval.proposal.reason}</p>
      {approval.proposal.operation === "update" && <p className="text-amber-700"><T text="此变更会影响所有使用该工具的 Agent。" /></p>}
      {approval.proposal.before && <details><summary><T text="当前配置" /></summary><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(approval.proposal.before, null, 2)}</pre></details>}
      <details open><summary><T text="待批准的配置" /></summary><pre className="max-h-80 overflow-auto whitespace-pre-wrap break-all">{JSON.stringify(approval.proposal.request, null, 2)}</pre></details>
    </>}
    {editing ? <>
      <label htmlFor={feedbackId}><T text="修改意见" /></label>
      <Textarea id={feedbackId} value={feedback} onChange={e => setFeedback(e.target.value)} maxLength={12000} disabled={locked} />
      <div className="flex gap-2">
        <Button disabled={locked || !feedback.trim()} onClick={() => void decide("revise")}><T text="提交修改意见" /></Button>
        <Button variant="ghost" disabled={locked} onClick={() => setEditing(false)}><T text="返回" /></Button>
      </div>
    </> : <div className="flex flex-wrap gap-2">
      <Button disabled={locked} onClick={() => void decide("approve")}><T text={brief ? "确认并构建" : "批准并继续"} /></Button>
      <Button variant="outline" disabled={locked} onClick={() => setEditing(true)}><T text="提出修改" /></Button>
      {!brief && <Button variant="ghost" disabled={locked} onClick={() => void decide("deny")}><T text="拒绝此变更" /></Button>}
    </div>}
  </article>;
}
