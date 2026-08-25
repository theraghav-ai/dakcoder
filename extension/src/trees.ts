/**
 * The three trees: Sessions, Quota, Context Inspector.
 *
 * Three decisions run through all of it.
 *
 * **Nothing here invents server data.** Every number rendered came off the wire
 * in `protocol.ts`. Where a field the design asked for does not exist — per-file
 * context token counts are the live example — the row says so in a sentence
 * instead of showing a plausible breakdown. A fabricated breakdown is worse than
 * a missing one: the developer acts on it, and it was never measured.
 *
 * **Colour never carries meaning alone.** Every status row pairs a `ThemeIcon`
 * with the status *word* in its description, so the tree is readable in a
 * high-contrast theme, in a screenshot pasted into a support chat, and by a
 * screen reader — none of which see `charts.red`.
 *
 * **Quota never polls while idle.** The gateway is a shared service for the
 * whole pilot; a 60-second timer in every open window is a self-inflicted load
 * test. The timer is armed by a task starting and disarmed when the last one
 * ends (§7.2).
 */

import * as vscode from 'vscode';

import { HttpError, type GatewayClient, type RuntimeClient } from './client';
import {
  isResumable,
  type ContextSnapshot,
  type QuotaSnapshot,
  type QuotaWindow,
  type SessionSummary,
  type WireEvent,
} from './protocol';

// ── shared node ─────────────────────────────────────────────────────────────

/**
 * One row. Children are built eagerly because every tree here is small and
 * already fully in hand after one request — a lazy `getChildren` per row would
 * turn one round trip into N for no benefit.
 */
class Node extends vscode.TreeItem {
  readonly children: Node[] = [];
  parent?: Node;

  add(child: Node): Node {
    child.parent = this;
    this.children.push(child);
    if (this.collapsibleState === vscode.TreeItemCollapsibleState.None) {
      this.collapsibleState = vscode.TreeItemCollapsibleState.Collapsed;
    }
    return child;
  }
}

/** A session row. Carries the summary so command handlers need no second fetch. */
class SessionNode extends Node {
  constructor(
    readonly session: SessionSummary,
    label: string,
  ) {
    super(label, vscode.TreeItemCollapsibleState.Collapsed);
  }
}

function group(label: string, icon: string, expanded = true): Node {
  const node = new Node(
    label,
    expanded ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed,
  );
  node.iconPath = new vscode.ThemeIcon(icon);
  return node;
}

function leaf(label: string, description: string, icon: string, tooltip: string): Node {
  const node = new Node(label, vscode.TreeItemCollapsibleState.None);
  node.description = description;
  node.iconPath = new vscode.ThemeIcon(icon);
  node.tooltip = md(tooltip);
  return node;
}

/**
 * Hovers are `MarkdownString`, but the text inside them is frequently the
 * server's — a task title, a summary, a layer name. Untrusted and un-HTML'd is
 * not enough on its own: plain markdown still renders links and images, so
 * server text is escaped at the point it is interpolated (`esc`).
 */
function md(text: string): vscode.MarkdownString {
  const value = new vscode.MarkdownString(text);
  value.isTrusted = false;
  value.supportHtml = false;
  return value;
}

function esc(text: string): string {
  return text.replace(/[\\`*_{}[\]()#+\-.!|<>]/g, '\\$&');
}

// ── formatting ──────────────────────────────────────────────────────────────

/** Abbreviated for a `description`, which VS Code truncates hard. */
function short(n: number): string {
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n) < 1000) return String(Math.round(n));
  if (Math.abs(n) < 1_000_000) return `${(n / 1000).toFixed(Math.abs(n) < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

function exact(n: number): string {
  return Number.isFinite(n) ? n.toLocaleString() : '—';
}

function duration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return vscode.l10n.t('unknown');
  const total = Math.round(seconds);
  if (total < 60) return vscode.l10n.t('{0}s', total);
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return vscode.l10n.t('{0}m', minutes);
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? vscode.l10n.t('{0}h {1}m', hours, rest) : vscode.l10n.t('{0}h', hours);
}

/**
 * Timestamps are ISO strings on the wire, but a string that fails to parse is
 * shown verbatim rather than as `Invalid Date` — the raw value is at least
 * evidence for a bug report.
 */
function stamp(iso: string | null | undefined): { relative: string; absolute: string } {
  if (!iso) return { relative: '—', absolute: vscode.l10n.t('not reported') };
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return { relative: iso, absolute: iso };
  const ago = (Date.now() - at.getTime()) / 1000;
  return {
    relative: ago < 45 ? vscode.l10n.t('just now') : vscode.l10n.t('{0} ago', duration(ago)),
    absolute: at.toLocaleString(),
  };
}

function pctOf(used: number, cap: number): number | undefined {
  return Number.isFinite(used) && Number.isFinite(cap) && cap > 0 ? (used / cap) * 100 : undefined;
}

/** `12.4k / 50k · 25%`, or an honest gap where the cap is not reported. */
function meter(used: number, cap: number | undefined): string {
  if (!Number.isFinite(used)) return vscode.l10n.t('not reported');
  if (cap === undefined || !Number.isFinite(cap) || cap <= 0) {
    return vscode.l10n.t('{0} used · no cap reported', short(used));
  }
  return `${short(used)} / ${short(cap)} · ${Math.round((used / cap) * 100)}%`;
}

/**
 * The pressure of a used/cap pair, as an icon, a theme colour *and* a word.
 * Thresholds are §7.1's: default, warning at 80%, error at 95%.
 */
function pressure(pct: number | undefined): { icon: string; color?: vscode.ThemeColor; word: string } {
  if (pct === undefined) return { icon: 'dash', word: vscode.l10n.t('not reported') };
  if (pct >= 95) {
    return {
      icon: 'error',
      color: new vscode.ThemeColor('list.errorForeground'),
      word: vscode.l10n.t('at the limit'),
    };
  }
  if (pct >= 80) {
    return {
      icon: 'warning',
      color: new vscode.ThemeColor('list.warningForeground'),
      word: vscode.l10n.t('near the limit'),
    };
  }
  return { icon: 'circle-filled', word: vscode.l10n.t('within budget') };
}

function oneLine(text: string, limit = 90): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > limit ? `${flat.slice(0, limit - 1)}…` : flat || vscode.l10n.t('(no task text)');
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ── session status ──────────────────────────────────────────────────────────

interface Face {
  icon: string;
  color?: vscode.ThemeColor;
  word: string;
}

const KNOWN_STATUSES: ReadonlySet<string> = new Set([
  'running',
  'done',
  'unverified',
  'no_progress',
  'exhausted',
  'error',
  'aborted',
]);

/**
 * Additive-only (C2): the default arm renders an unrecognised status as a
 * neutral row carrying the server's own word. A newer runtime inventing a
 * status must degrade to a row you can read, never to a thrown render.
 */
function statusFace(status: string): Face {
  switch (status) {
    case 'running':
      // The spin modifier is the only motion in the tree, and it answers the
      // one question a developer opens this view to ask.
      return { icon: 'sync~spin', word: vscode.l10n.t('running') };
    case 'done':
      return {
        icon: 'pass-filled',
        color: new vscode.ThemeColor('charts.green'),
        word: vscode.l10n.t('done'),
      };
    case 'unverified':
      return {
        icon: 'warning',
        color: new vscode.ThemeColor('charts.yellow'),
        word: vscode.l10n.t('unverified'),
      };
    case 'no_progress':
      return {
        icon: 'circle-slash',
        color: new vscode.ThemeColor('charts.orange'),
        word: vscode.l10n.t('no progress'),
      };
    case 'exhausted':
      return {
        icon: 'flame',
        color: new vscode.ThemeColor('charts.orange'),
        word: vscode.l10n.t('turns exhausted'),
      };
    case 'error':
      return {
        icon: 'error',
        color: new vscode.ThemeColor('charts.red'),
        word: vscode.l10n.t('error'),
      };
    case 'aborted':
      return {
        icon: 'stop-circle',
        color: new vscode.ThemeColor('charts.gray'),
        word: vscode.l10n.t('aborted'),
      };
    default:
      return { icon: 'circle-outline', word: status };
  }
}

/**
 * The four shapes a menu needs to distinguish. `done` is deliberately its own
 * value rather than folded in with the other terminal states: a finished run
 * takes a *follow-up*, and offering "Resume" there would re-enter the gate loop
 * on a change that already passed.
 */
function sessionContext(status: string): string {
  if (status === 'running') return 'dakcoder.session.running';
  if (status === 'done') return 'dakcoder.session.done';
  if (isResumable(status)) return 'dakcoder.session.resumable';
  return 'dakcoder.session.unknown';
}

// ── dependencies ────────────────────────────────────────────────────────────

export interface TreeDeps {
  runtime: RuntimeClient;
  gateway: GatewayClient;
  log: vscode.LogOutputChannel;
  /** Filter state lives in workspace state: filters are per-repo, not per-user. */
  state: vscode.Memento;
  /** The transcript lives in the chat webview, which this module must not import. */
  openSession(session: SessionSummary): void | Thenable<void>;
  /**
   * A follow-up on a finished session. There is no follow-up route on the wire
   * (`/messages` steers a *running* run and `/resume` refuses `done`), so the
   * host owns what it means — replay the transcript and open a new turn. Kept as
   * a callback rather than guessed at here.
   */
  followUp(session: SessionSummary, note: string): void | Thenable<void>;
}

// ── sessions ────────────────────────────────────────────────────────────────

interface Filters {
  /** Empty = every status. Multi-select, so it cannot be pushed to `?status=`. */
  statuses: string[];
  /** `null` = every workspace. */
  workspace: string | null;
}

const FILTER_KEY = 'dakcoder.sessions.filters';

export class SessionsTree implements vscode.TreeDataProvider<Node>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  private roots?: Node[];
  private view?: vscode.TreeView<Node>;
  private filters: Filters;

  constructor(private readonly deps: TreeDeps) {
    const saved = deps.state.get<Filters>(FILTER_KEY);
    this.filters = { statuses: saved?.statuses ?? [], workspace: saved?.workspace ?? null };
  }

  attach(view: vscode.TreeView<Node>): void {
    this.view = view;
    this.describe();
  }

  getTreeItem(element: Node): vscode.TreeItem {
    return element;
  }

  getParent(element: Node): Node | undefined {
    return element.parent;
  }

  async getChildren(element?: Node): Promise<Node[]> {
    if (element) return element.children;
    this.roots ??= await this.build();
    return this.roots;
  }

  refresh(): void {
    this.roots = undefined;
    this.changed.fire(undefined);
  }

  /**
   * Additive-only: only the events that change what this tree shows trigger a
   * reload, and the default arm ignores everything else — including event types
   * this build has never heard of.
   */
  applyEvent(event: WireEvent): void {
    switch (event.type) {
      case 'finish':
      case 'error':
      case 'end':
        this.refresh();
        break;
      default:
        break;
    }
  }

  /** Select without focusing: the editor keeps the caret (a11y, §16). */
  async reveal(sessionId: string): Promise<void> {
    if (!this.view) return;
    const roots = await this.getChildren();
    const found = roots.find((node) => node instanceof SessionNode && node.session.id === sessionId);
    if (found) await this.view.reveal(found, { select: true, focus: false, expand: false });
  }

  // ── building ──

  private async build(): Promise<Node[]> {
    if (!this.deps.runtime.baseUrl) {
      // Distinct from an error: the runtime spawns lazily, and "not started yet"
      // has a completely different remedy from "the request failed".
      return [
        leaf(
          vscode.l10n.t('The runtime is not running yet'),
          '',
          'debug-disconnect',
          vscode.l10n.t('Past sessions appear once the local runtime has started.'),
        ),
      ];
    }

    let sessions: SessionSummary[];
    try {
      sessions = (await this.deps.runtime.sessions()).sessions;
    } catch (err) {
      this.deps.log.warn(`sessions could not be listed: ${message(err)}`);
      const row = leaf(
        vscode.l10n.t('Sessions could not be listed'),
        message(err),
        'error',
        vscode.l10n.t('The local runtime refused the request: {0}', esc(message(err))),
      );
      row.command = {
        command: 'dakcoder.sessions.refresh',
        title: vscode.l10n.t('Try again'),
      };
      return [row];
    }

    const running = sessions.filter((s) => s.status === 'running').length;
    if (this.view) {
      // A badge, not a notification: the count of live runs belongs where the
      // developer already looks, and must never pull focus.
      this.view.badge = running
        ? {
            value: running,
            tooltip:
              running === 1
                ? vscode.l10n.t('1 session is running')
                : vscode.l10n.t('{0} sessions are running', running),
          }
        : undefined;
    }
    this.knownWorkspaces = [...new Set(sessions.map((s) => s.workspace).filter(Boolean))].sort();

    const visible = sessions.filter((s) => this.matches(s));
    if (!visible.length) {
      const filtered = this.isFiltered;
      const row = leaf(
        filtered ? vscode.l10n.t('No session matches the filters') : vscode.l10n.t('No sessions yet'),
        '',
        filtered ? 'filter' : 'inbox',
        !filtered
          ? vscode.l10n.t('Sessions appear here once a task has been started.')
          : sessions.length === 1
            ? vscode.l10n.t('1 session is hidden by the current filters.')
            : vscode.l10n.t('{0} sessions are hidden by the current filters.', sessions.length),
      );
      if (filtered) {
        row.command = {
          command: 'dakcoder.sessions.clearFilters',
          title: vscode.l10n.t('Clear filters'),
        };
      }
      return [row];
    }

    // Newest first, and a running session outranks its timestamp: it is the one
    // thing on this list that is still changing.
    visible.sort((a, b) => {
      const live = Number(b.status === 'running') - Number(a.status === 'running');
      return live || Date.parse(b.created_at) - Date.parse(a.created_at) || 0;
    });
    return visible.map((session) => this.sessionNode(session));
  }

  private knownWorkspaces: string[] = [];

  private matches(session: SessionSummary): boolean {
    if (this.filters.statuses.length && !this.filters.statuses.includes(session.status)) return false;
    if (this.filters.workspace !== null && session.workspace !== this.filters.workspace) return false;
    return true;
  }

  private get isFiltered(): boolean {
    return this.filters.statuses.length > 0 || this.filters.workspace !== null;
  }

  private sessionNode(session: SessionSummary): SessionNode {
    const face = statusFace(session.status);
    const created = stamp(session.created_at);
    const node = new SessionNode(session, oneLine(session.task));
    node.id = `session:${session.id}`;
    node.iconPath = new vscode.ThemeIcon(face.icon, face.color);
    // The word is in the description on purpose: the icon's colour is the
    // redundant channel here, not the primary one.
    node.description = `${face.word} · ${created.relative}`;
    node.contextValue = sessionContext(session.status);
    node.tooltip = md(this.hover(session, face, created.absolute));
    node.command = {
      command: 'dakcoder.sessions.open',
      title: vscode.l10n.t('Open Session'),
      arguments: [node],
    };
    // accessibilityInformation, because "done · 4m ago" read after a truncated
    // task line is not enough on its own.
    node.accessibilityInformation = {
      label: vscode.l10n.t('{0}, {1}, started {2}', oneLine(session.task), face.word, created.relative),
    };

    if (session.summary) {
      node.add(
        leaf(
          vscode.l10n.t('Summary'),
          oneLine(session.summary, 60),
          'note',
          esc(session.summary),
        ),
      );
    }

    if (session.mutations.length) {
      const files = group(
        session.mutations.length === 1
          ? vscode.l10n.t('1 file changed')
          : vscode.l10n.t('{0} files changed', session.mutations.length),
        'diff-multiple',
        false,
      );
      files.id = `session:${session.id}:files`;
      for (const [index, path] of session.mutations.entries()) {
        const uri = resolveIn(session.workspace, path);
        const file = new Node(path, vscode.TreeItemCollapsibleState.None);
        file.id = `session:${session.id}:file:${index}`;
        file.resourceUri = uri;
        file.contextValue = 'dakcoder.session.file';
        file.tooltip = md(vscode.l10n.t('Touched by this session. Click to open.'));
        file.command = { command: 'vscode.open', title: vscode.l10n.t('Open File'), arguments: [uri] };
        files.add(file);
      }
      node.add(files);
    }

    if (session.queued > 0) {
      node.add(
        leaf(
          session.queued === 1
            ? vscode.l10n.t('1 steering message queued')
            : vscode.l10n.t('{0} steering messages queued', session.queued),
          '',
          'comment-discussion',
          vscode.l10n.t('The run reads queued messages before its next turn.'),
        ),
      );
    }
    if (session.winding_down) {
      node.add(
        leaf(
          vscode.l10n.t('Winding down'),
          '',
          'debug-pause',
          vscode.l10n.t('The run stops after the current turn, so work in flight completes coherently.'),
        ),
      );
    }
    return node;
  }

  private hover(session: SessionSummary, face: Face, createdAbsolute: string): string {
    const lines = [
      `**${esc(oneLine(session.task, 160))}**`,
      '',
      vscode.l10n.t('Status: {0}', face.word),
      vscode.l10n.t('Workspace: {0}', esc(session.workspace || '—')),
      vscode.l10n.t('Started: {0}', createdAbsolute),
    ];
    if (session.finished_at) {
      lines.push(vscode.l10n.t('Finished: {0}', stamp(session.finished_at).absolute));
      const seconds = (Date.parse(session.finished_at) - Date.parse(session.created_at)) / 1000;
      if (Number.isFinite(seconds) && seconds >= 0) {
        lines.push(vscode.l10n.t('Took: {0}', duration(seconds)));
      }
    }
    lines.push(
      session.events === 1
        ? vscode.l10n.t('1 event recorded')
        : vscode.l10n.t('{0} events recorded', session.events),
    );
    lines.push(
      session.mutations.length === 1
        ? vscode.l10n.t('1 file changed')
        : vscode.l10n.t('{0} files changed', session.mutations.length),
    );
    if (session.summary) lines.push('', esc(session.summary));
    lines.push('', `\`${esc(session.id)}\``);
    // Two spaces before each newline: a markdown hover otherwise joins the
    // lines into one paragraph.
    return lines.join('  \n');
  }

  // ── filters ──

  async pickStatus(): Promise<void> {
    const items = [...KNOWN_STATUSES].map((status) => ({
      label: statusFace(status).word,
      status,
      picked: this.filters.statuses.includes(status),
    }));
    const picked = await vscode.window.showQuickPick(items, {
      canPickMany: true,
      title: vscode.l10n.t('Filter sessions by status'),
      placeHolder: vscode.l10n.t('Pick none to show every status'),
    });
    if (!picked) return;
    await this.setFilters({ ...this.filters, statuses: picked.map((item) => item.status) });
  }

  async pickWorkspace(): Promise<void> {
    const current = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const items: (vscode.QuickPickItem & { workspace: string | null })[] = [
      { label: vscode.l10n.t('All workspaces'), workspace: null },
    ];
    if (current) {
      items.push({
        label: vscode.l10n.t('This workspace'),
        description: current,
        workspace: current,
      });
    }
    for (const workspace of this.knownWorkspaces) {
      if (workspace === current) continue;
      items.push({ label: workspace, workspace });
    }
    const picked = await vscode.window.showQuickPick(items, {
      title: vscode.l10n.t('Filter sessions by workspace'),
    });
    if (!picked) return;
    await this.setFilters({ ...this.filters, workspace: picked.workspace });
  }

  async clearFilters(): Promise<void> {
    await this.setFilters({ statuses: [], workspace: null });
  }

  private async setFilters(filters: Filters): Promise<void> {
    this.filters = filters;
    await this.deps.state.update(FILTER_KEY, filters);
    this.describe();
    this.refresh();
  }

  /**
   * The active filter is spelled out in the view's own description. A tree that
   * silently hides rows is the classic "where did my session go" bug report.
   */
  private describe(): void {
    void vscode.commands.executeCommand('setContext', 'dakcoder.sessionsFiltered', this.isFiltered);
    if (!this.view) return;
    const parts: string[] = [];
    if (this.filters.statuses.length) {
      parts.push(this.filters.statuses.map((status) => statusFace(status).word).join(', '));
    }
    if (this.filters.workspace !== null) parts.push(basename(this.filters.workspace));
    this.view.description = parts.length ? vscode.l10n.t('filtered: {0}', parts.join(' · ')) : undefined;
  }

  dispose(): void {
    this.changed.dispose();
  }
}

// ── quota ───────────────────────────────────────────────────────────────────

export class QuotaTree implements vscode.TreeDataProvider<Node>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  /** For the status bar (§7.1), so it does not open a second request path. */
  private readonly updated = new vscode.EventEmitter<QuotaSnapshot | undefined>();
  readonly onDidUpdate = this.updated.event;

  private roots?: Node[];
  private timer?: ReturnType<typeof setInterval>;
  private active = 0;
  private inFlight?: Promise<Node[]>;

  constructor(private readonly deps: TreeDeps) {}

  getTreeItem(element: Node): vscode.TreeItem {
    return element;
  }

  getParent(element: Node): Node | undefined {
    return element.parent;
  }

  async getChildren(element?: Node): Promise<Node[]> {
    if (element) return element.children;
    if (!this.roots) {
      // Single-flight: the 60-second timer and a manual refresh land together
      // often enough, and two concurrent gateway calls would race to set roots.
      this.inFlight ??= this.build().finally(() => {
        this.inFlight = undefined;
      });
      this.roots = await this.inFlight;
    }
    return this.roots;
  }

  refresh(): void {
    this.roots = undefined;
    this.changed.fire(undefined);
  }

  /** Task boundaries own the refresh cadence (§7.2), not a wall clock. */
  noteTaskStarted(): void {
    this.active += 1;
    this.refresh();
    this.timer ??= setInterval(() => this.refresh(), 60_000);
  }

  noteTaskFinished(): void {
    this.active = Math.max(0, this.active - 1);
    this.refresh();
    if (this.active === 0) this.disarm();
  }

  /**
   * A `quota` event means the server's numbers moved, so refetch. The event's
   * payload is deliberately *not* read: `WireEvent.data` is an untyped bag and
   * `QuotaSnapshot` is only guaranteed by `GET /v1/quota`. Parsing the event
   * into a snapshot would be inventing a shape the contract does not promise.
   */
  applyEvent(event: WireEvent): void {
    switch (event.type) {
      case 'quota':
        this.refresh();
        break;
      default:
        break;
    }
  }

  private async build(): Promise<Node[]> {
    let quota: QuotaSnapshot;
    try {
      quota = await this.deps.gateway.quota();
    } catch (err) {
      this.updated.fire(undefined);
      if (err instanceof HttpError && err.status === 401) {
        const row = leaf(
          vscode.l10n.t('Sign in to see your quota'),
          '',
          'account',
          vscode.l10n.t('Quota is held by the IT 2.0 gateway and is tied to your GitLab identity.'),
        );
        row.command = {
          command: 'dakcoder.signIn',
          title: vscode.l10n.t('Sign In'),
        };
        return [row];
      }
      this.deps.log.warn(`quota could not be read: ${message(err)}`);
      return [
        leaf(
          vscode.l10n.t('Quota is unavailable'),
          message(err),
          'cloud-offline',
          vscode.l10n.t(
            'The gateway did not answer: {0}. The agent needs the gateway to reach the model, so this is also why a run would fail right now.',
            esc(message(err)),
          ),
        ),
      ];
    }

    this.updated.fire(quota);
    const rows: Node[] = [];

    if (quota.tightest) {
      const { name, used, cap, pct } = quota.tightest;
      const face = pressure(Number.isFinite(pct) ? pct : pctOf(used, cap));
      const row = new Node(vscode.l10n.t('Tightest limit'), vscode.TreeItemCollapsibleState.None);
      row.description = `${name} · ${meter(used, cap)} · ${face.word}`;
      row.iconPath = new vscode.ThemeIcon(face.icon, face.color);
      row.contextValue = 'dakcoder.quota.tightest';
      row.tooltip = md(
        vscode.l10n.t('The limit closest to biting: {0}. {1} of {2} used.', esc(name), exact(used), exact(cap)),
      );
      rows.push(row);
    }

    // ── current window
    const windowGroup = group(vscode.l10n.t('Current window'), 'clock');
    windowGroup.contextValue = 'dakcoder.quota.window';
    const win = quota.window;
    if (win) {
      const opened = stamp(win.opened_at);
      windowGroup.add(
        leaf(
          vscode.l10n.t('Opened'),
          opened.relative,
          'history',
          vscode.l10n.t('Opened at {0}.', opened.absolute),
        ),
      );
      const expires = win.expires_in ?? win.resets_in;
      windowGroup.add(
        leaf(
          vscode.l10n.t('Expires in'),
          expires === undefined ? vscode.l10n.t('not reported') : duration(expires),
          'timeline-view-icon',
          expires === undefined
            ? vscode.l10n.t('The gateway did not report a window expiry.')
            : vscode.l10n.t('The window refills when it expires; a queued task can simply wait.'),
        ),
      );
      windowGroup.add(this.usageRow(vscode.l10n.t('Tokens'), win.used, win.cap));
      windowGroup.add(this.runsRow(vscode.l10n.t('Runs'), win.runs));
    } else {
      windowGroup.add(this.absent(vscode.l10n.t('No window is open')));
    }
    rows.push(windowGroup);

    // ── rolling week
    const weekGroup = group(vscode.l10n.t('Rolling week'), 'calendar');
    weekGroup.contextValue = 'dakcoder.quota.week';
    if (quota.week) {
      weekGroup.add(this.runsRow(vscode.l10n.t('Sessions'), quota.week.sessions));
      weekGroup.add(this.usageRow(vscode.l10n.t('Tokens'), quota.week.used, quota.week.cap));
    } else {
      weekGroup.add(this.absent(vscode.l10n.t('No weekly figures reported')));
    }
    rows.push(weekGroup);

    // ── rolling hour
    const hourGroup = group(vscode.l10n.t('Rolling hour'), 'watch', false);
    hourGroup.contextValue = 'dakcoder.quota.hour';
    if (quota.hour) {
      hourGroup.add(this.usageRow(vscode.l10n.t('Tokens'), quota.hour.used, quota.hour.cap));
      if (quota.hour.resets_in !== undefined) {
        hourGroup.add(
          leaf(
            vscode.l10n.t('Resets in'),
            duration(quota.hour.resets_in),
            'timeline-view-icon',
            vscode.l10n.t('The hourly window is the one that recovers fastest.'),
          ),
        );
      }
    } else {
      hourGroup.add(this.absent(vscode.l10n.t('No hourly figures reported')));
    }
    rows.push(hourGroup);

    // ── identity
    const who = [quota.role, quota.tier].filter(Boolean).join(' · ');
    rows.push(
      leaf(
        vscode.l10n.t('Role and tier'),
        who || vscode.l10n.t('not reported'),
        'account',
        who
          ? vscode.l10n.t('Your caps come from this role and tier. Changing them is a gateway-side decision.')
          : vscode.l10n.t('The gateway did not report a role or tier for this account.'),
      ),
    );

    if (this.active > 0) {
      rows.push(
        leaf(
          vscode.l10n.t('Refreshing every minute'),
          vscode.l10n.t('while a task runs'),
          'sync',
          vscode.l10n.t('Quota is not polled while the agent is idle.'),
        ),
      );
    }
    return rows;
  }

  private usageRow(label: string, used: number | undefined, cap: number | undefined): Node {
    if (used === undefined) return this.absent(label);
    const face = pressure(pctOf(used, cap ?? 0));
    const node = new Node(label, vscode.TreeItemCollapsibleState.None);
    node.description = `${meter(used, cap)} · ${face.word}`;
    node.iconPath = new vscode.ThemeIcon(face.icon, face.color);
    node.tooltip = md(
      cap === undefined
        ? vscode.l10n.t('{0} used. No cap was reported for this window.', exact(used))
        : vscode.l10n.t('{0} of {1} used.', exact(used), exact(cap)),
    );
    return node;
  }

  private runsRow(label: string, quota: QuotaWindow | undefined): Node {
    if (!quota) return this.absent(label);
    const face = pressure(pctOf(quota.used, quota.cap));
    const node = new Node(label, vscode.TreeItemCollapsibleState.None);
    node.description = `${exact(quota.used)} / ${exact(quota.cap)} · ${face.word}`;
    node.iconPath = new vscode.ThemeIcon(face.icon, face.color);
    node.tooltip = md(vscode.l10n.t('{0} of {1} used.', exact(quota.used), exact(quota.cap)));
    return node;
  }

  /** A reported gap, never a zero. A zero here would read as a measurement. */
  private absent(label: string): Node {
    return leaf(
      label,
      vscode.l10n.t('not reported'),
      'dash',
      vscode.l10n.t('The gateway did not send this figure. It is not zero — it is unknown.'),
    );
  }

  private disarm(): void {
    if (!this.timer) return;
    clearInterval(this.timer);
    this.timer = undefined;
  }

  dispose(): void {
    this.disarm();
    this.changed.dispose();
    this.updated.dispose();
  }
}

// ── context inspector ───────────────────────────────────────────────────────

/**
 * Which `by_layer` key is the working set.
 *
 * The keys are server-chosen strings, not an enum on the wire, so this matches
 * loosely and falls back to plain rendering. A miss costs one explanatory child
 * row; a hard-coded key that the server later renames would cost the whole
 * section.
 */
const WORKING_SET = /^(working[_ \-]?set|work(ing)?|files|reads)$/i;

export class ContextTree implements vscode.TreeDataProvider<Node>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<Node | undefined>();
  readonly onDidChangeTreeData = this.changed.event;

  private roots?: Node[];
  private sessionId?: string;
  private view?: vscode.TreeView<Node>;

  constructor(private readonly deps: TreeDeps) {}

  attach(view: vscode.TreeView<Node>): void {
    this.view = view;
  }

  getTreeItem(element: Node): vscode.TreeItem {
    return element;
  }

  getParent(element: Node): Node | undefined {
    return element.parent;
  }

  async getChildren(element?: Node): Promise<Node[]> {
    if (element) return element.children;
    this.roots ??= await this.build();
    return this.roots;
  }

  /** The host points this at whichever session the developer is looking at. */
  setSession(sessionId: string | undefined): void {
    if (this.sessionId === sessionId) return;
    this.sessionId = sessionId;
    this.refresh();
  }

  refresh(): void {
    this.roots = undefined;
    this.changed.fire(undefined);
  }

  /**
   * The snapshot moves on every turn, but re-reading it on every event would
   * make this view a load generator against the runtime. Turn boundaries and
   * compaction are the two moments the numbers actually change shape.
   */
  applyEvent(event: WireEvent): void {
    switch (event.type) {
      case 'turn_start':
      case 'gate':
      case 'finish':
        this.refresh();
        break;
      default:
        break;
    }
  }

  private async build(): Promise<Node[]> {
    if (!this.sessionId) {
      return [
        leaf(
          vscode.l10n.t('No session selected'),
          '',
          'inspect',
          vscode.l10n.t('Open a session to see what the server is holding in its context.'),
        ),
      ];
    }

    let snapshot: ContextSnapshot;
    try {
      snapshot = await this.deps.runtime.context(this.sessionId);
    } catch (err) {
      this.deps.log.warn(`context snapshot failed: ${message(err)}`);
      const gone = err instanceof HttpError && err.status === 404;
      return [
        leaf(
          gone ? vscode.l10n.t('This session has no context snapshot') : vscode.l10n.t('Context is unavailable'),
          gone ? '' : message(err),
          gone ? 'circle-slash' : 'error',
          gone
            ? vscode.l10n.t('The session may have been deleted, or it never reached a turn.')
            : vscode.l10n.t('The runtime refused the request: {0}', esc(message(err))),
        ),
      ];
    }

    if (this.view) {
      this.view.description = vscode.l10n.t('{0} · turn {1}', snapshot.mode, snapshot.turn);
    }

    const rows: Node[] = [];
    const face = pressure(snapshot.used_pct);
    const total = new Node(vscode.l10n.t('Context used'), vscode.TreeItemCollapsibleState.None);
    // used_pct comes off the wire; it is not recomputed here, so client and
    // server never disagree about the number in the header.
    total.description = `${short(snapshot.total_tokens)} / ${short(snapshot.budget)} · ${Math.round(snapshot.used_pct)}% · ${face.word}`;
    total.iconPath = new vscode.ThemeIcon(face.icon, face.color);
    total.contextValue = 'dakcoder.context.total';
    total.tooltip = md(
      vscode.l10n.t(
        '{0} of {1} tokens, as counted by the server.',
        exact(snapshot.total_tokens),
        exact(snapshot.budget),
      ),
    );
    rows.push(total);

    rows.push(
      leaf(
        vscode.l10n.t('Tool schemas'),
        short(snapshot.tool_schema_tokens),
        'tools',
        vscode.l10n.t(
          '{0} tokens of tool definitions, sent on every turn. This is the floor no compaction can reduce.',
          exact(snapshot.tool_schema_tokens),
        ),
      ),
    );

    rows.push(this.layers(snapshot));

    const shape = group(vscode.l10n.t('Shape'), 'graph', false);
    shape.contextValue = 'dakcoder.context.shape';
    shape.add(
      leaf(
        vscode.l10n.t('Messages'),
        exact(snapshot.messages),
        'comment',
        vscode.l10n.t('Messages currently in the window.'),
      ),
    );
    shape.add(
      leaf(
        vscode.l10n.t('Compactions'),
        exact(snapshot.compactions),
        'archive',
        snapshot.compactions === 0
          ? vscode.l10n.t('Nothing has been compacted in this session yet.')
          : snapshot.compactions === 1
            ? vscode.l10n.t(
                'Older turns have been replaced by a recap once. Check the recap in the transcript if the agent seems to have forgotten something.',
              )
            : vscode.l10n.t(
                'Older turns have been replaced by a recap {0} times. Check the recap in the transcript if the agent seems to have forgotten something.',
                exact(snapshot.compactions),
              ),
      ),
    );
    shape.add(
      leaf(
        vscode.l10n.t('Stale slices'),
        exact(snapshot.stale_slices),
        snapshot.stale_slices > 0 ? 'warning' : 'check',
        snapshot.stale_slices > 0
          ? vscode.l10n.t(
              'File slices in context that no longer match the file on disk. The agent may be reasoning about an older version.',
            )
          : vscode.l10n.t('Every file slice in context still matches the file on disk.'),
      ),
    );
    shape.add(
      leaf(
        vscode.l10n.t('Token counts'),
        snapshot.calibrated ? vscode.l10n.t('calibrated') : vscode.l10n.t('estimated'),
        snapshot.calibrated ? 'verified' : 'question',
        snapshot.calibrated
          ? vscode.l10n.t("Counts have been reconciled against the endpoint's own usage figures.")
          : vscode.l10n.t(
              'Counts are the tokeniser estimate; the endpoint has not yet reported usage for this session, so treat them as approximate.',
            ),
      ),
    );
    if (snapshot.prefix) {
      shape.add(
        leaf(
          vscode.l10n.t('Prefix'),
          oneLine(snapshot.prefix, 24),
          'key',
          // Deliberately descriptive rather than explanatory: the field is on the
          // wire, its cache semantics are the server's, and asserting more than
          // that would be a guess printed as a fact.
          vscode.l10n.t('The prefix identifier the server reports for this session: {0}', esc(snapshot.prefix)),
        ),
      );
    }
    rows.push(shape);
    return rows;
  }

  private layers(snapshot: ContextSnapshot): Node {
    const layers = group(vscode.l10n.t('By layer'), 'layers');
    layers.contextValue = 'dakcoder.context.layers';

    const entries = Object.entries(snapshot.by_layer).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      layers.add(
        leaf(
          vscode.l10n.t('No layer breakdown reported'),
          '',
          'dash',
          vscode.l10n.t('The server sent an empty by_layer map for this turn.'),
        ),
      );
      return layers;
    }

    for (const [key, tokens] of entries) {
      const pct = pctOf(tokens, snapshot.total_tokens);
      const node = new Node(humanise(key), vscode.TreeItemCollapsibleState.None);
      node.description =
        pct === undefined ? short(tokens) : `${short(tokens)} · ${Math.round(pct)}%`;
      node.iconPath = new vscode.ThemeIcon('symbol-namespace');
      node.contextValue = 'dakcoder.context.layer';
      node.tooltip = md(
        vscode.l10n.t('{0}: {1} tokens of the {2} in context.', esc(key), exact(tokens), exact(snapshot.total_tokens)),
      );

      if (WORKING_SET.test(key)) {
        // The honest gap. The design (§10.2) sketches a per-file breakdown, and
        // /v1/sessions/{id}/context carries no per-file counts — by_layer is a
        // map of totals. Splitting this total across the files the session read
        // would be arithmetic invented on the client and it would be wrong
        // exactly when it matters: the one enormous read this view exists to
        // find. So the total is shown, and the missing half is named.
        node.add(
          leaf(
            vscode.l10n.t('Per-file detail is not available'),
            '',
            'info',
            vscode.l10n.t(
              'The context endpoint reports per-layer totals only. A per-file breakdown needs a new server field; until then no breakdown is shown rather than a guessed one.',
            ),
          ),
        );
        node.collapsibleState = vscode.TreeItemCollapsibleState.Expanded;
      }
      layers.add(node);
    }
    return layers;
  }

  dispose(): void {
    this.changed.dispose();
  }
}

// ── paths ───────────────────────────────────────────────────────────────────

function resolveIn(workspace: string, path: string): vscode.Uri {
  // Mutations arrive workspace-relative, but an absolute path is accepted too —
  // and on Windows both `C:\x` and `/c:/x` show up in practice.
  if (/^([a-zA-Z]:[\\/]|[\\/])/.test(path)) return vscode.Uri.file(path);
  return vscode.Uri.joinPath(vscode.Uri.file(workspace), ...path.split(/[\\/]+/));
}

function basename(path: string): string {
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** Layer keys are server data, so they are not localised — they are displayed. */
function humanise(key: string): string {
  const spaced = key.replace(/[_-]+/g, ' ').trim();
  return spaced ? spaced[0].toUpperCase() + spaced.slice(1) : key;
}

// ── commands ────────────────────────────────────────────────────────────────

function sessionOf(target: unknown): SessionSummary | undefined {
  return target instanceof SessionNode ? target.session : undefined;
}

async function openSession(deps: TreeDeps, target: unknown): Promise<void> {
  const session = sessionOf(target);
  if (session) await deps.openSession(session);
}

async function resumeSession(deps: TreeDeps, tree: SessionsTree, target: unknown): Promise<void> {
  const session = sessionOf(target);
  if (!session) return;
  if (!isResumable(session.status)) {
    // Belt and braces behind the menu `when` clause: a keybinding or the
    // palette can reach a command the menu would have hidden.
    void vscode.window.showWarningMessage(
      vscode.l10n.t('A {0} session cannot be resumed. Ask a follow-up instead.', statusFace(session.status).word),
    );
    return;
  }
  const note = await vscode.window.showInputBox({
    title: vscode.l10n.t('Resume session'),
    prompt: vscode.l10n.t('Anything to add before it retries? Leave empty to resume as-is.'),
    placeHolder: vscode.l10n.t('e.g. the failing test is in handler/pension_test.go'),
    ignoreFocusOut: true,
  });
  if (note === undefined) return;
  try {
    const resumed = await deps.runtime.resume(session.id, note);
    tree.refresh();
    await deps.openSession(resumed);
  } catch (err) {
    void vscode.window.showErrorMessage(vscode.l10n.t('The session could not be resumed: {0}', message(err)));
    deps.log.warn(`resume ${session.id} failed: ${message(err)}`);
  }
}

async function followUpSession(deps: TreeDeps, target: unknown): Promise<void> {
  const session = sessionOf(target);
  if (!session) return;
  const note = await vscode.window.showInputBox({
    title: vscode.l10n.t('Ask a follow-up'),
    // The wording is the whole point of separating this from Resume: this run
    // passed its gate, and the next turn builds on it rather than retrying it.
    prompt: vscode.l10n.t('This run finished. Your message becomes the next turn on the same transcript.'),
    ignoreFocusOut: true,
    validateInput: (value) =>
      value.trim() ? undefined : vscode.l10n.t('A follow-up needs something to act on.'),
  });
  if (!note?.trim()) return;
  await deps.followUp(session, note.trim());
}

async function deleteSession(deps: TreeDeps, tree: SessionsTree, target: unknown): Promise<void> {
  const session = sessionOf(target);
  if (!session) return;
  if (session.status === 'running') {
    void vscode.window.showWarningMessage(
      vscode.l10n.t('That session is still running. Stop it first, then delete it.'),
    );
    return;
  }
  const confirm = vscode.l10n.t('Delete');
  const detail =
    session.mutations.length === 1
      ? vscode.l10n.t('Its transcript is removed. The 1 file it changed is left exactly as it is.')
      : vscode.l10n.t(
          'Its transcript is removed. The {0} files it changed are left exactly as they are.',
          session.mutations.length,
        );
  const answer = await vscode.window.showWarningMessage(
    vscode.l10n.t('Delete this session?'),
    { modal: true, detail: `${oneLine(session.task, 120)}\n\n${detail}` },
    confirm,
  );
  if (answer !== confirm) return;
  try {
    await deps.runtime.deleteSession(session.id);
    tree.refresh();
  } catch (err) {
    const conflict = err instanceof HttpError && err.status === 409;
    void vscode.window.showErrorMessage(
      conflict
        ? vscode.l10n.t('The runtime says that session is still running.')
        : vscode.l10n.t('The session could not be deleted: {0}', message(err)),
    );
    deps.log.warn(`delete ${session.id} failed: ${message(err)}`);
  }
}

/**
 * Revert shows the exact paths first, then asks.
 *
 * "Revert my last task" is one keystroke away from being fired by accident, and
 * the operation deletes files. The plan is fetched, rendered in full, and only
 * then applied — and the running-session guard is checked here as well as
 * server-side, because a 409 after the confirmation dialog is a worse
 * experience than never showing the dialog.
 */
async function revertSession(deps: TreeDeps, tree: SessionsTree, target: unknown): Promise<void> {
  const session = sessionOf(target);
  if (!session) return;
  if (session.status === 'running') {
    void vscode.window.showWarningMessage(
      vscode.l10n.t('That session is still running. Stop it before reverting, or the revert races its own writes.'),
    );
    return;
  }

  let plan;
  try {
    plan = await deps.runtime.revertPlan(session.id);
  } catch (err) {
    void vscode.window.showErrorMessage(
      vscode.l10n.t('The revert plan could not be read: {0}', message(err)),
    );
    deps.log.warn(`revert plan ${session.id} failed: ${message(err)}`);
    return;
  }

  if (!plan.restore.length && !plan.delete.length) {
    void vscode.window.showInformationMessage(
      vscode.l10n.t('There is nothing to revert: this session left no file changes.'),
    );
    return;
  }

  const lines: string[] = [];
  if (plan.restore.length) {
    lines.push(
      plan.restore.length === 1
        ? vscode.l10n.t('1 file will be restored to git HEAD:')
        : vscode.l10n.t('{0} files will be restored to git HEAD:', plan.restore.length),
      ...plan.restore.map((path) => `  ${path}`),
    );
  }
  if (plan.delete.length) {
    if (lines.length) lines.push('');
    lines.push(
      plan.delete.length === 1
        ? vscode.l10n.t('1 file will be deleted (it has no baseline in git):')
        : vscode.l10n.t('{0} files will be deleted (they have no baseline in git):', plan.delete.length),
      ...plan.delete.map((path) => `  ${path}`),
    );
  }
  if (plan.blocked.length) {
    lines.push('');
    lines.push(
      plan.blocked.length === 1
        ? vscode.l10n.t('1 file will be left alone:')
        : vscode.l10n.t('{0} files will be left alone:', plan.blocked.length),
      ...plan.blocked.map((item) => `  ${item.path} — ${item.reason}`),
    );
  }

  const confirm = vscode.l10n.t('Revert');
  const answer = await vscode.window.showWarningMessage(
    vscode.l10n.t('Revert every file this session touched?'),
    { modal: true, detail: lines.join('\n') },
    confirm,
  );
  if (answer !== confirm) return;

  try {
    const applied = await deps.runtime.revert(session.id);
    tree.refresh();
    const restored = applied.restore.length;
    const deleted = applied.delete.length;
    void vscode.window.showInformationMessage(
      vscode.l10n.t('Reverted: {0} restored, {1} deleted.', restored, deleted),
    );
  } catch (err) {
    const conflict = err instanceof HttpError && err.status === 409;
    void vscode.window.showErrorMessage(
      conflict
        ? vscode.l10n.t('The runtime says that session is still running, so nothing was reverted.')
        : vscode.l10n.t('The revert failed: {0}', message(err)),
    );
    deps.log.warn(`revert ${session.id} failed: ${message(err)}`);
  }
}

async function copySessionId(target: unknown): Promise<void> {
  const session = sessionOf(target);
  if (!session) return;
  await vscode.env.clipboard.writeText(session.id);
  void vscode.window.setStatusBarMessage(vscode.l10n.t('Session id copied.'), 3000);
}

// ── registration ────────────────────────────────────────────────────────────

export interface Trees extends vscode.Disposable {
  sessions: SessionsTree;
  quota: QuotaTree;
  context: ContextTree;
}

export function register(deps: TreeDeps): Trees {
  const sessions = new SessionsTree(deps);
  const quota = new QuotaTree(deps);
  const context = new ContextTree(deps);

  const sessionsView = vscode.window.createTreeView('dakcoder.sessions', {
    treeDataProvider: sessions,
    showCollapseAll: true,
  });
  sessions.attach(sessionsView);

  const quotaView = vscode.window.createTreeView('dakcoder.quota', { treeDataProvider: quota });
  const contextView = vscode.window.createTreeView('dakcoder.context', {
    treeDataProvider: context,
    showCollapseAll: true,
  });
  context.attach(contextView);

  const disposables: vscode.Disposable[] = [
    sessionsView,
    quotaView,
    contextView,
    sessions,
    quota,
    context,
    vscode.commands.registerCommand('dakcoder.sessions.refresh', () => sessions.refresh()),
    vscode.commands.registerCommand('dakcoder.sessions.filterStatus', () => sessions.pickStatus()),
    vscode.commands.registerCommand('dakcoder.sessions.filterWorkspace', () => sessions.pickWorkspace()),
    vscode.commands.registerCommand('dakcoder.sessions.clearFilters', () => sessions.clearFilters()),
    vscode.commands.registerCommand('dakcoder.sessions.open', (node: unknown) => openSession(deps, node)),
    vscode.commands.registerCommand('dakcoder.sessions.resume', (node: unknown) =>
      resumeSession(deps, sessions, node),
    ),
    vscode.commands.registerCommand('dakcoder.sessions.followUp', (node: unknown) =>
      followUpSession(deps, node),
    ),
    vscode.commands.registerCommand('dakcoder.sessions.delete', (node: unknown) =>
      deleteSession(deps, sessions, node),
    ),
    vscode.commands.registerCommand('dakcoder.sessions.revert', (node: unknown) =>
      revertSession(deps, sessions, node),
    ),
    vscode.commands.registerCommand('dakcoder.sessions.copyId', (node: unknown) => copySessionId(node)),
    vscode.commands.registerCommand('dakcoder.quota.refresh', () => quota.refresh()),
    vscode.commands.registerCommand('dakcoder.context.refresh', () => context.refresh()),
    vscode.commands.registerCommand('dakcoder.showContextInspector', async (sessionId?: string) => {
      if (typeof sessionId === 'string') context.setSession(sessionId);
      // `focus` on the view command, not `vscode.window.showTextDocument`: this
      // reveals the panel without moving the caret out of the editor.
      await vscode.commands.executeCommand('dakcoder.context.focus');
    }),
  ];

  return {
    sessions,
    quota,
    context,
    dispose(): void {
      for (const item of disposables) item.dispose();
    },
  };
}
