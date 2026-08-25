/**
 * The wire contract, in one place.
 *
 * **Everything here is additive-only (C2).** Unknown event types and unknown
 * fields are ignored rather than treated as errors, which is what lets the
 * `.vsix` and the Python wheel version independently — and they will, because
 * one ships through a package registry and the other through a venv. Every type
 * below is therefore a *lower bound* on what may arrive, never an exhaustive
 * description, and no code may switch on an event type without a default arm.
 */

/** Contract version this build pins against. Compared with `/v1/health`. */
export const API_VERSION = '1.0';

// ── events (C2) ─────────────────────────────────────────────────────────────

export type EventType =
  | 'turn_start'
  | 'assistant'
  | 'assistant_delta'
  | 'tool_call'
  | 'tool_pending'
  | 'tool_result'
  | 'plan'
  | 'gate'
  | 'usage'
  | 'quota'
  | 'steer'
  | 'finish'
  | 'error'
  | 'heartbeat'
  | 'end';

export interface WireEvent {
  /** Monotonic, server-assigned. What `since_id` and `Last-Event-ID` resume from. */
  id: number;
  type: EventType | string;
  data: Record<string, unknown>;
}

export interface TurnStart {
  turn: number;
  mode: Mode;
  /** 1 or 2. The retry after a failed gate, which is what a developer is watching. */
  attempt?: number;
}

export interface AssistantText {
  text: string;
}

export interface PlanEvent {
  text: string;
  /** A count, not a list. The steps are parsed from `text`; see `parsePlan`. */
  steps: number;
}

export interface ToolCallEvent {
  id: string;
  name: string;
  arguments: unknown;
}

export interface Mutation {
  path: string;
  kind: 'create' | 'modify' | 'delete';
  /**
   * Computed server-side against `PROTECTED_GLOBS`. Never recomputed here: the
   * matcher is custom rather than `fnmatch`, so a reimplementation disagrees at
   * exactly the edges that matter, and it would be a security-relevant constant
   * duplicated across the seam with no test binding the copies.
   */
  protected: boolean;
}

export interface ToolResultEvent {
  id: string;
  name: string;
  ok: boolean;
  content: string;
  mutations: Mutation[];
  fix?: string;
  truncated?: boolean;
  /** Server-measured, so it excludes the approval wait — the developer's time. */
  ms?: number;
}

export interface ApprovalEvent {
  /** Minted with the request. `POST /v1/approvals/{id}` takes this. */
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
  reason: string;
  paths: string[];
  /** The subset of `paths` that is protected, and therefore never auto-approved. */
  protected: string[];
  unconditional: boolean;
  /**
   * Present on `GET /v1/approvals`, absent on the `tool_pending` event (which is
   * emitted the instant the approval is raised, when the answer is always the
   * full timeout).
   *
   * Reported by the server rather than counted locally: a client counting from
   * when it happened to *see* the card is wrong by however long the panel was
   * hidden, and would show a guess about the server's clock as if it were a fact.
   */
  seconds_left?: number;
  extensions?: number;
  session_id?: string;
}

export interface GateStage {
  name: string;
  ok: boolean;
  blocking: boolean;
  skipped: string;
  seconds: number;
  /** Only present for stages that failed. Absent on a clean run, by design. */
  content?: string;
  truncated?: boolean;
}

export interface GateEvent {
  kind: 'inner' | 'full' | 'compaction';
  ok: boolean;
  seconds: number;
  stages: GateStage[];
  not_run: string[];
  blocked_by: string;
  /** compaction only */
  before?: number;
  after?: number;
}

export interface UsageEvent {
  prompt_tokens: number;
  completion_tokens: number;
  /**
   * Null until the endpoint reports `prompt_tokens_details`. Render the segment
   * as "not reported" and never as `cache 0%`, which reads as a failure rather
   * than an unknown.
   */
  cached_tokens: number | null;
  /** The absolute denominator, so nothing has to divide to recover it. */
  budget: number;
  budget_used_pct: number;
  reasoning_tokens: number;
  estimate_error?: number;
  /** Present only on the anomaly: reasoning charged in a thinking-off mode. */
  reasoning_leaked?: number;
}

export interface FinishEvent {
  outcome: SessionStatus;
  summary: string;
  turns: number;
  mutations: string[];
}

export type Mode = 'planner' | 'scaffolder' | 'coder' | 'verifier' | 'debugger';

export type SessionStatus =
  | 'running'
  | 'done'
  | 'unverified'
  | 'no_progress'
  | 'exhausted'
  | 'error'
  | 'aborted';

const RESUMABLE: ReadonlySet<string> = new Set([
  'unverified',
  'no_progress',
  'exhausted',
  'error',
  'aborted',
]);

/**
 * Whether the runtime will accept `POST /v1/sessions/{id}/resume`.
 *
 * `done` is deliberately absent: a finished run takes a *follow-up*, which is a
 * new turn on the existing transcript, not a retry of the last one. Resuming a
 * successful run would re-enter the gate loop on a change that already passed.
 */
export function isResumable(status: string): boolean {
  return RESUMABLE.has(status);
}

// ── REST shapes ─────────────────────────────────────────────────────────────

export interface SessionSummary {
  id: string;
  task: string;
  workspace: string;
  status: SessionStatus;
  created_at: string;
  finished_at: string | null;
  summary: string;
  mutations: string[];
  events: number;
  resumable: boolean;
  queued: number;
  winding_down: boolean;
  transcript?: WireEvent[];
  pending_approvals?: ApprovalEvent[];
}

export interface Health {
  ok: boolean;
  api_version: string;
  version: string;
  workspace: string;
  gateway: string;
  ready: { prewarmed: boolean; latency_ms?: number; reason?: string };
  sessions: { total: number; running: number };
}

export interface RevertPlan {
  session_id: string;
  restore: string[];
  delete: string[];
  blocked: { path: string; reason: string }[];
}

export interface ContextSnapshot {
  mode: Mode;
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
}

export interface QuotaWindow {
  used: number;
  cap: number;
  resets_in?: number;
}

export interface QuotaSnapshot {
  window?: QuotaWindow & { opened_at?: string; expires_in?: number; runs?: QuotaWindow };
  week?: QuotaWindow & { sessions?: QuotaWindow };
  hour?: QuotaWindow;
  role?: string;
  tier?: string;
  /** Whichever limit is closest to biting. What the status bar shows. */
  tightest?: { name: string; used: number; cap: number; pct: number };
}

// ── plan parsing ────────────────────────────────────────────────────────────

export interface PlanStep {
  index: number;
  text: string;
  accepts: string;
  /**
   * Always `unknown` today. No field on the wire carries per-step status, and
   * no client-side heuristic can honestly infer it — tying "gate passed" to
   * "step advanced" would be a fabrication. Rendered as a dash with a sentence
   * saying why, which is the true thing.
   */
  status: 'unknown' | 'pending' | 'running' | 'passed' | 'failed';
}

/** The server's own step regex, so client and server agree on the count. */
const STEP = /^\s*\d+[.)]\s/;

export function parsePlan(text: string): { goal: string; steps: PlanStep[]; scope: string[] } {
  const lines = text.split(/\r?\n/);
  const steps: PlanStep[] = [];
  const scope: string[] = [];
  let goal = '';

  let current: PlanStep | null = null;
  for (const line of lines) {
    const path = line.match(/\b([\w./-]+\.go|go\.mod|[\w./-]+\.sql|[\w./-]+\.ya?ml)\b/g);
    if (path) scope.push(...path);

    if (STEP.test(line)) {
      current = {
        index: steps.length + 1,
        text: line.replace(STEP, '').trim(),
        accepts: '',
        status: 'unknown',
      };
      steps.push(current);
      continue;
    }
    const accepts = line.match(/^\s*Accepts?:\s*(.+)$/i);
    if (accepts && current) {
      current.accepts = accepts[1].trim();
      continue;
    }
    if (!goal && line.trim() && !STEP.test(line)) goal = line.trim();
  }
  return { goal, steps, scope: [...new Set(scope)] };
}
