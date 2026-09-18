// Generated from api/contract.json and api/openapi.json by scripts/gen-contract.mjs.
// Do not edit. Run `make contract` at the repository root and commit the result.
// openapi.json digest: 033f0e23044029cd

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
export const CONTRACT_HASH = 'c7951dfa23939e88';

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

/**
 * What each event type's `data` carries. A lower bound, like everything here:
 * a newer runtime may add fields, and they must be ignored.
 */
export interface EventPayloads {
  assistant: TextPayload;
  assistant_delta: TextPayload;
  end: RunResultPayload;
  error: ErrorPayload;
  finish: RunResultPayload;
  gate: GatePayload;
  heartbeat: HeartbeatPayload;
  metrics: MetricsPayload;
  plan: PlanPayload;
  quota: QuotaPayload;
  steer: SteerPayload;
  tool_call: ToolCallPayload;
  tool_pending: ToolPendingPayload;
  tool_result: ToolResultPayload;
  turn_start: TurnStartPayload;
  usage: UsagePayload;
  user: UserPayload;
}

// ── shapes ──────────────────────────────────────────────────────────────────
//
// Every request and response body, and every event payload, from
// api/openapi.json. Each is a lower bound (C2): a newer runtime may add fields,
// and they must be ignored.

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

export interface CompactionRecord {
  turn: number;
  reason: string;
  before: number;
  after: number;
  freed: number;
  evicted_messages: number;
  evicted_paths: string[];
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

export interface ErrorPayload {
  message: string;
  /** What failed, when it was not the run itself. */
  where?: string;
  /** What the run did instead. */
  effect?: string;
  kind?: string;
}

export interface Extended {
  id: string;
  extensions: number;
  /** Null when approvals have no timeout. */
  seconds_left: number | null;
}

/** An approval decided by the `auto_safe` policy, not by a person. */
export interface GateAutoApproval {
  kind: 'auto_approval';
  id: string;
  tool: string;
  paths: string[];
  approved: boolean;
  reason: string;
}

export interface GateCompaction {
  kind: 'compaction';
  reason: string;
  strategy: string;
  before: number;
  after: number;
  turns: number[] | null;
  evicted_messages: number;
  evicted_paths: string[];
}

export interface GateForcedToolCall {
  kind: 'forced_tool_call';
  mode: string;
}

export interface GateOverflowRecovery {
  kind: 'overflow_recovery';
  before: number;
  after: number;
  retrying: boolean;
}

/** One of several shapes, told apart by `kind`. */
export type GatePayload = GateRun | GateForcedToolCall | GateToolChoiceUnsupported | GateOverflowRecovery | GatePhase | GateRoutes | GateReplan | GateCompaction | GateAutoApproval;

/** A migration phase closed and the gate is deferred until the last one. */
export interface GatePhase {
  kind: 'phase';
  deferred: boolean;
  closed: string;
  phase: string;
  index: number;
  phases: number;
}

export interface GateReplan {
  kind: 'replan';
  reason: string;
  tried: string[];
}

export interface GateReport {
  ok: boolean;
  seconds: number;
  stages: GateStage[];
  not_run: string[];
  blocked_by: string;
}

/** The route inventory was saved before a migration touched anything. */
export interface GateRoutes {
  kind: 'routes';
  saved: string;
  routes: number;
  unresolved: number;
}

/** The verification gate ran: `inner` over the files just changed, `full` at the end. */
export interface GateRun {
  ok: boolean;
  seconds: number;
  stages: GateStage[];
  not_run: string[];
  blocked_by: string;
  kind: 'inner' | 'full';
  /** A full gate answered from its last run. */
  cached?: boolean;
}

export interface GateStage {
  name: string;
  ok: boolean;
  blocking: boolean;
  /** Why the stage did not run. Empty when it ran. */
  skipped: string;
  seconds: number;
  /** Only for a stage that failed. */
  content?: string;
  truncated?: boolean;
}

export interface GateToolChoiceUnsupported {
  kind: 'tool_choice_unsupported';
  value: string;
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
  /** Tool → version, `installed` when it has no readable version, null when missing. Absent until probed, shortly after start. */
  toolchain?: Record<string, string | null>;
}

/**
 * Declared and never emitted. The stream keeps itself alive with SSE comment
 * frames (`: keep-alive`), which are not events.
 */
export interface HeartbeatPayload {

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

/** What the whole run cost and where it ran out of room. Sent once, before `end`. */
export interface MetricsPayload {
  session_id: string;
  task: string;
  outcome: string;
  turns: number;
  prompt_tokens: number[];
  completion_tokens: number[];
  cached_tokens: number[];
  reasoning_tokens: number;
  budget: number;
  context_window: number;
  compactions: CompactionRecord[];
  evicted_paths: string[];
  evicted_paths_reread: string[];
  truncations: number;
  output_limit: number;
  intercepted_cached: number;
  intercepted_dead_end: number;
  intercepted_re_read: number;
  files_read: string[];
  bytes_read: number;
  bytes_reread: number;
  incomplete: string[];
  peak_prompt_tokens: number;
  total_prompt_tokens: number;
  peak_pct_of_budget: number;
  peak_pct_of_window: number;
  pressed_the_ceiling: boolean;
  lost_work: boolean;
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

export interface Mutation {
  path: string;
  kind: 'create' | 'modify' | 'delete';
  /** Computed by the runtime. Never recompute it client-side. */
  protected: boolean;
}

export interface Phase {
  name: string;
  covers: string;
  /** Comma-separated. */
  parts: string;
  status: 'pending' | 'done';
}

export interface PlanItem {
  index: number;
  file: string;
  action: string;
  accepts: string;
  status: 'pending' | 'written' | 'done' | 'failed' | 'skipped' | 'blocked';
  note: string;
}

export interface PlanPayload {
  text: string;
  steps: number;
  items: PlanItem[];
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

/** A signal to re-read `GET /v1/quota`. It carries no numbers on purpose. */
export interface QuotaPayload {
  reason: string;
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

/** `finish`, then `end`: how the run ended. */
export interface RunResultPayload {
  outcome: 'done' | 'aborted' | 'unverified' | 'no_progress' | 'exhausted' | 'error';
  summary: string;
  turns: number;
  mutations: string[];
  gate: GateReport | null;
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

/** A correction the running loop has read. */
export interface SteerPayload {
  text: string;
  turn: number;
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
  /** `interactive` (the default): a person answers every approval. `auto_safe`: decided by rule, for a caller with nobody to ask; protected files, deletions and new dependencies are refused. */
  approval_policy?: 'interactive' | 'auto_safe';
}

/** `assistant` (the whole reply) and `assistant_delta` (a streamed piece of it). */
export interface TextPayload {
  text: string;
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

export interface ToolCallPayload {
  id: string;
  name: string;
  /** As the model sent them, parsed when they parse. */
  arguments: unknown;
  turn: number;
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

/** An approval being raised. `id` is what `POST /v1/approvals/{id}` takes. */
export interface ToolPendingPayload {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  paths: string[];
  unconditional: boolean;
  protected: string[];
  turn: number;
}

/**
 * A tool call's outcome, or a call that was answered without being run.
 *
 * `mutations` is absent when nothing was dispatched: a call skipped, refused
 * for the output limit, or answered from a ledger.
 */
export interface ToolResultPayload {
  id: string;
  name: string;
  ok: boolean;
  content: string;
  turn: number;
  mutations?: Mutation[];
  fix?: string;
  truncated?: boolean;
  /** Server-measured; excludes the approval wait. */
  ms?: number;
  meta?: Record<string, unknown>;
  /** False when the call never ran. */
  dispatched?: boolean;
  /** A hook answered in the tool's place. */
  hooked?: boolean;
  /** Answered from a ledger. `ok` stays true: the content is current. */
  intercepted?: boolean;
  intercept?: string;
  arguments?: unknown;
  truncated_by_output_limit?: boolean;
  output_limit?: number;
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

export interface TurnStartPayload {
  turn: number;
  mode: 'ask' | 'planner' | 'agent';
  intent: 'auto' | 'ask' | 'agent';
  /** Whether a person chose the intent or it was classified. */
  intent_source: string;
  intent_why: string;
  /** The attempt about to be made: 1, then 2 after a failed gate. */
  attempt: number;
}

export interface UsagePayload {
  prompt_tokens: number;
  completion_tokens: number;
  /** Null until the endpoint reports it. Show it as not reported, not as 0%. */
  cached_tokens: number | null;
  budget: number;
  budget_used_pct: number;
  reasoning_tokens: number;
  estimate_error: number;
  prefix_break: string;
  /** Only on the anomaly: reasoning charged in a thinking-off mode. */
  reasoning_leaked?: number;
}

/** A message the developer sent: the task, a follow-up or a correction. */
export interface UserPayload {
  text: string;
  turn: number;
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
