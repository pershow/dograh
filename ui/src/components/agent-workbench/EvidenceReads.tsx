import { FileText, Paperclip } from "lucide-react";
import Link from "next/link";

import type { WorkbenchEvent } from "./types";
import styles from "./workbench.module.css";

type MessageRow = WorkbenchEvent & { evidence?: WorkbenchEvent[] };

export function groupEvidence(events: WorkbenchEvent[]): MessageRow[] {
  const rows: MessageRow[] = [];
  for (const event of events) {
    if (event.type !== "evidence.read") {
      rows.push(event);
      continue;
    }
    const previous = rows.at(-1);
    if (previous?.evidence) previous.evidence.push(event);
    else rows.push({ ...event, evidence: [event] });
  }
  return rows;
}

export function EvidenceReads({ events, workflowId }: {
  events: WorkbenchEvent[];
  workflowId: number;
}) {
  const sections: Record<string, string> = {
    transcript: "对话", variables: "变量", logs: "日志", logs_raw: "原始日志",
    definition: "运行版本", overview: "概览",
  };
  const runs = [...new Set(events.filter(e => e.payload.source === "agent_runs").map(e => e.payload.run_id))];
  const reused = events.every(e => e.payload.reused);
  return (
    <details className={styles.evidence}>
      <summary>{reused ? "复用" : "已读取"}{runs.length === 1 ? `运行 #${runs[0]}` : "参考资料"} · {events.length} 项资料</summary>
      {events.map(({ sequence, payload }) => payload.source === "agent_runs" ? (
        <Link key={sequence} href={`/workflow/${workflowId}/run/${payload.run_id}`} target="_blank">
          <FileText size={12} /> {payload.reused ? "复用" : "已读取"}运行 #{String(payload.run_id)} · {sections[String(payload.section)] || String(payload.section)}
        </Link>
      ) : payload.source === "evaluation" ? (
        <span key={sequence}><FileText size={12} /> 已读取 · {String(payload.name || "测试结果")}</span>
      ) : (
        <span key={sequence}><Paperclip size={12} /> 按需读取 · {String(payload.name)}</span>
      ))}
    </details>
  );
}
