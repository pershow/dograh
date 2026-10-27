import type { CaseResult } from "./types";
import styles from "./workbench.module.css";

export const simulationModes = {dynamic: "动态客户仿真", single_step: "真实上下文单步测试", scripted: "固定脚本回放"};

export function SimulationEvidence({result}: {result: CaseResult}) {
  const v = result.validity;
  if (!v) return <p className={styles.subtle}>历史测试未进行逐轮对话有效性检查。</p>;
  const scenario = v.scenario;
  const stop = ({script_mismatch: "固定脚本已错位，停止继续回放", simulation_mismatch: "模拟回复校正后仍不匹配，停止发送", context_ambiguous: "真实上下文不能唯一对应当前节点", missing_scenario_fact: "缺少必要的场景信息", turn_limit: "达到场景轮次边界，尚不能判断完整流程", customer_ended: "模拟客户自然结束", agent_completed: "Agent 已结束", script_exhausted: "固定脚本已读完"} as Record<string, string>)[v.stop_reason || ""];
  return <section aria-label="测试方法与有效性" className={styles.candidateResult}>
    <strong>{simulationModes[result.mode || "scripted"]} · {result.status === "running" ? "检查中" : ({valid: "对话有效", invalid: "测试无效", inconclusive: "有效性未确定", unchecked: "未检查"} as Record<string,string>)[v.status] || v.status}</strong>
    {stop && <p>{stop}</p>}
    {v.stop_reason === "alignment_uncertain" && <p>缺少足够证据确认下一句的前提，已暂停回放；尚未认定脚本错位。</p>}
    {v.stop_reason === "execution_error" && <p>仿真执行异常；不能据此判定 Agent 业务失败。</p>}
    {v.stop_reason === "replay_context_ambiguous" && <p>无法可靠确定样本的初始身份，已保留原因，未使用冲突身份启动仿真。</p>}
    {v.replay_context && <details><summary>本次冻结的测试变量及来源</summary>
      <p>{v.replay_context.assumption}</p>
      {Object.entries(v.replay_context.variables).map(([key, value]) => <p key={key}>{key}：{String(value)}</p>)}
      {v.replay_context.bindings.map(b => <p key={b.variable}>{b.variable} 的来源：“{b.quote}” · {b.reason}</p>)}
      {v.replay_context.limitations.map((s, i) => <p key={i}>限制：{s}</p>)}
    </details>}
    {v.status === "invalid" && <p>本条不计入 Agent 正常流程通过率；已观察到的回复与评审线索仍保留。</p>}
    {v.scope === "single_reply" && <p>仅测试原始第 {v.source_turn} 轮的一个回复，不代表完整流程通过。起始节点是依据前文推定的测试条件。</p>}
    {v.context_selection && <p>上下文定位：{v.context_selection.node_id || "无法定位"} · {v.context_selection.reason}</p>}
    {!!result.history?.length && <details><summary>本轮使用的原始前文（{result.history.length} 条）</summary>{result.history.map((m,i)=><p key={i}>{m.role === "user" ? "原客户" : "原坐席"}：{m.text}</p>)}</details>}
    {scenario && <details><summary>客户事实、测试假设与诉求</summary>
      <p>以下为冻结的测试场景；测试假设不写入 Agent 记忆，模拟操作不代表真实后台操作。</p>
      {scenario.facts.map(f=><p key={f.id}><b>{f.provenance === "customer_quote" ? "客户原话" : "测试假设"}</b>：{f.statement}{f.quote && <span> · 原文“{f.quote}”</span>}{f.reason && <span> · {f.reason}</span>}{f.applies_when && <span> · 适用条件：{f.applies_when}</span>}</p>)}
      {scenario.goals.map(g=><p key={g.id}>诉求：{g.description} · 原文“{g.quote}”{v.pending_goals?.includes(g.id) ? " · 尚未覆盖" : ""}</p>)}
      {scenario.limitations.map((s,i)=><p key={i}>限制：{s}</p>)}
    </details>}
    {!!v.checks?.length && <details><summary>逐轮错位检查（{v.checks.length} 次）</summary>{v.checks.map((c,i)=><p key={i}>第 {c.turn} 轮 · {c.delivered ? "已发送" : "未发送"} · {c.verdict === "aligned" ? "承接有效" : c.verdict === "misaligned" ? "对话错位" : "无法确认"}：{c.reason}<br/>拟回复：{c.proposal.text || "（结束或停止）"}{c.proposal.simulated_action && <span> · 模拟操作：{c.proposal.simulated_action}</span>}</p>)}</details>}
  </section>;
}
