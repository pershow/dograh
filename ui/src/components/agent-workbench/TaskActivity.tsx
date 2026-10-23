import { Check, ChevronRight, Circle, Loader2, X } from "lucide-react";
import { useState } from "react";

import type { ActivityStep } from "./activity";
import { DiagnosticEvidence } from "./DiagnosticEvidence";
import { EvidenceReads } from "./EvidenceReads";
import styles from "./workbench.module.css";

export function TaskActivity({ steps, workflowId, running = false }: { steps: ActivityStep[]; workflowId: number; running?: boolean }) {
  const [expanded, setExpanded] = useState<boolean>();
  const open = expanded ?? running;
  const labels = { running: "进行中", done: "已完成", failed: "未完成", recorded: "已记录", cancelled: "已取消" };
  return <section className={styles.activity} aria-label="处理过程">
    <button type="button" className={styles.activityToggle} aria-expanded={open} onClick={() => setExpanded(!open)}>
      <ChevronRight size={14} className={open ? styles.activityExpanded : undefined} />
      <span>处理过程 · {steps.length} 步</span>
      {running && <Loader2 size={13} className="animate-spin" aria-label="任务进行中" />}
    </button>
    {!open && <p className={styles.activitySummary}>{steps.at(-1)?.text}</p>}
    {open && <ol className={styles.activitySteps}>
      {steps.map(step => <li key={step.sequence} data-event-sequence={step.sequence} data-status={step.status}>
        <span className={styles.activityIcon} title={step.statusLabel || labels[step.status]} aria-label={step.statusLabel || labels[step.status]}>
          {step.status === "running" ? <Loader2 size={13} className="animate-spin" /> : step.status === "done" ? <Check size={13} /> : step.status === "failed" ? <X size={13} /> : <Circle size={10} />}
        </span>
        <div><span>{step.text}</span>{step.detail && <p>{step.detail}</p>}
          {step.evidence && <EvidenceReads events={step.evidence} workflowId={workflowId} />}
          {step.diagnostic && <DiagnosticEvidence data={step.diagnostic} />}
        </div>
        {step.elapsed !== undefined && <small>{step.elapsed.toFixed(1)} 秒</small>}
      </li>)}
    </ol>}
  </section>;
}
