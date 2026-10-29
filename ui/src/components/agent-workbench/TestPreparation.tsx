import type { PreparationReceipt } from "./types";

export function TestPreparation({receipt}: {receipt?: PreparationReceipt}) {
  if (!receipt) return null;
  return <section aria-label="测试准备结果">
    <h3>场景与验收标准</h3>
    <p>共 {receipt.total} 条 · 可执行 {receipt.ready} · 资料待确认 {receipt.needs_input} · 准备异常 {receipt.preparation_error} · 准备中 {receipt.preparing || 0} · 待处理 {receipt.pending || 0}</p>
    <p>原始用例保留，已完成的阶段单独保存。准备完成不代表 Agent 测试通过。</p>
    <details><summary>逐条转换记录与待处理项</summary>
      {receipt.rows.map(row => <details key={row.name}>
        <summary>{row.name} · {{ready: "可执行", needs_input: "资料待确认", preparation_error: "准备异常", preparing: "准备中", pending: "待处理"}[row.status]} · {row.requirement_count} 项业务断言</summary>
        <p>来源：{row.source.sheet || "原始用例"}{row.source.row != null ? ` · 第 ${row.source.row} 行` : ""}</p>
        {row.questions.map((question, i) => <p key={i}>{question}</p>)}
        {row.error && <p>{row.error}</p>}
      </details>)}
    </details>
  </section>;
}
