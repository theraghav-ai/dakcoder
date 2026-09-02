/**
 * The run state machine: one derivation of every number on screen.
 *
 * Every surface — the chat header, the plan checklist, the gate ladder, the
 * status bar, the context inspector — reads *this* object. None of them derive
 * anything from the raw stream themselves. That rule exists because two
 * surfaces once computed the context denominator independently and disagreed on
 * screen by a few hundred tokens; a developer who sees two numbers for one
 * quantity stops trusting both. There is exactly one `usage` snapshot here, one
 * mutation set, one gate ladder, and the percentages come off the wire rather
 * than being recomputed from parts.
 *
 * **Resumption, not restart.** `attach` follows the SSE stream and reconnects
 * from `lastId` with `since_id`, first retry at 250 ms so the common blip is
 * repaired inside the 2 s budget. A run keeps executing server-side while the
 * link is down; without resumption the developer cannot tell a dropped socket
 * from a dead run, which is the whole "did it die?" class of confusion.
 *
 * **Additive-only (C2).** Every switch has a default arm that ignores the
 * event. An unknown type is a row we skip, never an error we show, and every
 * field is read defensively because `WireEvent.data` is `Record<string,
 * unknown>` — an older runtime may omit what a newer one sends.
 *
 * **Two de-duplications.** The planner emits its plan prose as an `assistant`
 * and then as a `plan`; a failing run emits an `error` and then a `finish`
 * carrying the same sentence. Both print twice unless one is suppressed, so a
 * row is held back for one event (or 400 ms, whichever comes first) and dropped
 * if its twin arrives. The delay is invisible: the streaming deltas are already
 * on screen, and the held row is only the persisted copy of what is showing.
 */

import * as vscode from 'vscode';

import { HttpError, type RuntimeClient } from './client';
import {
  isResumable,
  parsePlan,
  type ApprovalEvent,
  type GateEvent,
  type GateStage,
  type Mode,
  type Mutation,
  type SessionStatus,
  type SessionSummary,
  type WireEvent,
} from './protocol';

/** §14: memory budget. Older rows are evicted and handed to the chat to archive. */
const TRANSCRIPT_CAP = 500;

/** Backstop for a held row when its twin never arrives. Below perception. */
const HOLD_MS = 400;

/** §14 targets ≤2 s. The first retry lands well inside it; the tail is for a gateway that is genuinely down. */
const BACKOFF_MS = [250, 500, 1000, 2000, 4000, 8000, 15_000];

/** Reconnecting is normal; say "offline" only once it has stopped looking like a blip. */
const OFFLINE_AFTER_ATTEMPTS = 3;

/**
 * How many reconnect attempts before the run is reported as unreachable.
 *
 * There was no such limit: `isPermanent` only recognises an `HttpError`, and a
 * runtime that has exited answers with a `TypeError` from `fetch`, so the loop
 * retried a dead socket indefinitely. Twelve attempts across the backoff table
 * is a little over two minutes.
 */
const MAX_RECONNECT_ATTEMPTS = 12;

// ── the derived view types ──────────────────────────────────────────────────
//
// These are *derivations*, not wire shapes. Where a wire object is carried
// whole it is carried under its own type from ./protocol rather than restated
// with nicer field names — a renamed copy of a wire shape is a second contract
// nobody tests.

export type ParsedPlan = ReturnType<typeof parsePlan>;

export interface GateRun {
  /**
   * Neither is on the `gate` event: both come from the enclosing `turn_start`.
   * The ladder is stage × attempt, so the attempt has to be stitched on here —
   * which is precisely why it is stitched on in one place.
   */
  turn: number;
  attempt: number;
  at: number;
  event: GateEvent;
}

interface EntryBase {
  /** The wire event id. Stable across a reconnect replay, so rows are idempotent. */
  id: number;
  turn: number;
  /**
   * When *we* saw it. No event carries a server timestamp, so a transcript
   * replayed by `hydrate` is stamped with the moment it was read — which is why
   * nothing renders this as the time the tool actually ran.
   */
  at: number;
}

export type TranscriptEntry =
  | (EntryBase & { kind: 'user' | 'assistant' | 'error' | 'steer'; text: string })
  | (EntryBase & { kind: 'plan'; text: string; plan: ParsedPlan; steps: number })
  | (EntryBase & {
      kind: 'tool';
      /** The wire's `tool_call.id`. Named apart from `id`, which is the event id. */
      callId: string;
      name: string;
      arguments: unknown;
      /** Absent while the call is still in flight. */
      ok?: boolean;
      content: string;
      mutations: Mutation[];
      /** Server-measured, so it excludes the approval wait — the developer's own time. */
      ms?: number;
      truncated?: boolean;
      /** Answered from a ledger; no call was dispatched. See ToolResultEvent. */
      intercepted?: boolean;
      fix?: string;
    })
  | (EntryBase & { kind: 'gate'; gate: GateRun })
  | (EntryBase & { kind: 'finish'; outcome: string; summary: string; turns: number; paths: string[] });

export type ToolEntry = Extract<TranscriptEntry, { kind: 'tool' }>;

/** A path this run touched. */
export interface TouchedPath {
  path: string;
  /**
   * `null` when the wire named the path but not the operation — `finish` sends
   * paths only, so a session hydrated from its summary, or one we joined after
   * the `tool_result` scrolled past, honestly knows less. Rendered as "touched",
   * never guessed into "modified".
   */
  kind: Mutation['kind'] | null;
  protected: boolean;
}

/** The last `usage` event, verbatim. Nothing here is recomputed from anything else here. */
export interface ContextUsage {
  promptTokens: number;
  completionTokens: number;
  /** `null` = the endpoint did not report it. Never 0. See `cacheLabel`. */
  cachedTokens: number | null;
  budget: number;
  usedPct: number;
  reasoningTokens: number;
  estimateError?: number;
  reasoningLeaked?: number;
}

export interface GateLadder {
  /** Attempt numbers seen, ascending. Columns. */
  attempts: number[];
  /** One row per stage, in the order the stages first appeared. */
  rows: { name: string; cells: (GateStage | undefined)[] }[];
  /** Verbatim from the last gate. Stages that did not run are not stages that passed. */
  notRun: string[];
  blockedBy: string;
}

export type Connection =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'offline'
  | 'closed';

export interface RunStateOptions {
  client: RuntimeClient;
  log?: vscode.LogOutputChannel;
}

export class RunState implements vscode.Disposable {
  // ── emitters ──────────────────────────────────────────────────────────────
  private readonly changeEmitter = new vscode.EventEmitter<void>();
  private readonly appendEmitter = new vscode.EventEmitter<TranscriptEntry>();
  private readonly updateEmitter = new vscode.EventEmitter<TranscriptEntry>();
  private readonly evictEmitter = new vscode.EventEmitter<TranscriptEntry[]>();
  private readonly streamEmitter = new vscode.EventEmitter<string>();
  private readonly approvalEmitter = new vscode.EventEmitter<ApprovalEvent>();
  private readonly activityEmitter = new vscode.EventEmitter<boolean>();
  private readonly quotaEmitter = new vscode.EventEmitter<void>();
  private readonly receiveEmitter = new vscode.EventEmitter<WireEvent>();

  /** Coalesced. Read the getters; do not expect a payload. */
  readonly onDidChange = this.changeEmitter.event;
  readonly onDidAppend = this.appendEmitter.event;
  /** An existing row changed in place — a tool call that completed. */
  readonly onDidUpdate = this.updateEmitter.event;
  /** Rows pushed past the 500-entry cap, for the chat to archive to `workspaceState`. */
  readonly onDidEvict = this.evictEmitter.event;
  /** The live delta buffer. Never persisted (S11); the `assistant` row supersedes it. */
  readonly onDidStream = this.streamEmitter.event;
  readonly onDidRequestApproval = this.approvalEmitter.event;
  /** `true` on task start, `false` on task end. The quota poll's only trigger. */
  readonly onDidChangeActivity = this.activityEmitter.event;
  /** The server said quota moved. A push, so nothing has to poll while idle. */
  readonly onDidSignalQuota = this.quotaEmitter.event;
  /**
   * The raw wire event, re-emitted.
   *
   * The panel renders from events; the trees and the status bar read the derived
   * getters. Both are served from here rather than from two independent readers
   * of the same stream, because two readers means two `since_id` cursors and one
   * of them silently falls behind on a reconnect.
   */
  readonly onDidReceive = this.receiveEmitter.event;

  // ── the picture ───────────────────────────────────────────────────────────
  private _sessionId?: string;
  private _task = '';
  private _mode: Mode | string = '';
  private _turn = 0;
  private _attempt = 1;
  private _status: SessionStatus | string = 'running';
  private _summary = '';
  private _startedAt?: number;
  private _finishedAt?: number;
  private _plan?: ParsedPlan;
  private _planSteps = 0;
  private _usage?: ContextUsage;
  private _lastError?: string;
  private _connection: Connection = 'idle';
  private _attached = false;
  /** True only while `hydrate` is replaying a stored transcript. */
  private replaying = false;
  private _active = false;

  private readonly entries: TranscriptEntry[] = [];
  private readonly toolRows = new Map<string, ToolEntry>();
  private readonly inFlight = new Map<string, string>();
  private readonly approvals = new Map<string, ApprovalEvent>();
  private readonly touched = new Map<string, TouchedPath>();
  private readonly gates: GateRun[] = [];
  private readonly compactionRuns: GateRun[] = [];
  private ladder?: GateLadder;

  private streaming = '';
  private lastId = 0;
  private held?: TranscriptEntry;
  private holdTimer?: ReturnType<typeof setTimeout>;
  private changeTimer?: ReturnType<typeof setTimeout>;
  private follower?: AbortController;

  constructor(private readonly opts: RunStateOptions) {}

  // ── reading the picture ───────────────────────────────────────────────────

  get sessionId(): string | undefined {
    return this._sessionId;
  }

  get task(): string {
    return this._task;
  }

  /** The raw wire value. A mode added after this build still shows, per C2. */
  get modeId(): string {
    return this._mode;
  }

  /**
   * The progressive form the status bar and the chat header both show, so one
   * run is never "coding" in one place and "coder" in another.
   */
  get modeLabel(): string {
    switch (this._mode) {
      case 'ask':
        return vscode.l10n.t('reading');
      case 'planner':
        return vscode.l10n.t('planning');
      case 'agent':
        return vscode.l10n.t('working');
      // The retired five. Kept so a stored session recorded under them still
      // reads as English rather than as a raw wire value.
      case 'scaffolder':
      case 'coder':
      case 'debugger':
        return vscode.l10n.t('working');
      case 'verifier':
        return vscode.l10n.t('verifying');
      default:
        // C2: an unknown mode is displayed, not swallowed. Better a word the
        // developer can search for than a state the UI pretends is idle.
        return this._mode || vscode.l10n.t('working');
    }
  }

  get turn(): number {
    return this._turn;
  }

  /** 1 or 2. Above 1 this is the retry after a failed gate — what a developer watches for. */
  get attempt(): number {
    return this._attempt;
  }

  /**
   * The known values are `SessionStatus`; typed as a string because a newer
   * runtime may report an outcome this build has never heard of, and C2 says
   * show it rather than reject it.
   */
  get status(): string {
    return this._status;
  }

  get summary(): string {
    return this._summary;
  }

  get resumable(): boolean {
    return isResumable(this._status);
  }

  /** Attached to a session the server is still working on. */
  get running(): boolean {
    return this._attached && this._status === 'running';
  }

  get connection(): Connection {
    return this._connection;
  }

  get lastError(): string | undefined {
    return this._lastError;
  }

  /**
   * Wall clock from our side. `tool_result.ms` is server-measured and excludes
   * the approval wait, so summing it would under-report exactly the time the
   * developer spent staring at a dialog.
   */
  get elapsedMs(): number {
    if (this._startedAt === undefined) return 0;
    return (this._finishedAt ?? Date.now()) - this._startedAt;
  }

  /** The name of a tool call still in flight, if any. */
  get currentTool(): string | undefined {
    let last: string | undefined;
    for (const name of this.inFlight.values()) last = name;
    return last;
  }

  get plan(): ParsedPlan | undefined {
    return this._plan;
  }

  /**
   * The server's own count. It is emitted alongside the text so the two sides
   * agree; the checklist renders `plan.steps`, whose parse uses the server's
   * regex. Per-step status is not on the wire and is not inferred — see
   * `PlanStep.status`.
   */
  get planSteps(): number {
    return this._planSteps;
  }

  get usage(): ContextUsage | undefined {
    return this._usage;
  }

  get transcript(): readonly TranscriptEntry[] {
    return this.entries;
  }

  /** Live delta text for the bubble in flight. Empty between turns. */
  get streamingText(): string {
    return this.streaming;
  }

  get pendingApprovals(): ApprovalEvent[] {
    return [...this.approvals.values()];
  }

  get pendingApproval(): ApprovalEvent | undefined {
    for (const approval of this.approvals.values()) return approval;
    return undefined;
  }

  get mutations(): TouchedPath[] {
    return [...this.touched.values()].sort((a, b) => a.path.localeCompare(b.path));
  }

  get compactions(): readonly GateRun[] {
    return this.compactionRuns;
  }

  /** Stage × attempt. Memoised because a tree view asks once per visible row. */
  get gateLadder(): GateLadder {
    if (this.ladder) return this.ladder;
    const attempts: number[] = [];
    const order: string[] = [];
    const cells = new Map<string, Map<number, GateStage>>();

    for (const run of this.gates) {
      if (!attempts.includes(run.attempt)) attempts.push(run.attempt);
      for (const stage of run.event.stages) {
        if (!cells.has(stage.name)) {
          cells.set(stage.name, new Map());
          order.push(stage.name);
        }
        cells.get(stage.name)!.set(run.attempt, stage);
      }
    }
    attempts.sort((a, b) => a - b);

    const last = this.gates[this.gates.length - 1];
    this.ladder = {
      attempts,
      rows: order.map((name) => ({
        name,
        cells: attempts.map((attempt) => cells.get(name)?.get(attempt)),
      })),
      notRun: last ? last.event.not_run : [],
      blockedBy: last ? last.event.blocked_by : '',
    };
    return this.ladder;
  }

  // ── the numbers, formatted once ───────────────────────────────────────────

  /**
   * `18.2k / 32k context · 1.4k reasoning · cache 84%`.
   *
   * The percentage is the server's `budget_used_pct`, never `prompt / budget`
   * recomputed here — the wire carries both so that no two surfaces can round
   * their way to different answers.
   */
  contextMeter(): string | undefined {
    const usage = this._usage;
    if (!usage || usage.budget <= 0) return undefined;

    const segments = [
      vscode.l10n.t(
        '{0} / {1} context ({2}%)',
        compact(usage.promptTokens),
        compact(usage.budget),
        Math.round(usage.usedPct),
      ),
    ];
    // Reasoning is only meaningful where thinking is on (Planner, Debugger);
    // a `0` on every coder turn would be noise that teaches nothing.
    if (usage.reasoningTokens > 0) {
      segments.push(vscode.l10n.t('{0} reasoning', compact(usage.reasoningTokens)));
    }
    // §7.6: the compact header omits the cache segment when it is unreported,
    // rather than printing a figure. The full breakdown says "not reported" in
    // words — see `cacheLabel` — because in a tooltip the absence is worth a
    // sentence and in a one-line meter it is worth nothing.
    if (usage.cachedTokens !== null && usage.budget > 0) {
      segments.push(vscode.l10n.t('cache {0}%', Math.round((usage.cachedTokens / usage.budget) * 100)));
    }
    return segments.join(' · ');
  }

  /**
   * `84%` or "not reported". Never `0%`: the endpoint may simply not send
   * `prompt_tokens_details`, and a zero there reads as a cache that failed
   * rather than a number nobody has.
   */
  cacheLabel(): string {
    const usage = this._usage;
    if (!usage || usage.cachedTokens === null || usage.budget <= 0) {
      return vscode.l10n.t('not reported');
    }
    return vscode.l10n.t('{0}%', Math.round((usage.cachedTokens / usage.budget) * 100));
  }

  /** Count-bearing, so it needs both forms: `vscode.l10n` has no ICU plurals. */
  mutationSummary(): string {
    const n = this.touched.size;
    return n === 1 ? vscode.l10n.t('1 file changed') : vscode.l10n.t('{0} files changed', n);
  }

  // ── lifecycle ─────────────────────────────────────────────────────────────

  /**
   * Seed from a REST summary before (or instead of) following the stream, so
   * opening a past session is not an empty panel that fills in a second later.
   * The transcript is replayed through the same `ingest`, which is what keeps
   * the de-duplication rules from applying only to live runs.
   */
  hydrate(summary: SessionSummary): void {
    if (summary.id !== this._sessionId) this.reset();
    this._sessionId = summary.id;
    this._task = summary.task;
    this._status = summary.status;
    this._summary = summary.summary;
    this._startedAt ??= Date.parse(summary.created_at) || Date.now();
    // Cleared, not merely overwritten. A follow-up puts a finished session back
    // to running and sends `finished_at: null`; keeping the old value would
    // leave the elapsed timer frozen at whatever the last run took, which reads
    // as a run that has stalled.
    this._finishedAt = summary.finished_at
      ? Date.parse(summary.finished_at) || Date.now()
      : undefined;

    /*
     * A stored transcript is history, and the difference matters for exactly
     * one row: `tool_pending`.
     *
     * Opening a finished session replayed every approval it had ever raised as
     * a live card with Accept and Reject on it, and five seconds later the
     * approval poller noticed the runtime was not holding them and toasted
     * "released by the runtime... recorded as a rejection" for each one.
     * Buttons that did nothing, followed by a warning about a decision nobody
     * made.
     *
     * Replay is the right discriminator rather than "is the run finished",
     * because a `tool_pending` in a *transcript* is history whatever the
     * session's status: an approval that is genuinely still open comes back
     * separately below, in `summary.pending_approvals`, which is the server's
     * live set and the only authority on the question.
     */
    this.replaying = true;
    try {
      for (const event of summary.transcript ?? []) this.ingest(event);
    } finally {
      this.replaying = false;
    }
    for (const path of summary.mutations) this.notePath(path);
    for (const approval of summary.pending_approvals ?? []) {
      this.approvals.set(approval.id, approval);
      // Raised again, not merely remembered.
      //
      // An approval that was already waiting when this window opened the
      // session has no `tool_pending` frame left to replay -- the transcript
      // above has been ingested and the approval outlived it. Recording it here
      // and stopping would put it in this map and nowhere else, so the service
      // that owns the answer would never learn of it and the card would sit
      // unanswerable until the runtime timed it out. `present` is keyed by id
      // and returns early on one it already holds, so re-raising a live
      // approval is idempotent.
      this.approvalEmitter.fire(approval);
    }
    this.settleActivity();
    this.scheduleChange();
  }

  /**
   * Follow a session, reconnecting on its own until `detach`.
   *
   * Re-attaching to the session already loaded resumes from `lastId` rather
   * than replaying it, which is what makes hydrate-then-attach cheap.
   */
  attach(sessionId: string): void {
    if (sessionId !== this._sessionId) this.reset();
    this._sessionId = sessionId;
    this._attached = true;
    this._startedAt ??= Date.now();

    this.follower?.abort();
    const controller = new AbortController();
    this.follower = controller;
    void this.follow(sessionId, controller.signal);
    this.settleActivity();
    this.scheduleChange();
  }

  detach(): void {
    this.follower?.abort();
    this.follower = undefined;
    this._attached = false;
    this.release();
    this.setConnection('idle');
    this.settleActivity();
    this.scheduleChange();
  }

  /**
   * The wire carries no "approval resolved" event, so the surface that answered
   * one has to say so. Without this an answered card sits on screen looking
   * like it is still blocking the run.
   */
  resolveApproval(id: string): void {
    if (this.approvals.delete(id)) this.scheduleChange();
  }

  reset(): void {
    this.follower?.abort();
    this.follower = undefined;
    this.clearHold();
    this.entries.length = 0;
    this.toolRows.clear();
    this.inFlight.clear();
    this.approvals.clear();
    this.touched.clear();
    this.gates.length = 0;
    this.compactionRuns.length = 0;
    this.ladder = undefined;
    this.streaming = '';
    this.lastId = 0;
    this._sessionId = undefined;
    this._task = '';
    this._mode = '';
    this._turn = 0;
    this._attempt = 1;
    this._status = 'running';
    this._summary = '';
    this._startedAt = undefined;
    this._finishedAt = undefined;
    this._plan = undefined;
    this._planSteps = 0;
    this._usage = undefined;
    this._lastError = undefined;
    this._attached = false;
    this.setConnection('idle');
    this.settleActivity();
    this.scheduleChange();
  }

  dispose(): void {
    this.follower?.abort();
    this.clearHold();
    if (this.changeTimer) clearTimeout(this.changeTimer);
    this.receiveEmitter.dispose();
    this.changeEmitter.dispose();
    this.appendEmitter.dispose();
    this.updateEmitter.dispose();
    this.evictEmitter.dispose();
    this.streamEmitter.dispose();
    this.approvalEmitter.dispose();
    this.activityEmitter.dispose();
    this.quotaEmitter.dispose();
  }

  // ── the stream ────────────────────────────────────────────────────────────

  private async follow(sessionId: string, signal: AbortSignal): Promise<void> {
    let failures = 0;

    while (!signal.aborted) {
      try {
        if (failures === 0) this.setConnection('connecting');
        for await (const event of this.opts.client.events(sessionId, this.lastId, signal)) {
          // A delivered event is the only proof the link works; a successful
          // connect that immediately dies would otherwise reset the backoff and
          // spin.
          failures = 0;
          this.setConnection('live');
          this.ingest(event);
          if (event.type === 'end') {
            this.release();
            this.setConnection('closed');
            this.settleActivity();
            return;
          }
        }
      } catch (err) {
        if (signal.aborted) return;
        if (this.isPermanent(err)) {
          this._lastError = err instanceof Error ? err.message : String(err);
          this.setConnection('closed');
          this.settleActivity();
          this.scheduleChange();
          return;
        }
        this.opts.log?.warn(`event stream dropped: ${String(err)}`);
      }

      if (signal.aborted) return;
      // A dropped link must not strand a held row behind a twin that will never
      // arrive on this connection.
      this.release();
      // A stream that closes cleanly on a finished session is not a failure to
      // retry; reconnecting forever against a terminal session is a busy loop
      // with a spinner on it.
      if (!this.running) {
        this.setConnection('closed');
        this.settleActivity();
        return;
      }

      const wait = this.retryDelay(failures);
      failures += 1;
      // Give up eventually.
      //
      // Only an `HttpError` counted as permanent, and a dead daemon does not
      // produce one - `fetch failed` is a `TypeError`. So a runtime that had
      // exited was reconnected to forever, at fifteen-second intervals, behind
      // a "Working..." spinner nothing could ever resolve. The developer's only
      // signal was that nothing happened, which is the same signal every other
      // failure gave them.
      //
      // The backoff table tops out at 15s, so this is roughly two minutes of
      // trying before the run is reported as unreachable. Long enough for a VPN
      // blink or a runtime restart; short enough that a real death is visible
      // while the developer is still looking at it.
      if (failures >= MAX_RECONNECT_ATTEMPTS) {
        this._lastError = vscode.l10n.t(
          'Lost contact with the runtime after {0} attempts. It may have exited; try again to restart it.',
          failures,
        );
        this._status = 'error';
        this._summary = this._lastError;
        this._finishedAt = Date.now();
        this._attached = false;
        this.approvals.clear();
        this.inFlight.clear();
        this.setConnection('closed');
        this.settleActivity();
        this.scheduleChange();
        return;
      }
      this.setConnection(failures >= OFFLINE_AFTER_ATTEMPTS ? 'offline' : 'reconnecting');
      await sleep(wait, signal);
    }
  }

  private retryDelay(index: number): number {
    const base = BACKOFF_MS[Math.min(index, BACKOFF_MS.length - 1)];
    // Jitter, because every window in the office reconnects at once when the
    // VPN blinks and a synchronised retry is a self-inflicted thundering herd.
    return base + Math.floor(Math.random() * (base / 2));
  }

  /** 401/403/404/410 will not fix themselves by being asked again. */
  private isPermanent(err: unknown): boolean {
    if (!(err instanceof HttpError)) return false;
    if (err.isQuota) return false;
    return err.status === 401 || err.status === 403 || err.status === 404 || err.isGone;
  }

  // ── ingestion ─────────────────────────────────────────────────────────────

  private ingest(event: WireEvent): void {
    /*
     * A stored `tool_pending` from a run that has ended is history, not a
     * question, and is marked as such before anything renders it.
     *
     * Opening a finished session replays its transcript through this same path,
     * so every approval it ever raised came back to the panel as a live card
     * with Accept and Reject on it - and five seconds later the approval poller
     * noticed the runtime was not holding them and toasted "released by the
     * runtime... recorded as a rejection" for each one. Buttons that do
     * nothing, followed by a warning about a decision nobody made.
     *
     * `replaying` is the whole test: an approval that is genuinely still open
     * arrives through `summary.pending_approvals`, which is the server's live
     * set, not through a row in the transcript.
     */
    if (event.type === 'tool_pending' && this.replaying) {
      event = { ...event, data: { ...(event.data ?? {}), historical: true } };
    }

    // Re-emitted first, so a consumer that renders raw events sees them in
    // exactly the order the server sent them, before any derived state moves.
    this.receiveEmitter.fire(event);

    const d = event.data ?? {};
    const at = Date.now();

    // Transient events are handled above the cursor, because they do not have
    // one. The server does not persist `assistant_delta` or `heartbeat`, so it
    // does not spend an id on them either — it stamps them with the id the
    // *next* stored event will get. Running them through the monotonic guard
    // therefore drops every delta after the first and then drops the authoritative
    // `assistant` message that shares their id, and advancing `lastId` from one
    // makes the next reconnect resume past an answer that was never delivered.
    if (event.type === 'assistant_delta') {
      this.setStreaming(this.streaming + str(d.text));
      return; // never a row: S11 says do not build a transcript from deltas
    }
    if (event.type === 'heartbeat') {
      // Liveness only. It proves the link, which `follow` already recorded.
      return;
    }

    // A server that resumed inclusively rather than exclusively would double
    // every row at each reconnect. Ids are monotonic; trust nothing else.
    if (this.lastId && event.id <= this.lastId) return;
    if (event.id) this.lastId = event.id;

    switch (event.type) {
      case 'user': {
        // The developer's own message, recorded by the runtime rather than only
        // echoed by whichever panel was open when they typed it. Without it a
        // re-opened conversation is the agent talking to itself.
        this.release();
        this.append({ id: event.id, turn: this._turn, at, kind: 'user', text: str(d.text) });
        break;
      }

      case 'turn_start': {
        this.release();
        this._turn = num(d.turn, this._turn + 1);
        this._mode = str(d.mode, this._mode);
        this._attempt = num(d.attempt, 1);
        // Deltas are not persisted, so a new turn starts from nothing rather
        // than from whatever the last one left in the buffer.
        this.setStreaming('');
        this._startedAt ??= at;
        break;
      }

      case 'assistant': {
        this.release();
        const text = str(d.text);
        this.hold({ id: event.id, turn: this._turn, at, kind: 'assistant', text });
        break;
      }

      case 'plan': {
        const text = str(d.text);
        // Rule one: the plan prose already arrived as an `assistant`.
        if (this.held?.kind === 'assistant' && this.held.turn === this._turn && same(this.held.text, text)) {
          this.clearHold();
        } else {
          this.release();
        }
        this.setStreaming('');
        this._plan = parsePlan(text);
        this._planSteps = num(d.steps, this._plan.steps.length);
        this.append({ id: event.id, turn: this._turn, at, kind: 'plan', text, plan: this._plan, steps: this._planSteps });
        break;
      }

      case 'tool_call': {
        this.release();
        const callId = str(d.id);
        const name = str(d.name);
        const entry: ToolEntry = {
          id: event.id,
          turn: this._turn,
          at,
          kind: 'tool',
          callId,
          name,
          arguments: d.arguments,
          content: '',
          mutations: [],
        };
        this.toolRows.set(callId, entry);
        this.inFlight.set(callId, name);
        this.append(entry);
        break;
      }

      case 'tool_pending': {
        this.release();
        const approval = readApproval(d);
        // Replayed from a stored transcript: recorded, never presented. See
        // the note in `hydrate`.
        if (this.replaying) break;
        this.approvals.set(approval.id, approval);
        this.approvalEmitter.fire(approval);
        break;
      }

      case 'tool_result': {
        this.release();
        const callId = str(d.id);
        const mutations = readMutations(d.mutations);
        for (const mutation of mutations) this.noteMutation(mutation);
        this.inFlight.delete(callId);
        // Best effort: the approval id is minted with the request and is not
        // documented as equal to the call id, so `resolveApproval` remains the
        // authoritative way a decided card is cleared.
        this.approvals.delete(callId);

        const existing = this.toolRows.get(callId);
        const patch = {
          ok: bool(d.ok),
          content: str(d.content),
          mutations,
          ...(typeof d.ms === 'number' ? { ms: d.ms } : {}),
          ...(d.truncated === true ? { truncated: true } : {}),
          ...(d.intercepted === true ? { intercepted: true } : {}),
          ...(typeof d.fix === 'string' ? { fix: d.fix } : {}),
        };
        if (existing) {
          Object.assign(existing, patch);
          this.updateEmitter.fire(existing);
        } else {
          // The call scrolled past the 500-row cap, or we joined mid-run. A
          // result with no call is still the more informative half.
          this.append({
            id: event.id,
            turn: this._turn,
            at,
            kind: 'tool',
            callId,
            name: str(d.name),
            arguments: undefined,
            ...patch,
          });
        }
        break;
      }

      case 'gate': {
        this.release();
        const run: GateRun = { turn: this._turn, attempt: this._attempt, at, event: readGate(d) };
        if (run.event.kind === 'compaction') {
          this.compactionRuns.push(run);
        } else {
          this.gates.push(run);
          this.ladder = undefined;
        }
        this.append({ id: event.id, turn: this._turn, at, kind: 'gate', gate: run });
        break;
      }

      case 'usage': {
        // Header furniture, not a row — and deliberately not a `release`, since
        // a usage event between an assistant and its plan must not break the
        // pairing the de-duplication depends on.
        this._usage = {
          promptTokens: num(d.prompt_tokens),
          completionTokens: num(d.completion_tokens),
          cachedTokens: numOrNull(d.cached_tokens),
          budget: num(d.budget),
          usedPct: num(d.budget_used_pct),
          reasoningTokens: num(d.reasoning_tokens),
          ...(typeof d.estimate_error === 'number' ? { estimateError: d.estimate_error } : {}),
          ...(typeof d.reasoning_leaked === 'number' ? { reasoningLeaked: d.reasoning_leaked } : {}),
        };
        break;
      }

      case 'quota': {
        // protocol.ts declares `QuotaSnapshot` for `GET /v1/quota` and no shape
        // for this event, so it is treated as a signal and not as data: the
        // status bar re-reads the endpoint whose shape is under contract.
        this.quotaEmitter.fire();
        return;
      }

      case 'steer': {
        this.release();
        // No `SteerEvent` in protocol.ts, so the text is read the same
        // defensive way as any field that may not be there.
        this.append({ id: event.id, turn: this._turn, at, kind: 'steer', text: str(d.text) });
        break;
      }

      case 'error': {
        this.release();
        const message = str(d.message) || str(d.error) || str(d.detail) || vscode.l10n.t('The run reported an error with no message.');
        this._lastError = message;
        // Rule two: a failing run usually repeats this sentence as the finish
        // summary. Hold it and drop it if that is what arrives next.
        this.hold({ id: event.id, turn: this._turn, at, kind: 'error', text: message });
        break;
      }

      case 'finish': {
        const summary = str(d.summary);
        if (this.held?.kind === 'error' && same(this.held.text, summary)) {
          this.clearHold();
        } else {
          this.release();
        }
        this._status = str(d.outcome, 'error');
        this._summary = summary;
        this._finishedAt = at;
        this.setStreaming('');
        const paths = strings(d.mutations);
        for (const path of paths) this.notePath(path);
        // Nothing can still be waiting on a run that has finished.
        this.approvals.clear();
        this.inFlight.clear();
        this.append({
          id: event.id,
          turn: this._turn,
          at,
          kind: 'finish',
          outcome: this._status,
          summary,
          turns: num(d.turns, this._turn),
          paths,
        });
        this.settleActivity();
        break;
      }

      case 'end': {
        /*
         * `end` is terminal, whether or not `finish` came first.
         *
         * The panel's `running` flag cleared only on `finish`, and a crashed run
         * never sends one: the runtime emits `error` then `end` and the thread
         * dies. So any backend exception - a gateway 401 on an expired token, a
         * timeout inside the loop - froze the panel on "Working..." forever, and
         * the developer's next message was then treated as a mid-run correction,
         * queued against a session that had stopped, and never seen again. From
         * the panel, every backend failure looked like "your message, then
         * nothing".
         *
         * `end.outcome` carries the answer and was ignored outright. It is read
         * here, and a run that reached `finish` first keeps the status it set:
         * `finish` is the richer event and must not be overwritten by the
         * envelope that follows it.
         */
        if (this._status === 'running') {
          this._status = str(d.outcome, 'error');
          this._summary = str(d.summary) || this._summary;
          this._finishedAt = at;
          this.setStreaming('');
          for (const path of strings(d.mutations)) this.notePath(path);
        }
        // Nothing can still be waiting on a run that has ended, however it
        // ended. Without this a crashed run leaves approval cards on screen
        // that nothing will ever answer.
        this.approvals.clear();
        this.inFlight.clear();
        this.release();
        this._attached = false;
        this.settleActivity();
        this.scheduleChange();
        return;
      }

      default:
        // C2: an unknown type is a row we skip, not an error we show.
        return;
    }

    this.scheduleChange();
  }

  // ── rows ──────────────────────────────────────────────────────────────────

  private append(entry: TranscriptEntry): void {
    this.entries.push(entry);
    if (this.entries.length > TRANSCRIPT_CAP) {
      const evicted = this.entries.splice(0, this.entries.length - TRANSCRIPT_CAP);
      // Drop the lookup too, or a long run leaks one map entry per tool call.
      for (const old of evicted) if (old.kind === 'tool') this.toolRows.delete(old.callId);
      this.evictEmitter.fire(evicted);
    }
    this.appendEmitter.fire(entry);
  }

  /** Park a row for one event or `HOLD_MS`, whichever comes first. */
  private hold(entry: TranscriptEntry): void {
    this.release();
    this.held = entry;
    this.holdTimer = setTimeout(() => {
      this.holdTimer = undefined;
      this.release();
      this.scheduleChange();
    }, HOLD_MS);
  }

  /** Emit the held row: its twin did not arrive. */
  private release(): void {
    const entry = this.held;
    this.clearHold();
    if (!entry) return;
    if (entry.kind === 'assistant') this.setStreaming('');
    this.append(entry);
  }

  private clearHold(): void {
    if (this.holdTimer) clearTimeout(this.holdTimer);
    this.holdTimer = undefined;
    this.held = undefined;
  }

  private setStreaming(text: string): void {
    if (this.streaming === text) return;
    this.streaming = text;
    this.streamEmitter.fire(text);
  }

  // ── mutations ─────────────────────────────────────────────────────────────

  private noteMutation(mutation: Mutation): void {
    const existing = this.touched.get(mutation.path);
    if (!existing) {
      this.touched.set(mutation.path, { ...mutation });
      return;
    }
    // A file this run created and then edited is still a file this run created:
    // reverting it means deleting it, not restoring a baseline that never
    // existed. A later delete wins outright.
    if (mutation.kind === 'delete' || existing.kind === null) existing.kind = mutation.kind;
    else if (existing.kind !== 'create') existing.kind = mutation.kind;
    // `protected` is computed server-side against PROTECTED_GLOBS and never
    // recomputed here; once true it stays true.
    existing.protected ||= mutation.protected;
  }

  /** A path we know was touched but not how — `finish` and the REST summary carry paths only. */
  private notePath(path: string): void {
    if (!this.touched.has(path)) this.touched.set(path, { path, kind: null, protected: false });
  }

  // ── notification plumbing ─────────────────────────────────────────────────

  private setConnection(next: Connection): void {
    if (this._connection === next) return;
    this._connection = next;
    this.scheduleChange();
  }

  /** Fire only on the edges, because this is what starts and stops the quota poll. */
  private settleActivity(): void {
    const active = this.running;
    if (active === this._active) return;
    this._active = active;
    this.activityEmitter.fire(active);
  }

  private scheduleChange(): void {
    if (this.changeTimer) return;
    // One notification per frame or so. Every surface re-reads from getters, so
    // firing per delta would be twenty-five redraws a second for no new numbers.
    this.changeTimer = setTimeout(() => {
      this.changeTimer = undefined;
      this.changeEmitter.fire();
    }, 16);
  }
}

// ── defensive readers ───────────────────────────────────────────────────────
//
// `WireEvent.data` is `Record<string, unknown>`: the declared interfaces in
// ./protocol are a lower bound on what arrives, not a guarantee. Nothing below
// throws — a field that is missing renders as its absence.

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function num(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

/** Distinguishes "the endpoint reported nothing" from zero. See `cacheLabel`. */
function numOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function bool(value: unknown): boolean {
  return value === true;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((v): v is Record<string, unknown> => !!v && typeof v === 'object')
    : [];
}

function readMutations(value: unknown): Mutation[] {
  return records(value).map((m) => ({
    path: str(m.path),
    kind: m.kind === 'create' || m.kind === 'delete' ? m.kind : 'modify',
    protected: bool(m.protected),
  }));
}

/**
 * A `gate` event's data as a typed `GateEvent`.
 *
 * Exported because activation needs the same reading to decide whether to offer
 * a local re-run, and two parsers of one wire shape is one parser too many.
 */
export function readGateEvent(d: Record<string, unknown>): GateEvent {
  return readGate(d);
}

function readGate(d: Record<string, unknown>): GateEvent {
  const stages: GateStage[] = records(d.stages).map((s) => ({
    name: str(s.name),
    ok: bool(s.ok),
    blocking: bool(s.blocking),
    skipped: str(s.skipped),
    seconds: num(s.seconds),
    ...(typeof s.content === 'string' ? { content: s.content } : {}),
    ...(s.truncated === true ? { truncated: true } : {}),
  }));
  return {
    // Only `compaction` is special-cased; any other kind — including one added
    // after this build — is carried through and shown as a verification gate.
    kind: str(d.kind, 'full') as GateEvent['kind'],
    ok: bool(d.ok),
    seconds: num(d.seconds),
    stages,
    not_run: strings(d.not_run),
    blocked_by: str(d.blocked_by),
    ...(typeof d.before === 'number' ? { before: d.before } : {}),
    ...(typeof d.after === 'number' ? { after: d.after } : {}),
  };
}

function readApproval(d: Record<string, unknown>): ApprovalEvent {
  const args = d.arguments;
  return {
    id: str(d.id),
    tool: str(d.tool),
    arguments: args && typeof args === 'object' ? (args as Record<string, unknown>) : {},
    reason: str(d.reason),
    paths: strings(d.paths),
    protected: strings(d.protected),
    unconditional: bool(d.unconditional),
  };
}

// ── small shared helpers ────────────────────────────────────────────────────

/** Trailing whitespace differs between the two copies often enough to matter. */
function same(a: string, b: string): boolean {
  return a.trim() === b.trim() && a.trim().length > 0;
}

function compact(tokens: number): string {
  if (tokens < 1000) return String(tokens);
  const thousands = tokens / 1000;
  return `${thousands >= 100 ? Math.round(thousands) : Number(thousands.toFixed(1))}k`;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}
