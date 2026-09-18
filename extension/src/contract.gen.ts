// Generated from api/contract.json and api/openapi.json by scripts/gen-contract.mjs.
// Do not edit. Run `make contract` at the repository root and commit the result.
// openapi.json digest: 7d268f2d0b7107ac

/**
 * The runtime API this build speaks. A mismatch with `/v1/health` is refused
 * at connect time.
 */
export const API_VERSION = '1.1';

/**
 * A hash of the contract this build was generated from. `/v1/health` reports
 * the runtime's. When only this differs, the runtime speaks the same version
 * with additions this build does not know about. That is legal under C2, so it
 * is logged, not refused.
 */
export const CONTRACT_HASH = '60c910b774fc2063';

/**
 * Every event type the runtime can emit (C2). A lower bound: a newer runtime
 * may send types not listed here, and they must be ignored.
 */
export type EventType =
  | 'assistant'
  | 'assistant_delta'
  | 'end'
  | 'error'
  | 'finish'
  | 'gate'
  | 'heartbeat'
  | 'metrics'
  | 'plan'
  | 'quota'
  | 'steer'
  | 'tool_call'
  | 'tool_pending'
  | 'tool_result'
  | 'turn_start'
  | 'usage'
  | 'user';

// ── REST shapes ─────────────────────────────────────────────────────────────
//
// Every request and response body, from api/openapi.json. Each is a lower
// bound (C2): a newer runtime may add fields, and they must be ignored.

export interface Aborting {
  aborting: string;
  status: 'running' | 'done' | 'unverified' | 'no_progress' | 'exhausted' | 'error' | 'aborted';
}

export interface AgendaList {
  tasks: AgendaTask[];
  state: string;
}

export interface AgendaMove {
  state: 'proposed' | 'approved' | 'done' | 'dropped';
  by?: string;
  note?: string;
}

export interface AgendaProposal {
  title: string;
  why?: string;
  paths?: string[];
  /** 1 (highest) to 5. Defaults to 3. */
  priority?: number;
  /** The session that proposed it. */
  session_id?: string;
}

export interface AgendaTask {
  id: string;
  title: string;
  why: string;
  state: 'proposed' | 'approved' | 'done' | 'dropped';
  paths: string[];
  priority: number;
  origin_session: string;
  created_at: string;
  updated_at: string;
  decided_by: string;
  note: string;
}

export interface Approval {
  id: string;
  session_id: string;
  /** Null when approvals have no timeout. */
  seconds_left: number | null;
  extensions: number;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  paths: string[];
  unconditional: boolean;
  protected: string[];
}

export interface ApprovalList {
  approvals: Approval[];
}

export interface Blocked {
  path: string;
  reason: string;
}

export interface CanonicalRow {
  seq: number;
  role: string;
  turn: number;
  tool: string;
  path: string | null;
  visibility: string;
  characters: number;
  content: string;
}

export interface CompactionReport {
  session_id: string;
  strategy: string;
  before: number;
  after: number;
  evicted_messages: number;
  evicted_paths: string[];
  goal: string;
}

export interface CompactionState {
  recap: Recap;
  source_seq: number;
  source_prefix_hash: string;
  source_count: number;
  source_tokens: number;
  source_paths: string[];
  strategy: string;
  turn: number;
  created_at: string;
  generation: number;
}

export interface ContextSnapshot {
  mode: 'ask' | 'planner' | 'agent';
  turn: number;
  total_tokens: number;
  budget: number;
  used_pct: number;
  tool_schema_tokens: number;
  by_layer: Record<string, number>;
  messages: number;
  compactions: number;
  stale_slices: number;
  calibrated: boolean;
  prefix: string;
  canonical_records: number;
  compacted_records: number;
  compaction_stale: boolean;
  elided_records: number;
  elided_lines: number;
  collapsed_echoes: number;
}

export interface CredentialAccepted {
  ok: boolean;
  fingerprint: string;
}

export interface CredentialRequest {
  /** A fresh gateway JWT. Only a fingerprint is echoed back. */
  jwt: string;
}

export interface Decision {
  id: string;
  decision: 'accept' | 'reject' | 'edit';
}

export interface DecisionRequest {
  decision?: 'accept' | 'reject' | 'edit';
  /** Required for `edit`: the corrected arguments. */
  arguments?: Record<string, unknown>;
}

export interface Deleted {
  deleted: string;
}

export interface Error {
  error: string;
}

export interface Extended {
  id: string;
  extensions: number;
  /** Null when approvals have no timeout. */
  seconds_left: number | null;
}

/** The fields after `version` need the token and are absent without it. */
export interface Health {
  ok: boolean;
  api_version: string;
  contract_hash: string;
  version: string;
  workspace?: string;
  gateway?: string;
  ready?: Readiness;
  sessions?: SessionCounts;
}

export interface MessageRequest {
  text: string;
  intent?: 'auto' | 'ask' | 'agent';
  /**
   * Read as `intent` by older clients.
   * @deprecated
   */
  mode?: string;
}

export interface Migration {
  active: boolean;
  branch: string;
  base: string;
  closed: number;
  log: string[];
  phases: Phase[];
}

export interface ModelRow {
  seq: number;
  role: string;
  layer: string;
  turn: number;
  path: string | null;
  line_range: number[] | null;
  characters: number;
  content: string;
}

export interface Phase {
  name: string;
  covers: string;
  /** Comma-separated. */
  parts: string;
  status: 'pending' | 'done';
}

export interface PlanRecord {
  session_id: string;
  summary: string;
  forced: boolean;
  updated_at: string;
  migration: Migration;
  steps: PlanStep[];
  revisions: PlanRevision[];
}

export interface PlanRevision {
  at: string;
  /** `submitted`, `replanned` (the loop) or `revised` (the model). */
  cause: string;
  summary: string;
  reason: string;
  steps: PlanStep[];
}

export interface PlanStep {
  file: string;
  action: string;
  accepts: string;
  phase: string;
  part: string;
  status: 'pending' | 'written' | 'done' | 'failed' | 'skipped' | 'blocked';
  note: string;
}

export interface Readiness {
  prewarmed: boolean;
  latency_ms?: number;
  reason?: string;
}

export interface Recap {
  goal: string;
  plan_step: string;
  files_created: string[];
  files_modified: string[];
  files_read: string[];
  decisions: string[];
  findings: string[];
  verified: string[];
  open_items: string[];
  do_not_retry: string[];
  /** First and last turn the recap covers. */
  turns: number[];
}

export interface ResumeRequest {
  note?: string;
}

export interface RevertPlan {
  session_id: string;
  restore: string[];
  delete: string[];
  /** Changed, but cannot be reverted. */
  blocked: Blocked[];
}

export interface Session {
  id: string;
  task: string;
  workspace: string;
  status: 'running' | 'done' | 'unverified' | 'no_progress' | 'exhausted' | 'error' | 'aborted';
  created_at: string;
  finished_at: string | null;
  summary: string;
  mutations: string[];
  events: number;
  resumable: boolean;
  /** Corrections typed during the run and not yet read. */
  queued: number;
  /** Messages the developer has sent in this conversation. */
  turns: number;
  winding_down: boolean;
}

export interface SessionCounts {
  total: number;
  running: number;
}

export interface SessionDetail {
  id: string;
  task: string;
  workspace: string;
  status: 'running' | 'done' | 'unverified' | 'no_progress' | 'exhausted' | 'error' | 'aborted';
  created_at: string;
  finished_at: string | null;
  summary: string;
  mutations: string[];
  events: number;
  resumable: boolean;
  /** Corrections typed during the run and not yet read. */
  queued: number;
  /** Messages the developer has sent in this conversation. */
  turns: number;
  winding_down: boolean;
  /** Present with `?transcript=true`. */
  transcript?: WireEvent[];
  pending_approvals: Approval[];
}

export interface SessionList {
  sessions: Session[];
}

export interface TaskRequest {
  task: string;
  intent?: 'auto' | 'ask' | 'agent';
  /**
   * Read as `intent` by older clients.
   * @deprecated
   */
  mode?: string;
  acceptance?: string[];
}

export interface Tool {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  modes: string[];
  mutates: boolean;
  approval: 'none' | 'conditional' | 'always';
  provider: 'python' | 'gotools' | 'gopls';
  gate_only?: boolean;
  unavailable?: string;
  instead?: string;
}

/** Contract C1. The published copy is `api/tool-catalog.json`. */
export interface ToolCatalog {
  component?: string;
  version?: string;
  contract: string;
  limits?: ToolLimits;
  visible_per_mode?: Record<string, string[]>;
  tools: Tool[];
}

export interface ToolLimits {
  max_params: number;
  max_description: number;
}

/** `view=canonical` returns `CanonicalRow` rows, anything else `ModelRow` rows. */
export interface TranscriptView {
  session_id: string;
  view: string;
  records: number;
  returned: number;
  compaction: CompactionState | null;
  messages: (CanonicalRow | ModelRow)[];
}

/** FastAPI's own 422, for a path or query parameter of the wrong type. */
export interface ValidationFailure {
  detail: Record<string, unknown>[];
}

export interface WindingDown {
  id: string;
  winding_down: boolean;
}

/** One stored event (C2). `type` is a lower bound: ignore unknown types. */
export interface WireEvent {
  id: number;
  type: string;
  data: Record<string, unknown>;
  at: string;
}
