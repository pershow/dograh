import type { ReplayReview } from "./types";

export const reviewCategoryText: Record<string, string> = {
  satisfied: "已满足",
  script_mismatch: "脚本不匹配",
  agent_issue: "Agent 问题",
  configuration_issue: "配置执行不一致",
  quality_suggestion: "体验建议",
  source_conflict: "资料冲突",
  insufficient_evidence: "证据不足",
};

export function ReviewCounts({ summary }: { summary?: Record<string, unknown> }) {
  if (!summary || summary.agent_issue == null) return null;
  return <div aria-label="评审分类汇总">
    <p>Agent 问题 {Number(summary.agent_issue || 0)} 条 · 脚本不匹配 {Number(summary.script_mismatch || 0)} 条 · 证据不足 {Number(summary.insufficient_evidence || 0)} 条</p>
    <p>复核纠正初评 {Number(summary.review_corrected || 0)} 条。同一条对话可包含多类发现；脚本不匹配不直接认定为 Agent 失败。</p>
    {Number(summary.invalid || 0) > 0 && <p>分类中保留了无效测试的观察线索，不能将分类条数等同于 Agent 失败数；请结合每条测试的有效性查看。</p>}
    {summary.configuration_issue != null && <p>配置执行不一致 {Number(summary.configuration_issue || 0)} 条 · 资料冲突 {Number(summary.source_conflict || 0)} 条 · 体验建议 {Number(summary.quality_suggestion || 0)} 条。体验建议不计为业务失败。</p>}
    {Number(summary.review_pending || 0) > 0 && <p>另有 {Number(summary.review_pending)} 条评审未完成，实际对话已保留；继续任务只补评审。</p>}
  </div>;
}

const severityText: Record<string, string> = {blocking: "影响业务验收", minor: "轻微问题", suggestion: "建议", unknown: "待核实"};
const layerText: Record<string, string> = {source: "原始资料", construction: "构建还原", runtime: "运行执行", script: "测试脚本", experience: "交互体验", unknown: "尚未定位"};

export function ReviewFindings({ review }: { review?: ReplayReview }) {
  if (!review || ![2, 3].includes(review.version || 0)) return null;
  const corrections = review.verification?.reviews.filter(r => r.verdict !== "supported") || [];
  return <section aria-label="证据复核结果">
    <h3>证据复核</h3>
    <p>{review.version === 3 ? "以原始用户要求、业务资料和已确认约束为依据，分别检查构建、运行与体验。参考对话保留为示例，生成配置作为被检查对象；未覆盖的行为保留为无法判定。" : "这是旧版评审，依据包含生成配置。可用原始资料重新复核已有对话，旧结论会保留。"}</p>
    {review.warnings?.map((warning, i) => <p key={i}>{warning}</p>)}
    {review.checks?.map((check, i) => <details key={i}>
      <summary>{reviewCategoryText[check.category] || check.category} · {check.criterion}</summary>
      <p>{check.reason}</p>
      {check.severity && <p>影响：{severityText[check.severity] || check.severity} · 归因：{layerText[check.layer || "unknown"] || check.layer}</p>}
      {check.applicability && <p>适用条件：{check.applicability}</p>}
      {check.citations.map((citation, j) => <blockquote key={j}>
        <small>{citation.source === "actual" ? `实际${citation.role === "user" ? "客户" : "Agent"} · 第 ${citation.turn ?? 0} 轮` : citation.source === "reference" ? "附件参考对话" : citation.source === "basis" ? `原始依据 · ${citation.title || citation.id} · 位置 ${citation.offset || 0}` : citation.source === "policy" ? `被检查的配置 · ${citation.path || citation.id}` : "本次测试要求"}</small>
        <p>{citation.quote}</p>
      </blockquote>)}
    </details>)}
    {corrections.length > 0 && <details>
      <summary>初评复核记录 · {corrections.length} 项</summary>
      {corrections.map((r, i) => <div key={i}>
        <p>{r.verdict === "corrected" ? "已纠正初评" : "复核仍无法判定"}：{r.reason}</p>
        <p>初评：{review.draft_review?.checks[r.check_index]?.reason}</p>
        <p>复核：{r.check.reason}</p>
      </div>)}
    </details>}
  </section>;
}
