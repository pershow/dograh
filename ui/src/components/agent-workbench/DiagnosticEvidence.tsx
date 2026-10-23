import styles from "./workbench.module.css";

const object = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const rows = (value: unknown): Record<string, unknown>[] => Array.isArray(value) ? value.map(object) : [];
const text = (value: unknown) => typeof value === "string" || typeof value === "number" ? String(value) : "";
const provenance: Record<string, string> = { captured: "实际请求记录", reconstructed: "基于历史证据重建", unavailable: "请求证据缺失", original_source: "已读取的原始资料" };

function JsonEvidence({ label, value }: { label: string; value: unknown }) {
  if (value === undefined || value === null) return null;
  return <details><summary>{label}</summary><pre className={styles.diagnosticJson}>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre></details>;
}

/** Only observed receipts are rendered; predictions never become results. */
export function DiagnosticEvidence({ data, label }: { data: Record<string, unknown>; label?: string }) {
  const results = rows(data.results);
  const findings = rows(data.findings);
  const citations = [...rows(data.reads), ...rows(data.experiments)];
  const execution = object(data.execution);
  const transitions = rows(execution.transitions);
  return <details className={styles.diagnosticEvidence}>
    <summary>{label || `查看排查证据${data.run_id ? ` · 运行 #${text(data.run_id)}` : ""}`}</summary>
    {data.evidence_id ? <p>证据编号：<code>{text(data.evidence_id)}</code></p> : null}
    {data.node_name ? <p>实际节点：{text(data.node_name)}（{text(data.node_id)}）</p> : null}
    {data.provenance ? <p>证据来源：{provenance[text(data.provenance)] || text(data.provenance)}</p> : null}
    {data.hypothesis ? <p><strong>待验证解释：</strong>{text(data.hypothesis)}</p> : null}
    {data.prediction ? <p><strong>实验前预期：</strong>{text(data.prediction)}</p> : null}
    {transitions.length > 0 && <p>节点轨迹：{transitions.map(t => `第 ${text(t.turn)} 轮 ${text(t.node_name) || text(t.node_id)}`).join(" → ")}</p>}
    {data.agent_modified === false && <p>Agent 未修改；实验返回的工具调用未执行。</p>}
    {Array.isArray(data.cause_chain) && <ol>{data.cause_chain.map((s, i) => <li key={i}>{text(s)}</li>)}</ol>}
    {findings.map((f, i) => <div key={i}>
      <p><strong>{{ supported: "有证据支持", refuted: "已被反证", untested: "尚未验证" }[text(f.status)] || text(f.status)}：</strong>{text(f.claim)}</p>
      <p>{text(f.reason)}</p>
      {Array.isArray(f.evidence_ids) && f.evidence_ids.map((id, j) => {
        const cited = citations.find(c => c.evidence_id === id);
        return cited ? <DiagnosticEvidence key={j} data={cited} label={`查看引用：${text(id)}`} /> : <small key={j}>引用：{text(id)} </small>;
      })}
    </div>)}
    {results.length > 0 && <table><thead><tr><th>组别</th><th>次数</th><th>实际回复 / 工具调用</th></tr></thead><tbody>{results.map((r, i) => <tr key={i}>
      <td>{r.arm === "baseline" ? "原始条件" : "临时对照"}</td><td>{text(r.repeat)}</td>
      <td>{r.error ? `调用失败：${text(r.error)}` : <>{text(r.text)}{rows(r.tool_calls).map((t, j) => <div key={j}>工具：{text(t.name)} {text(t.arguments)}（未执行）</div>)}</>}</td>
    </tr>)}</tbody></table>}
    {Array.isArray(data.limitations) && data.limitations.length > 0 && <div><strong>证据限制</strong><ul>{data.limitations.map((s, i) => <li key={i}>{text(s)}</li>)}</ul></div>}
    {data.complete === false && <p>分析器尚未读完此证据的所有分页。</p>}
    {data.next_offset !== undefined && data.next_offset !== null && <p>本次读取至字符 {text(data.next_offset)} / {text(data.characters)}，后续内容尚未包含在本页。</p>}
    {Array.isArray(data.next_steps) && data.next_steps.length > 0 && <div><strong>待验证事项</strong><ul>{data.next_steps.map((s, i) => <li key={i}>{text(s)}</li>)}</ul></div>}
    <JsonEvidence label="本次读取的原文" value={data.text ?? data.content} />
    <JsonEvidence label="原始条件：提示词、历史与工具" value={data.baseline} />
    <JsonEvidence label="临时对照：提示词、历史与工具" value={data.variant} />
    <JsonEvidence label="全部读取证据" value={data.reads} />
    <JsonEvidence label="全部实验记录" value={data.experiments} />
  </details>;
}
