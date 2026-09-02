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
/**
 * The runtime contract this build speaks. A mismatch is refused at connect
 * time rather than discovered later in a way nobody attributes.
 *
 * **1.1** — the mode vocabulary changed: five modes became `ask`, `planner`
 * and `agent`. A 1.0 client is sent values its `Mode` union does not carry.
 * The extension and the runtime ship in the same `.vsix`, so this only ever
 * fires on a hand-mixed pair — which is exactly when it should.
 */
export const API_VERSION = '1.1';

// ── events (C2) ─────────────────────────────────────────────────────────────

export type EventType =
  | 'turn_start'
  | 'assistant'
  | 'assistant_delta'
  | 'user'
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
  /** Why the run is in that mode. Carried on every turn. */
  intent?: Intent;
  /** How many failing gates have come back with nothing edited between them. */
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
  /**
   * The answer came from a ledger; no call was dispatched.
   *
   * `ok` stays true because the content IS the current answer — marking it
   * failed would read as an error and invite a retry. But a row that never ran
   * must not look identical to one that did: the field transcript showed four
   * green `patch_file` ticks against a file that never changed, and that is
   * what made a seventeen-turn deadlock look like progress.
   *
   * Optional, so an older runtime that never sends it keeps working.
   */
  intercepted?: boolean;
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

/**
 * What the model may do this turn.
 *
 * Three, where there were five. Coder, Scaffolder, Verifier and Debugger were a
 * fixed pipeline every request walked; they are one `agent` that holds every
 * tool. The retired names are still in the union because a stored session and a
 * saved setting can carry one, and the runtime coerces every one of them.
 */
export type Mode =
  | 'ask'
  | 'planner'
  | 'agent'
  /** Retired. Accepted on the wire, mapped by the runtime. */
  | 'scaffolder'
  | 'coder'
  | 'verifier'
  | 'debugger';

/**
 * What the developer asked for, decided before the first turn.
 *
 * `auto` classifies with one cheap schema-constrained call; `ask` is read-only;
 * `agent` plans and then does the work. This is the field `POST /v1/tasks`
 * takes now - `mode` is still read as a synonym for a client that has not been
 * rebuilt.
 */
export type Intent = 'auto' | 'ask' | 'agent';

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

/**
 * Shape a `/v1/quota` body for the surfaces here, whichever envelope it came in.
 *
 * The gateway's `Snapshot.as_dict()` sends its counters flat — `used` and
 * `limits` keyed by series (`window_tokens`, `week_sessions`, …), a boolean
 * `window_open`, an ISO `window_expires_at`, and `tightest: {limit, used_pct}`.
 * Every renderer here reads the nested view above. Casting one to the other
 * left `tightest.name` undefined and the tooltip died on `escape(undefined)` —
 * "quota refresh failed: Cannot read properties of undefined (reading
 * 'replace')" on every refresh, for a payload that was perfectly good.
 *
 * Newer gateways emit both envelopes; older ones only the flat one. Nested
 * fields win when present, the flat ones fill in when not, and the result is
 * always a `QuotaSnapshot` with nothing undefined where a renderer will read a
 * string or a number. `now` is injectable so the derived `expires_in` is
 * testable.
 */
export function normaliseQuota(raw: unknown, now = Date.now()): QuotaSnapshot {
  if (!isRecord(raw)) return {};
  const used = isRecord(raw.used) ? raw.used : {};
  const limits = isRecord(raw.limits) ? raw.limits : {};
  const counter = (series: string): QuotaWindow | undefined =>
    typeof limits[series] === 'number' ? { used: num(used[series]), cap: num(limits[series]) } : undefined;

  const out: QuotaSnapshot = {};
  if (typeof raw.role === 'string' && raw.role) out.role = raw.role;
  if (typeof raw.tier === 'string' && raw.tier) out.tier = raw.tier;

  const window = readWindow(raw.window);
  if (window) {
    out.window = window;
  } else if (raw.window_open === true) {
    const tokens = counter('window_tokens') ?? { used: 0, cap: 0 };
    const runs = counter('window_runs');
    const expiresIn = secondsUntil(raw.window_expires_at, now);
    out.window = {
      ...tokens,
      ...(typeof raw.window_opened_at === 'string' ? { opened_at: raw.window_opened_at } : {}),
      ...(expiresIn === undefined ? {} : { expires_in: expiresIn }),
      ...(runs ? { runs } : {}),
    };
  }

  const week = readWindow(raw.week);
  if (week) {
    out.week = week;
  } else {
    const tokens = counter('week_tokens');
    const sessions = counter('week_sessions');
    if (tokens || sessions) out.week = { ...(tokens ?? { used: 0, cap: 0 }), ...(sessions ? { sessions } : {}) };
  }

  const hour = readWindow(raw.hour);
  if (hour) {
    out.hour = hour;
  } else {
    const tokens = counter('hour_tokens');
    if (tokens) out.hour = tokens;
  }

  if (isRecord(raw.tightest)) {
    const t = raw.tightest;
    // An empty name is the gateway saying "nothing has been used yet".
    const name = typeof t.name === 'string' && t.name ? t.name : typeof t.limit === 'string' ? t.limit : '';
    if (name) {
      const usedN = typeof t.used === 'number' ? t.used : num(used[name]);
      const cap = typeof t.cap === 'number' ? t.cap : num(limits[name]);
      const pct =
        typeof t.pct === 'number'
          ? t.pct
          : typeof t.used_pct === 'number'
            ? t.used_pct
            : cap > 0
              ? (usedN / cap) * 100
              : 0;
      out.tightest = { name, used: usedN, cap, pct };
    }
  }
  return out;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function num(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0;
}

/** A nested counter as a newer gateway already sends it; undefined unless it has a numeric cap. */
function readWindow(
  value: unknown,
): (QuotaWindow & { opened_at?: string; expires_in?: number; runs?: QuotaWindow; sessions?: QuotaWindow }) | undefined {
  if (!isRecord(value) || typeof value.cap !== 'number') return undefined;
  const out: QuotaWindow & { opened_at?: string; expires_in?: number; runs?: QuotaWindow; sessions?: QuotaWindow } = {
    used: num(value.used),
    cap: num(value.cap),
  };
  if (typeof value.resets_in === 'number') out.resets_in = value.resets_in;
  if (typeof value.expires_in === 'number') out.expires_in = value.expires_in;
  if (typeof value.opened_at === 'string') out.opened_at = value.opened_at;
  const runs = readWindow(value.runs);
  if (runs) out.runs = { used: runs.used, cap: runs.cap };
  const sessions = readWindow(value.sessions);
  if (sessions) out.sessions = { used: sessions.used, cap: sessions.cap };
  return out;
}

function secondsUntil(iso: unknown, now: number): number | undefined {
  if (typeof iso !== 'string') return undefined;
  const at = Date.parse(iso);
  return Number.isFinite(at) ? Math.max(0, Math.round((at - now) / 1000)) : undefined;
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
