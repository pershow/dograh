import type { Edge, Node } from "@xyflow/react";
import type { RuntimeProfileData } from "./RuntimeProfile";

export type WorkbenchSession = {
  id: string;
  workflow_id: number;
  model_protocol: "chat" | "responses";
};
export type WorkbenchSessionListItem = WorkbenchSession & {
  created_at: string;
  active_job_status?: "queued" | "running" | null;
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
export type ReviewCheck = {
  criterion: string;
  category: string;
  reason: string;
  severity?: string; layer?: string; applicability?: string; repair_target?: string;
  citations: { id: string; quote: string; source: string; role?: string; turn?: number; path?: string; title?: string; source_kind?: string; offset?: number; content_hash?: string }[];
};
export type ReplayReview = {
  status: string; reason: string; version?: number;
  warnings?: string[]; basis_fingerprint?: string; business_status?: string; execution_status?: string; coverage?: string;
  checks?: ReviewCheck[];
  draft_review?: { checks: ReviewCheck[] };
  verification?: { reviews: { check_index: number; verdict: string; reason: string; check: ReviewCheck }[] };
};
export type CaseResult = {
  audio_evidence?: import("./AudioEvidence").AudioReplayEvidence | null;
  runtime_profile?: RuntimeProfileData;
  business_state?: Record<string, unknown>;
  mode?: "scripted" | "single_step" | "dynamic";
  history?: {role: string; text: string}[];
  validity?: {
    status: string; mode?: string; scope?: string; stop_reason?: string; source_turn?: number;
    context_is_inferred?: boolean;
    replay_context?: {variables: Record<string, unknown>; assumption: string; limitations: string[]; bindings: {variable: string; value: string; quote: string; reason: string}[]};
    context_selection?: {node_id: string | null; reason: string; history_quote: string; ambiguous: boolean};
    scenario?: {fingerprint: string; facts: {id: string; statement: string; provenance: string; quote: string; reason: string; applies_when?: string}[]; goals: {id: string; description: string; quote: string}[]; limitations: string[]};
    checks?: {turn: number; verdict: string; reason: string; delivered: boolean; proposal: {text: string; simulated_action?: string}}[];
    pending_goals?: string[];
  };
  run_id?: number;
  trace_url?: string | null;
  source?: {sheet?: string; row?: number; column?: string};
  reference_review?: ReplayReview;
  review_pending?: boolean;
  name: string;
  status: string;
  error?: string;
  transcript: { role: string; text: string }[];
  assertions: { kind: string; value: unknown; passed: boolean; turn?: number | null; actual?: unknown; reason?: string; reference_quote?: string; actual_quote?: string; category?: string; informational?: boolean }[];
  tool_calls: unknown[];
};
export type Evaluation = {
  id: string;
  job_id?: string;
  candidate_id: string;
  suite_id: string;
  status: string;
  summary: Record<string, number | string>;
  results: CaseResult[];
};
export type Suite = { id: string; payload: { name: string; cases: unknown[] } };
export type WorkbenchState = {
  target_fingerprint?: string;
  memory_revision?: string;
  revision?: string;
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
export type EventPage = {
  events: WorkbenchEvent[];
  before: number | null;
  after: number;
  has_more: boolean;
};
