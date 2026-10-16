import type { Edge, Node } from "@xyflow/react";

export type WorkbenchSession = {
  id: string;
  workflow_id: number;
  model_protocol: "chat" | "responses";
};
export type Snapshot = {
  name: string;
  workflow_json: { nodes: Node[]; edges: Edge[] };
  workflow_configurations: Record<string, unknown>;
  template_context_variables: Record<string, unknown>;
};
export type Candidate = {
  id: string;
  name: string;
  summary: string;
  code: string;
  fingerprint: string;
  snapshot?: Snapshot;
};
export type Asset = {
  id: string;
  name: string;
  byte_size: number;
  status: string;
  error?: string;
};
export type Job = {
  id: string;
  kind: string;
  status: string;
  error?: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
};
export type CaseResult = {
  name: string;
  status: string;
  error?: string;
  transcript: { role: string; text: string }[];
  assertions: { kind: string; value: unknown; passed: boolean }[];
  tool_calls: unknown[];
};
export type Evaluation = {
  id: string;
  candidate_id: string;
  suite_id: string;
  status: string;
  summary: Record<string, number | string>;
  results: CaseResult[];
};
export type Suite = { id: string; payload: { name: string; cases: unknown[] } };
export type WorkbenchState = {
  session: WorkbenchSession;
  target: Snapshot;
  assets: Asset[];
  candidates: Candidate[];
  jobs: Job[];
  suites: Suite[];
  evaluations: Evaluation[];
};
export type WorkbenchEvent = {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};
