import { useState } from "react";

import type { CaseResult, Evaluation, WorkbenchState } from "./types";
import styles from "./workbench.module.css";

const resultLabels: Record<string, string> = {
  passed: "通过", failed: "未通过", error: "执行错误", inconclusive: "无法判定",
  running: "进行中", cancelled: "已取消", queued: "排队中",
};
const finished = (result: CaseResult) => ["passed", "failed", "error", "inconclusive"].includes(result.status);
const groupId = (evaluation: Evaluation) => evaluation.job_id || evaluation.id;

/** Saved evidence is browsable independently of the canvas version or active task. */
export function EvaluationArchive({ state, onOpen }: {
  state: WorkbenchState;
  onOpen: (evaluation: Evaluation, name: string) => void;
}) {
  const [task, setTask] = useState("");
  const [filter, setFilter] = useState("finished");
  const [query, setQuery] = useState("");
  const groupIds = new Set(state.evaluations.map(groupId));
  const groups = [...state.jobs.map(j => j.id).filter(id => groupIds.has(id)),
    ...[...groupIds].filter(id => !state.jobs.some(j => j.id === id))];
  const selectedTask = groupIds.has(task) ? task : groups[0];
  const evaluations = state.evaluations.filter(e => groupId(e) === selectedTask);
  const rows = evaluations.flatMap(evaluation => evaluation.results.map(result => ({ evaluation, result })));
  const completed = rows.filter(row => finished(row.result)).length;
  const running = rows.filter(row => row.result.status === "running").length;
  const visible = rows.filter(({result}) =>
    (filter === "all" || (filter === "finished" ? finished(result) : result.status === filter)) &&
    result.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  if (!groups.length) return null;

  return <section className={styles.evaluationArchive} aria-label="已保存测试结果">
    <strong>已保存测试结果</strong>
    <p>已完成 {completed} 条 · 进行中 {running} 条 · 共 {evaluations.length} 个批次。任务运行期间也可查看已保存的对话和评审。</p>
    <div className={styles.archiveFilters}>
      <label>测试任务<select aria-label="结果所属任务" value={selectedTask} onChange={e => setTask(e.target.value)}>
        {groups.map((id, i) => {
          const job = state.jobs.find(j => j.id === id);
          return <option key={id} value={id}>任务 {groups.length - i} · {id.slice(0, 8)} · {job?.status === "running" ? "运行中" : job?.status === "completed" ? "已完成" : job?.status === "failed" ? "失败，保留已有结果" : job?.status === "cancelled" ? "已取消，保留已有结果" : "已保存"}</option>;
        })}
      </select></label>
      <label>结果筛选<select aria-label="结果筛选" value={filter} onChange={e => setFilter(e.target.value)}>
        <option value="finished">已完成</option><option value="all">全部已保存</option>
        {Object.entries(resultLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      <input aria-label="搜索测试用例" placeholder="搜索用例名称" value={query} onChange={e => setQuery(e.target.value)} />
    </div>
    <div className={styles.archiveTable} role="region" aria-label="跨批次结果列表" tabIndex={0}>
      <table>
        <thead><tr><th>测试用例</th><th>批次</th><th>结果</th><th>详情</th></tr></thead>
        <tbody>{visible.map(({evaluation, result}) => <tr key={`${evaluation.id}:${result.name}`}>
          <td>{result.name}{result.review_pending && <small> · 评审待完成</small>}</td>
          <td>{state.suites.find(s => s.id === evaluation.suite_id)?.payload.name || evaluation.suite_id.slice(0, 8)}</td>
          <td>{resultLabels[result.status] || result.status}</td>
          <td><button type="button" onClick={() => onOpen(evaluation, result.name)} aria-label={`查看 ${result.name} 的结果`}>查看对话与评审</button></td>
        </tr>)}</tbody>
      </table>
      {!visible.length && <p>{rows.length ? "没有符合筛选条件的结果，可切换筛选或清除搜索。" : "本次任务尚未保存用例结果，已有历史结果可切换测试任务查看。"}</p>}
    </div>
  </section>;
}
