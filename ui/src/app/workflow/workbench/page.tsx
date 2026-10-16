"use client";
import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { AgentWorkbench } from "@/components/agent-workbench/AgentWorkbench";
function WorkbenchPage() {
  const params = useSearchParams();
  const id = params.get("workflowId");
  return (
    <AgentWorkbench
      key={params.get("session") || id || "new"}
      workflowId={id && /^\d+$/.test(id) ? Number(id) : undefined}
      sessionId={params.get("session") || undefined}
    />
  );
}
export default function Page() {
  return (
    <Suspense fallback={<p>正在载入工作台…</p>}>
      <WorkbenchPage />
    </Suspense>
  );
}
