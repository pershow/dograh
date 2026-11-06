"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet } from "@/client/sdk.gen";
import { createWorkflowApiV1WorkflowCreateDefinitionPost } from "@/client/sdk.gen";
import { WorkflowBuilder } from "@/components/workflow-builder/WorkflowBuilder";
import { AgentWorkbench } from "@/components/agent-workbench/AgentWorkbench";
import { PageShell } from "@/components/layout/PageShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { detailFromError } from "@/lib/apiError";
import { useAuth } from "@/lib/auth";

export default function CreateWorkflowPage() {
  const { user, loading } = useAuth();
  const [enabled, setEnabled] = useState<boolean>();
  const [legacyBuilder, setLegacyBuilder] = useState(true);
  const [capabilityError, setCapabilityError] = useState("");
  useEffect(() => {
    if (loading || !user) return;
    let disposed = false;
    workbenchCapabilitiesApiV1AgentWorkbenchCapabilitiesGet()
      .then((response) => {
        if (disposed) return;
        if (response.error)
          setCapabilityError(
            detailFromError(
              response.error,
              "Unable to load Agent Builder configuration",
            ),
          );
        else {
          setEnabled(response.data?.data.enabled === true);
          setLegacyBuilder(response.data?.data.legacy_builder !== false);
        }
      })
      .catch(() => {
        if (!disposed) setCapabilityError("Unable to connect to Agent Builder");
      });
    return () => {
      disposed = true;
    };
  }, [loading, user]);
  if (capabilityError)
    return (
      <p role="alert" className="p-8">
        {capabilityError}
      </p>
    );
  if (loading || enabled === undefined)
    return <p className="p-8 text-muted-foreground">Loading Agent Builder…</p>;
  return enabled ? (
    <AgentWorkbench />
  ) : legacyBuilder ? (
    <PageShell className="flex min-h-[calc(100vh-80px)] max-w-[1500px] flex-col py-6 md:px-8"><WorkflowBuilder /></PageShell>
  ) : (
    <BlankAgentBuilder />
  );
}

function BlankAgentBuilder() {
  const router = useRouter();
  const [name, setName] = useState("New Agent");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <PageShell width="narrow">
      <h1 className="text-2xl font-semibold">Create Voice Agent</h1>
      <p className="my-4 text-muted-foreground">
        从空白画布开始，配置你自己的模型连接。
      </p>
      <Label htmlFor="blank-agent-name">Agent name</Label>
      <Input
        id="blank-agent-name"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      {error && <p role="alert">{error}</p>}
      <Button
        className="mt-4"
        disabled={busy || !name.trim()}
        onClick={async () => {
          setBusy(true);
          setError("");
          try {
            const response =
              await createWorkflowApiV1WorkflowCreateDefinitionPost({
                body: {
                  name: name.trim(),
                  workflow_definition: { nodes: [], edges: [] },
                  draft_only: true,
                },
              });
            if (response.error)
              throw new Error(detailFromError(response.error));
            router.push(`/workflow/${response.data!.id}`);
          } catch (err) {
            setError(
              err instanceof Error ? err.message : "Unable to create agent",
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        创建空白 Agent
      </Button>
    </PageShell>
  );
}
