/**
 * The two status bar items: what the agent is doing, and what it is costing.
 *
 * Both read `RunState` and nothing else, so the bar can never disagree with the
 * panel about the turn, the mode or the context percentage. Neither item
 * computes a number that `RunState` or the quota endpoint already carries.
 *
 * **Idle CPU is a budget (§14, ~0%).** There is no timer while nothing is
 * running. The elapsed ticker and the 60 s quota poll both start on
 * `onDidChangeActivity(true)` and are cleared on `false`; a `quota` event on the
 * stream refreshes as a push. Nothing is fetched at activation either — §3.3
 * forbids touching the network there, so the quota item stays hidden until
 * something real has been fetched. A quota figure nobody has asked the gateway
 * for is either stale or invented, and neither belongs in the status bar.
 *
 * **Colour never carries meaning alone (§16).** Every state has a word, and the
 * two quota thresholds swap the leading glyph as well as the foreground, so the
 * warning survives a high-contrast theme and a monochrome screenshot.
 */

import * as vscode from 'vscode';

import { HttpError } from './client';
import { isResumable, type QuotaSnapshot, type QuotaWindow } from './protocol';
import type { RunState } from './session-state';

/** §7.1. Below the first, no colour. */
const WARNING_PCT = 80;
const CRITICAL_PCT = 95;

/** §7.2: while a task runs, and only then. */
const POLL_MS = 60_000;

/** A chatty server must not turn a push into a poll storm. */
const REFRESH_FLOOR_MS = 30_000;

export type Severity = 'normal' | 'warning' | 'critical';

export interface StatusBarCommands {
  /**
   * Defaults are VS Code's generated `<viewId>.focus` commands for the two
   * contributed views, so the click targets need no manifest entry of their own.
   */
  panel?: string;
  quota?: string;
  stop?: string;
}

export interface StatusBarOptions {
  /** Injected rather than taking a `GatewayClient`, so the bar survives sign-out and re-auth. */
  quota: () => Promise<QuotaSnapshot>;
  commands?: StatusBarCommands;
  log?: vscode.LogOutputChannel;
}

export class StatusBar implements vscode.Disposable {
  private readonly agent: vscode.StatusBarItem;
  private readonly quota: vscode.StatusBarItem;
  private readonly subscriptions: vscode.Disposable[] = [];
  private readonly commands: Required<StatusBarCommands>;

  private snapshot?: QuotaSnapshot;
  private fetchedAt = 0;
  private stale = false;
  private inFlight?: Promise<void>;
  private tick?: ReturnType<typeof setInterval>;
  private poll?: ReturnType<typeof setInterval>;
  private offlineNote?: string;
  private lastAgentText = '';

  constructor(
    private readonly state: RunState,
    private readonly opts: StatusBarOptions,
  ) {
    this.commands = {
      panel: opts.commands?.panel ?? 'dakcoder.chat.focus',
      quota: opts.commands?.quota ?? 'dakcoder.quota.focus',
      stop: opts.commands?.stop ?? 'dakcoder.stopTask',
    };

    this.agent = vscode.window.createStatusBarItem('dakcoder.agent', vscode.StatusBarAlignment.Left, 100);
    // Without `name` the item cannot be hidden from the status bar's context
    // menu, and shows there unlabelled.
    this.agent.name = vscode.l10n.t('dakcoder agent');
    this.agent.command = this.commands.panel;

    this.quota = vscode.window.createStatusBarItem('dakcoder.quota', vscode.StatusBarAlignment.Right, 100);
    this.quota.name = vscode.l10n.t('dakcoder quota');
    this.quota.command = this.commands.quota;

    this.subscriptions.push(
      this.state.onDidChange(() => this.renderAgent()),
      this.state.onDidChangeActivity((active) => this.onActivity(active)),
      this.state.onDidSignalQuota(() => void this.refresh()),
    );

    this.renderAgent();
    this.agent.show();
  }

  // ── the agent item ────────────────────────────────────────────────────────

  private onActivity(active: boolean): void {
    this.stopTimers();
    // Task start and task end are both refresh points: the window that opened
    // and the tokens the run just spent.
    void this.refresh(true);
    if (!active) {
      this.renderAgent();
      return;
    }
    this.tick = setInterval(() => this.renderAgent(true), 1000);
    this.poll = setInterval(() => void this.refresh(true), POLL_MS);
    this.renderAgent();
  }

  private renderAgent(force = false): void {
    const face = this.agentFace();
    const detail = this.agentDetail();
    const text = `${face.icon} ${vscode.l10n.t('dakcoder')}: ${face.word}${detail ? ` · ${detail}` : ''}`;

    // `onDidChange` fires up to sixty times a second during a run and the text
    // changes about once a turn. Every property here crosses the extension-host
    // RPC boundary, and rebuilding the tooltip's MarkdownString for an unchanged
    // line is the kind of waste that shows up as a laggy status bar in someone
    // else's extension. The one-second ticker forces a rebuild for the elapsed
    // clock; a run that is not running has no ticker, so it always rebuilds.
    if (!force && this.state.running && text === this.lastAgentText) return;
    this.lastAgentText = text;

    this.agent.text = text;
    this.agent.color = face.colour;
    this.agent.tooltip = this.agentTooltip(face.word);
    // Screen readers get the words without the codicon syntax, which otherwise
    // reads out as literal "dollar sign, open paren, sync".
    this.agent.accessibilityInformation = {
      label: vscode.l10n.t('dakcoder, {0}', detail ? `${face.word}, ${detail}` : face.word),
      role: 'button',
    };
  }

  private agentFace(): { icon: string; word: string; colour?: vscode.ThemeColor } {
    const warning = new vscode.ThemeColor('statusBarItem.warningForeground');
    const error = new vscode.ThemeColor('statusBarItem.errorForeground');
    const pending = this.state.pendingApprovals.length;

    if (this.offlineNote !== undefined || this.state.connection === 'offline') {
      return { icon: '$(debug-disconnect)', word: vscode.l10n.t('offline'), colour: warning };
    }
    if (this.state.connection === 'reconnecting') {
      return { icon: '$(sync~spin)', word: vscode.l10n.t('reconnecting'), colour: warning };
    }
    if (this.state.connection === 'connecting' && !this.state.running) {
      return { icon: '$(sync~spin)', word: vscode.l10n.t('connecting') };
    }
    if (pending > 0) {
      return {
        icon: '$(person)',
        word:
          pending === 1
            ? vscode.l10n.t('needs approval')
            : vscode.l10n.t('needs {0} approvals', pending),
        colour: warning,
      };
    }
    if (this.state.running) {
      return { icon: '$(sync~spin)', word: this.state.modeLabel };
    }
    if (!this.state.sessionId) {
      return { icon: '$(hubot)', word: vscode.l10n.t('idle') };
    }

    switch (this.state.status) {
      case 'done':
        return { icon: '$(pass)', word: vscode.l10n.t('done') };
      case 'unverified':
        return { icon: '$(warning)', word: vscode.l10n.t('unverified'), colour: warning };
      case 'no_progress':
        return { icon: '$(warning)', word: vscode.l10n.t('no progress'), colour: warning };
      case 'exhausted':
        return { icon: '$(warning)', word: vscode.l10n.t('out of turns'), colour: warning };
      case 'aborted':
        return { icon: '$(circle-slash)', word: vscode.l10n.t('stopped'), colour: warning };
      case 'error':
        return { icon: '$(error)', word: vscode.l10n.t('failed'), colour: error };
      default:
        // C2: an outcome added after this build is shown verbatim rather than
        // flattened into "idle", which would claim the run ended cleanly.
        return { icon: '$(info)', word: this.state.status };
    }
  }

  /**
   * `turn 4`, not `4/8`.
   *
   * Nothing on the wire carries a turn ceiling or a current plan step —
   * `PlanStep.status` is `unknown` by construction — so a denominator here would
   * be invented. The turn number alone still answers "is it moving?", which is
   * what the developer is actually asking.
   */
  private agentDetail(): string {
    if (!this.state.running) return '';
    const parts = [vscode.l10n.t('turn {0}', this.state.turn || 1)];
    // Only above 1, where it means the retry after a failed gate.
    if (this.state.attempt > 1) parts.push(vscode.l10n.t('attempt {0}', this.state.attempt));
    return parts.join(' · ');
  }

  private agentTooltip(word: string): vscode.MarkdownString {
    const md = new vscode.MarkdownString(undefined, true);
    // Scoped to the one command this tooltip offers; blanket trust in a string
    // that carries server text would be a command-injection surface.
    md.isTrusted = { enabledCommands: [this.commands.stop] };

    const lines: string[] = [`**${vscode.l10n.t('dakcoder')}** — ${word}`];
    if (this.state.task) lines.push(escape(clip(this.state.task, 120)));

    if (this.state.running) {
      lines.push(vscode.l10n.t('Turn {0}, attempt {1}', this.state.turn || 1, this.state.attempt));
      const tool = this.state.currentTool;
      if (tool) lines.push(vscode.l10n.t('Running `{0}`', tool));
      lines.push(vscode.l10n.t('Elapsed {0}', formatDuration(Math.round(this.state.elapsedMs / 1000))));
    } else if (this.state.summary) {
      lines.push(escape(clip(this.state.summary, 200)));
    }

    const meter = this.state.contextMeter();
    if (meter) lines.push(`${meter} · ${vscode.l10n.t('cache {0}', this.state.cacheLabel())}`);
    if (this.state.mutations.length) lines.push(this.state.mutationSummary());

    if (this.state.connection === 'offline') {
      lines.push(
        this.offlineNote ??
          vscode.l10n.t('The event stream dropped and is being retried. The run keeps going on the runtime.'),
      );
    }
    if (!this.state.running && isResumable(this.state.status)) {
      lines.push(vscode.l10n.t('This session can be resumed.'));
    }

    md.appendMarkdown(lines.join('\n\n'));
    md.appendMarkdown('\n\n---\n\n');
    if (this.state.running) {
      // The link is the mouse shortcut. Keyboard reach is the palette command,
      // which is named here so it is discoverable without one.
      md.appendMarkdown(
        `[${vscode.l10n.t('Stop this task')}](command:${this.commands.stop}) — ${vscode.l10n.t('or run "dakcoder: Stop Current Task" from the Command Palette.')}\n\n`,
      );
    }
    md.appendMarkdown(vscode.l10n.t('Click to open the dakcoder panel.'));
    return md;
  }

  /**
   * The gateway is unreachable, which is a state and not an error (§7.4). The
   * event stream only sees the local runtime, so whoever talks to the gateway
   * has to tell the bar.
   */
  setGatewayOffline(reason?: string): void {
    this.offlineNote = reason ?? vscode.l10n.t('The agent needs the IT 2.0 gateway to reach the model. Retrying.');
    this.renderAgent();
  }

  clearGatewayOffline(): void {
    if (this.offlineNote === undefined) return;
    this.offlineNote = undefined;
    this.renderAgent();
  }

  // ── the quota item ────────────────────────────────────────────────────────

  /**
   * Fetch the quota snapshot. Public so sign-in — a user-initiated moment, not
   * activation — can put the item on screen without waiting for a first task.
   */
  async refresh(force = false): Promise<void> {
    if (!force && Date.now() - this.fetchedAt < REFRESH_FLOOR_MS) return;
    // Single-flight: task start and a `quota` event land within milliseconds of
    // each other often enough to matter.
    this.inFlight ??= this.fetchQuota().finally(() => {
      this.inFlight = undefined;
    });
    return this.inFlight;
  }

  private async fetchQuota(): Promise<void> {
    try {
      this.snapshot = await this.opts.quota();
      this.fetchedAt = Date.now();
      this.stale = false;
      this.clearGatewayOffline();
      this.renderQuota();
    } catch (err) {
      if (err instanceof HttpError && (err.status === 401 || err.status === 403)) {
        // Not signed in, or no longer entitled. Someone else's quota is not
        // ours to display, and a stale figure would outlive the session.
        this.snapshot = undefined;
        this.quota.hide();
        return;
      }
      this.opts.log?.warn(`quota refresh failed: ${String(err)}`);
      // Keep the last figure but stop presenting it as current.
      this.stale = true;
      this.renderQuota();
    }
  }

  private renderQuota(): void {
    if (!this.snapshot) {
      this.quota.hide();
      return;
    }
    const view = formatQuota(this.snapshot);
    this.quota.text = view.text;
    this.quota.color =
      view.severity === 'critical'
        ? new vscode.ThemeColor('statusBarItem.errorForeground')
        : view.severity === 'warning'
          ? new vscode.ThemeColor('statusBarItem.warningForeground')
          : undefined;
    this.quota.tooltip = quotaTooltip(this.snapshot, this.stale ? this.fetchedAt : undefined);
    this.quota.accessibilityInformation = {
      label: vscode.l10n.t('dakcoder quota, {0}', accessibleQuota(this.snapshot, view)),
      role: 'button',
    };
    this.quota.show();
  }

  // ── teardown ──────────────────────────────────────────────────────────────

  private stopTimers(): void {
    if (this.tick) clearInterval(this.tick);
    if (this.poll) clearInterval(this.poll);
    this.tick = undefined;
    this.poll = undefined;
  }

  dispose(): void {
    this.stopTimers();
    for (const subscription of this.subscriptions) subscription.dispose();
    this.agent.dispose();
    this.quota.dispose();
  }
}

// ── formatting, exported for its own tests ──────────────────────────────────

export interface QuotaView {
  text: string;
  /** The percentage shown, or undefined when nothing on the wire supports one. */
  pct?: number;
  /** What `pct` is a percentage *of*. A bare number that could mean two limits is the bug. */
  pctName?: string;
  severity: Severity;
}

/**
 * `$(clock) 2h 41m · 3/12 · 38%` — window time left, sessions used this week,
 * and the limit closest to biting.
 *
 * `tightest.pct` is what protocol.ts says the status bar shows, and it is taken
 * from the wire rather than recomputed. The one division here is the fallback
 * for a gateway that does not send `tightest`, and it lives in this exported
 * function precisely so the quota view divides in the same place or not at all.
 */
export function formatQuota(snapshot: QuotaSnapshot): QuotaView {
  const segments: string[] = [];

  // `expires_in` is the window's own field; `resets_in` is the generic one.
  const seconds = snapshot.window?.expires_in ?? snapshot.window?.resets_in;
  if (typeof seconds === 'number' && seconds > 0) segments.push(formatDuration(Math.round(seconds)));

  const sessions = snapshot.week?.sessions;
  if (sessions && sessions.cap > 0) segments.push(`${sessions.used}/${sessions.cap}`);

  let pct: number | undefined;
  let pctName: string | undefined;
  if (snapshot.tightest && typeof snapshot.tightest.pct === 'number') {
    pct = snapshot.tightest.pct;
    pctName = snapshot.tightest.name;
  } else if (snapshot.week && snapshot.week.cap > 0) {
    pct = (snapshot.week.used / snapshot.week.cap) * 100;
    pctName = vscode.l10n.t('weekly tokens');
  }
  if (pct !== undefined) segments.push(vscode.l10n.t('{0}%', Math.round(pct)));

  const severity: Severity =
    pct === undefined ? 'normal' : pct >= CRITICAL_PCT ? 'critical' : pct >= WARNING_PCT ? 'warning' : 'normal';

  // The glyph changes with the threshold so the warning is not colour alone.
  const icon = severity === 'critical' ? '$(error)' : severity === 'warning' ? '$(warning)' : '$(clock)';
  const body = segments.length ? segments.join(' · ') : vscode.l10n.t('quota');

  return {
    text: `${icon} ${body}`,
    ...(pct === undefined ? {} : { pct }),
    ...(pctName === undefined ? {} : { pctName }),
    severity,
  };
}

/** The full breakdown. Every line is omitted rather than zero-filled when absent. */
export function quotaTooltip(snapshot: QuotaSnapshot, staleSince?: number): vscode.MarkdownString {
  const md = new vscode.MarkdownString(undefined, true);
  const lines: string[] = [`**${vscode.l10n.t('dakcoder quota')}**`];

  const who = [snapshot.role, snapshot.tier].filter(Boolean).join(' · ');
  if (who) lines.push(escape(who));

  const window = snapshot.window;
  if (window) {
    const parts: string[] = [];
    if (window.cap > 0) parts.push(vscode.l10n.t('{0} of {1} tokens', thousands(window.used), thousands(window.cap)));
    const seconds = window.expires_in ?? window.resets_in;
    if (typeof seconds === 'number' && seconds > 0) parts.push(vscode.l10n.t('{0} left', formatDuration(Math.round(seconds))));
    if (window.runs && window.runs.cap > 0) parts.push(runsPhrase(window.runs));
    if (parts.length) lines.push(`${vscode.l10n.t('Current window')}: ${parts.join(' · ')}`);
    if (window.opened_at) lines.push(vscode.l10n.t('Opened {0}', escape(window.opened_at)));
  }

  const week = snapshot.week;
  if (week) {
    const parts: string[] = [];
    if (week.sessions && week.sessions.cap > 0) parts.push(sessionsPhrase(week.sessions));
    if (week.cap > 0) parts.push(vscode.l10n.t('{0} of {1} tokens', thousands(week.used), thousands(week.cap)));
    if (parts.length) lines.push(`${vscode.l10n.t('This week')}: ${parts.join(' · ')}`);
  }

  if (snapshot.hour && snapshot.hour.cap > 0) {
    lines.push(
      `${vscode.l10n.t('This hour')}: ${vscode.l10n.t('{0} of {1} tokens', thousands(snapshot.hour.used), thousands(snapshot.hour.cap))}`,
    );
  }

  const tightest = snapshot.tightest;
  if (tightest) {
    lines.push(
      vscode.l10n.t(
        'Closest limit: {0} at {1}%',
        escape(tightest.name),
        String(Math.round(tightest.pct)),
      ),
    );
  }

  if (staleSince !== undefined) {
    lines.push(
      vscode.l10n.t(
        'The gateway did not answer the last refresh. Figures are from {0} ago.',
        formatDuration(Math.max(1, Math.round((Date.now() - staleSince) / 1000))),
      ),
    );
  }

  md.appendMarkdown(lines.join('\n\n'));
  md.appendMarkdown(`\n\n---\n\n${vscode.l10n.t('Click to open the quota view.')}`);
  return md;
}

/** `2h 41m`, `41m`, `58s`. Shared by the elapsed ticker so both round alike. */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  if (seconds < 60) return vscode.l10n.t('{0}s', Math.round(seconds));
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return vscode.l10n.t('{0}m', minutes);
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return vscode.l10n.t('{0}h {1}m', hours, minutes % 60);
  return vscode.l10n.t('{0}d {1}h', Math.floor(hours / 24), hours % 24);
}

// ── helpers ─────────────────────────────────────────────────────────────────

/** `vscode.l10n` has no ICU plurals, so "1 sessions" needs its own string. */
function sessionsPhrase(sessions: QuotaWindow): string {
  return sessions.used === 1
    ? vscode.l10n.t('1 session of {0}', sessions.cap)
    : vscode.l10n.t('{0} sessions of {1}', sessions.used, sessions.cap);
}

function runsPhrase(runs: QuotaWindow): string {
  return runs.used === 1
    ? vscode.l10n.t('1 run of {0}', runs.cap)
    : vscode.l10n.t('{0} runs of {1}', runs.used, runs.cap);
}

function accessibleQuota(snapshot: QuotaSnapshot, view: QuotaView): string {
  const parts: string[] = [];
  const seconds = snapshot.window?.expires_in ?? snapshot.window?.resets_in;
  if (typeof seconds === 'number' && seconds > 0) {
    parts.push(vscode.l10n.t('{0} left in this window', formatDuration(Math.round(seconds))));
  }
  if (snapshot.week?.sessions) parts.push(sessionsPhrase(snapshot.week.sessions));
  if (view.pct !== undefined) {
    parts.push(vscode.l10n.t('{0} at {1}%', view.pctName ?? vscode.l10n.t('usage'), Math.round(view.pct)));
  }
  return parts.join(', ');
}

function thousands(value: number): string {
  return value.toLocaleString();
}

/** Task text and gateway strings reach a MarkdownString; nothing there may become markup. */
function escape(text: string): string {
  return text.replace(/[\\`*_{}[\]()#+\-.!|]/g, (c) => `\\${c}`);
}

function clip(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}
