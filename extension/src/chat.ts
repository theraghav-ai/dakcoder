/**
 * The chat panel: a `WebviewViewProvider` and the seam it posts across.
 *
 * Four decisions here are load-bearing.
 *
 * **`retainContextWhenHidden` stays off.** A retained webview costs tens of
 * megabytes of the 60 MB extension-host budget for a panel nobody is looking
 * at, so a hidden view is torn down and rebuilt. The transcript therefore lives
 * in two places instead: the webview's own `setState` (capped at 500 rows) and
 * the ring of recent wire events below. On re-create the webview reports the
 * furthest point in that ring it still holds and only what came after is
 * replayed — the same `since_id` idea `RuntimeClient.events` uses, for the same
 * reason. The cursor is the host's own counter and not the wire event id,
 * because event ids restart at 1 for every session and a ring spans several.
 *
 * **Outbound messages are rate-capped.** The budget is ~25 messages/second, and
 * a run that emits a tool result per 20 ms would blow past it. Messages are
 * queued and flushed once per 40 ms as a single `batch`, which the webview
 * unwraps and applies inside one `requestAnimationFrame`. The cap is a property
 * of the transport rather than a discipline callers have to remember.
 *
 * **Strings cross the seam, not `l10n` calls.** `vscode.l10n` does not exist
 * inside a webview, so the bundle is built here — where the extraction tooling
 * can see the literals — and shipped in `init`. Count-bearing strings ship as
 * two variants and the renderer picks by `n === 1`: `vscode.l10n` has no ICU
 * plurals, so a single template yields "Revert 1 files", which on a delete
 * dialog reads as a bug rather than as English.
 *
 * **Unknown event types are rows we skip.** C2 is additive-only; the renderer's
 * switch has a default arm that returns nothing, and this file never inspects
 * event types at all beyond passing them through.
 */

import { randomBytes } from 'node:crypto';
import * as vscode from 'vscode';

import type { Mode, WireEvent } from './protocol';

/** One flush per 40 ms is 25 messages/second — the §14 webview budget, exactly. */
const FLUSH_MS = 40;

/** Matches the webview's own row cap, so a replay can never outrun what it keeps. */
const RING = 500;

/**
 * Types that are relayed and never replayed.
 *
 * The same set the runtime calls transient, for the same reason: they are
 * superseded by what follows them, so replaying them would re-type an answer the
 * panel already holds in full. Keeping them out of the ring matters more than
 * that, though — a streamed reply is dozens of `assistant_delta` frames, and a
 * ring that stored them would evict the tool calls and gate results behind them
 * within a single turn.
 */
const TRANSIENT = new Set(['assistant_delta', 'heartbeat']);

export type ApprovalDecision = 'accept' | 'reject' | 'edit';

/** One event as the ring holds it: the event, whose session it was, and where
 *  it sits in the host's own ordering. */
interface RingEntry {
  seq: number;
  session: string;
  event: WireEvent;
}

/**
 * `symbol` and `package` deliberately carry a reference and not content: a
 * `@pkg:` mention costs tens of tokens where the package's files cost
 * thousands, and the server resolves it just in time. Nothing in this file ever
 * reads a mentioned file — the token text *is* the payload.
 */
export type MentionKind = 'file' | 'symbol' | 'package' | 'build' | 'diag';

export interface MentionItem {
  /** The literal text substituted into the composer. */
  insert: string;
  label: string;
  detail?: string;
}

export type SlashCommand =
  | 'scaffold'
  | 'service'
  | 'audit'
  | 'legacy'
  | 'migrate'
  | 'debug'
  | 'explain'
  | 'fix'
  | 'test'
  | 'wire'
  | 'compact'
  | 'rule';

/** What the panel knows about the run. Everything here comes from the host. */
export interface RunState {
  phase: 'idle' | 'running' | 'winding_down';
  mode?: Mode;
  turn?: number;
  /** The tool the working indicator names. Absent between calls, which is fine. */
  tool?: string;
  /** Epoch ms. The webview owns the ticking clock so the host is not a timer. */
  startedAt?: number;
  /** 1 or 2. Shown only when >1: "attempt 1" on every turn hides the retry. */
  attempt?: number;
  /** The gate stage blocking the run, if one is. The console's only colour. */
  blockedBy?: string;
  /** Pre-formatted by the host, so nothing here divides tokens by a budget. */
  context?: string;
}

/**
 * Everything the panel cannot do for itself. The provider owns presentation and
 * the two generic editor affordances (open a scratch document, copy); anything
 * that needs a session id, the runtime client or `gopls` belongs to the caller.
 */
export interface ChatHost {
  /** `steering` is true when the developer typed during a run — see the note on
   *  the queued chip in `chat.js`. */
  submit(text: string, steering: boolean): Promise<void> | void;
  // `steering` is what the *composer* believed when the developer pressed
  // send, and the host is right not to act on it: a run can end between the
  // last event and the keystroke. It is reported because the panel draws the
  // row differently, not because it decides anything.
  /** Abort now. Discards the turn in flight. */
  stop(): void;
  /** Stop after the current turn, so work in flight completes coherently. */
  windDown(): void;
  decide(approvalId: string, decision: ApprovalDecision): Promise<void> | void;
  /** Native `vscode.diff`, not a blob in the webview — Go source deserves real
   *  syntax highlighting and folding. */
  showDiff(approvalId: string): void;
  editArguments(approvalId: string): void;
  runSlashCommand(command: SlashCommand, argument: string): void;
  openPath(path: string): void;
  /**
   * Optional. `file` and `diag` are answered by the panel itself — they need
   * nothing but the workspace and the diagnostic collection — so a host only
   * implements this to reach `gopls` for `symbol` and `package`, or the last
   * build for `@build`. Absent is a legitimate answer: the popup then offers the
   * trigger and no candidates, which is honest about what is wired.
   *
   * A host must not implement it by calling `completions()` back; that is a
   * loop, and `ChatViewProvider` guards against it rather than trusting nobody
   * ever writes it.
   */
  complete?(kind: MentionKind, query: string): Promise<MentionItem[]>;
}

// ── the wire between host and webview ───────────────────────────────────────

interface SlashSpec {
  name: SlashCommand;
  hint: string;
}

interface MentionSpec {
  /** What the developer types: `@`, `@#`, `@pkg:`, `@build`, `@diag`. */
  trigger: string;
  hint: string;
  kind: MentionKind;
  /** Rendered as a note in the popup, because "sends a reference" is the whole
   *  reason a symbol mention is cheap and a developer should be told. */
  reference: boolean;
}

type HostMessage =
  | {
      type: 'init';
      strings: Record<string, string>;
      maxRows: number;
      commands: SlashSpec[];
      mentions: MentionSpec[];
    }
  | { type: 'batch'; messages: HostMessage[] }
  | { type: 'event'; event: WireEvent; session: string; seq: number }
  | { type: 'user'; text: string; steering: boolean }
  | { type: 'run'; state: RunState }
  | { type: 'offline'; reason?: string }
  | { type: 'queued'; count: number }
  | { type: 'notice'; level: 'info' | 'warn' | 'error'; text: string }
  | { type: 'mentions'; token: number; items: MentionItem[] }
  | { type: 'approval-resolved'; id: string; decision: ApprovalDecision }
  | { type: 'session'; id: string }
  | { type: 'clear' }
  | { type: 'focus' };

// ── the provider ────────────────────────────────────────────────────────────

export class ChatViewProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  static readonly viewType = 'dakcoder.chat';

  private view?: vscode.WebviewView;
  /**
   * The conversation the panel is showing.
   *
   * The panel shows one at a time. Opening a different session used to leave the
   * previous one's rows on screen and append the new one's underneath, so a
   * developer clicking through the Sessions tree accumulated every conversation
   * they had looked at in one transcript.
   */
  private session = '';
  private readonly ring: RingEntry[] = [];
  /**
   * A host-side cursor over the ring, independent of any session.
   *
   * Wire event ids restart at 1 for every session, so they cannot order a ring
   * that spans several — a replay filtered on `event.id > lastEventId` silently
   * dropped a new session's opening events because an older session had reached
   * higher ids. This counter only ever goes up.
   */
  private seq = 0;
  private readonly queue: HostMessage[] = [];
  private timer?: NodeJS.Timeout;
  private run: RunState = { phase: 'idle' };
  private offline?: string;
  private queued = 0;
  /** Set while a mention is delegated to the host; see `completions`. */
  private delegating = false;
  private readonly disposables: vscode.Disposable[] = [];

  constructor(
    private readonly extensionUri: vscode.Uri,
    private readonly host: ChatHost,
    private readonly log: vscode.LogOutputChannel,
  ) {}

  // ── what the extension calls ──────────────────────────────────────────────

  /**
   * Feed one C2 event to the transcript. Unknown types are the webview's problem
   * to ignore, not this method's to filter — filtering here would make the
   * additive-only guarantee depend on a list kept in two files.
   *
   * `session` travels with the event because the renderer keys its rows by it.
   * Event ids are only unique *within* a session, so without this a second
   * conversation's `assistant:1` lands on the first one's row and replaces the
   * answer that was already on screen.
   */
  push(event: WireEvent, session = ''): void {
    // Defensive rather than the main path: `showSession` is called on every
    // deliberate switch, and gets the panel cleared before the first event
    // rather than on it. This catches a switch nothing announced.
    if (session) this.showSession(session);
    // Numbered even when it is not kept: the cursor is what tells the webview a
    // message is new, and an unnumbered delta would look like one it had already
    // applied.
    this.seq += 1;
    if (!TRANSIENT.has(event.type)) {
      this.ring.push({ seq: this.seq, session, event });
      if (this.ring.length > RING) this.ring.splice(0, this.ring.length - RING);
    }
    this.post({ type: 'event', event, session, seq: this.seq });
  }

  /** Echo a message the developer did not type into the composer — a command,
   *  a resumed session's follow-up — so the transcript stays the whole story. */
  echo(text: string, steering = false): void {
    this.post({ type: 'user', text, steering });
  }

  setRunState(state: RunState): void {
    this.run = state;
    this.post({ type: 'run', state });
    // A run that has ended has nothing queued against it. Posted rather than
    // only recorded, because the chip is drawn in the webview and a count kept
    // solely on this side leaves "2 queued" on screen after the run they were
    // queued for is over.
    if (state.phase === 'idle' && this.queued) {
      this.queued = 0;
      this.post({ type: 'queued', count: 0 });
    }
  }

  /**
   * Offline is a state, not an error: losing the gateway means the agent cannot
   * reach the model at all, by design, and the composer is disabled with one
   * sentence rather than left accepting input that is going to fail.
   */
  setOffline(reason?: string): void {
    this.offline = reason;
    this.post({ type: 'offline', ...(reason === undefined ? {} : { reason }) });
  }

  setQueued(count: number): void {
    this.queued = count;
    this.post({ type: 'queued', count });
  }

  note(level: 'info' | 'warn' | 'error', text: string): void {
    this.post({ type: 'notice', level, text });
  }

  /** The approval left the pending set — answered here, or answered elsewhere,
   *  or gone (410). The card stops offering buttons either way. */
  approvalResolved(id: string, decision: ApprovalDecision): void {
    this.post({ type: 'approval-resolved', id, decision });
  }

  /**
   * Show this conversation, clearing whatever was on screen if it is a different
   * one.
   *
   * Called before the transcript is replayed, so the panel is empty from the
   * moment of the switch — waiting for the first event would leave the previous
   * conversation on screen for a session that has no events yet, and would leave
   * it there for ever for one that never produces any.
   *
   * `seq` deliberately does not reset. It is the cursor a rebuilt webview
   * resumes from, and restarting it would make every event after a switch look
   * like one the panel had already applied.
   */
  showSession(id: string): void {
    if (id === this.session) return;
    this.session = id;
    // Dropped so a rebuilt webview is not replayed a conversation it is no
    // longer showing.
    this.ring.length = 0;
    // `session`, not `clear`. The webview decides whether anything needs
    // removing, because it is the side that knows what is on screen: a panel
    // showing nothing but the optimistic echo of the message that *started*
    // this conversation must keep it, and an unconditional clear from here
    // would wipe the sentence the developer had just typed.
    this.post({ type: 'session', id });
  }

  clear(): void {
    this.session = '';
    this.ring.length = 0;
    this.post({ type: 'clear' });
  }

  /** Whether anything at all has been pushed. Used to decide whether a rebuilt
   *  panel has a transcript to catch up on. */
  get hasHistory(): boolean {
    return this.ring.length > 0;
  }

  /** Only ever from an explicit command. A stream starting must not move focus
   *  out of the editor, which is the one focus rule that gets broken by accident. */
  focusComposer(): void {
    this.post({ type: 'focus' });
  }

  /**
   * Candidates for an `@` mention.
   *
   * Public because the mention surface is the panel's, not the caller's: the
   * webview asks over the message channel and the extension may ask directly.
   *
   * Symbol and package mentions resolve to a *reference* — the token text — and
   * never to file content. That is the whole economy of the feature: `@pkg:` is
   * tens of tokens where attaching the package's files is thousands, and the
   * server retrieves what it needs when it needs it.
   */
  async completions(kind: MentionKind, query: string): Promise<MentionItem[]> {
    switch (kind) {
      case 'file':
        return this.fileMentions(query);
      case 'diag':
        return this.diagnosticMentions();
      default: {
        // The guard is for the wiring that reads naturally and recurses
        // forever: `complete: (k, q) => chatView.completions(k, q)`.
        if (this.delegating) return [];
        this.delegating = true;
        try {
          return (await this.host.complete?.(kind, query)) ?? [];
        } finally {
          this.delegating = false;
        }
      }
    }
  }

  private async fileMentions(query: string): Promise<MentionItem[]> {
    // Glob metacharacters in a half-typed path would turn a search into a very
    // different search, so they are stripped rather than escaped.
    const safe = query.replace(/[*?[\]{}]/g, '');
    const pattern = safe ? `**/*${safe}*` : '**/*.go';
    const found = await vscode.workspace.findFiles(
      pattern,
      '**/{node_modules,.git,vendor,dist,bin}/**',
      20,
    );
    return found.map((uri) => {
      const relative = vscode.workspace.asRelativePath(uri, false);
      const slash = relative.lastIndexOf('/');
      return {
        insert: `@${relative}`,
        label: slash === -1 ? relative : relative.slice(slash + 1),
        detail: slash === -1 ? '' : relative.slice(0, slash),
      };
    });
  }

  private diagnosticMentions(): MentionItem[] {
    const editor = vscode.window.activeTextEditor;
    if (!editor) return [];
    const count = vscode.languages.getDiagnostics(editor.document.uri).length;
    const name = vscode.workspace.asRelativePath(editor.document.uri, false);
    return [
      {
        insert: '@diag',
        label: '@diag',
        detail:
          count === 1
            ? vscode.l10n.t('1 problem in {0}', name)
            : vscode.l10n.t('{0} problems in {1}', count, name),
      },
    ];
  }

  // ── WebviewViewProvider ───────────────────────────────────────────────────

  async resolveWebviewView(view: vscode.WebviewView): Promise<void> {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      // Nothing outside media/ is reachable, so a path bug cannot turn into a
      // read of the workspace through the webview origin.
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, 'media')],
    };
    view.webview.html = await this.html(view.webview);

    this.disposables.push(
      view.webview.onDidReceiveMessage((raw: unknown) => {
        void this.onMessage(raw);
      }),
    );
    view.onDidDispose(() => {
      if (this.view === view) this.view = undefined;
      // A queue with no webview to drain into would grow for the rest of the
      // session; the ring is what makes dropping it safe.
      this.queue.length = 0;
    });
  }

  dispose(): void {
    if (this.timer) clearTimeout(this.timer);
    for (const d of this.disposables) d.dispose();
    this.disposables.length = 0;
  }

  // ── inbound ───────────────────────────────────────────────────────────────

  private async onMessage(raw: unknown): Promise<void> {
    if (!isRecord(raw) || typeof raw['type'] !== 'string') return;
    const message = raw as Record<string, unknown> & { type: string };

    try {
      switch (message.type) {
        case 'ready':
          this.replay(numberOr(message['lastSeq'], 0));
          return;

        case 'submit': {
          const text = stringOr(message['text'], '');
          if (!text.trim()) return;
          await this.host.submit(text, message['steering'] === true);
          return;
        }

        case 'stop':
          this.host.stop();
          return;

        case 'wind-down':
          this.host.windDown();
          return;

        case 'decide': {
          const id = stringOr(message['id'], '');
          const decision = message['decision'];
          if (!id || (decision !== 'accept' && decision !== 'reject' && decision !== 'edit')) return;
          if (decision === 'edit') this.host.editArguments(id);
          else await this.host.decide(id, decision);
          return;
        }

        case 'show-diff':
          this.host.showDiff(stringOr(message['id'], ''));
          return;

        case 'slash': {
          const command = message['command'];
          if (!isSlashCommand(command)) return;
          this.host.runSlashCommand(command, stringOr(message['argument'], ''));
          return;
        }

        case 'complete': {
          const kind = message['kind'];
          if (!isMentionKind(kind)) return;
          const token = numberOr(message['token'], 0);
          const items = await this.completions(kind, stringOr(message['query'], ''));
          this.post({ type: 'mentions', token, items });
          return;
        }

        case 'open-path':
          this.host.openPath(stringOr(message['path'], ''));
          return;

        case 'open-in-editor':
          await this.openScratch(
            stringOr(message['content'], ''),
            stringOr(message['language'], 'plaintext'),
          );
          return;

        case 'copy':
          await vscode.env.clipboard.writeText(stringOr(message['text'], ''));
          return;

        default:
          // Additive in this direction too: a newer webview asset in a stale
          // host must degrade, not throw.
          return;
      }
    } catch (err) {
      this.log.warn(`chat: ${message.type} failed: ${String(err)}`);
      this.note('error', vscode.l10n.t('That action did not complete. See the dakcoder output for details.'));
    }
  }

  /**
   * Rebuild a torn-down webview from the events it has not seen.
   *
   * `init` is sent from here rather than from `resolveWebviewView` because a
   * message posted before the webview's own listener exists is dropped on the
   * floor, and a panel with no string bundle renders blank labels.
   *
   * The status, offline and queued lines are re-sent unconditionally: they are
   * current state rather than history, and a webview that restored its rows but
   * not its run state would show a Stop button for a run that finished while it
   * was hidden.
   */
  private replay(lastSeq: number): void {
    this.post(initMessage());
    this.post({ type: 'run', state: this.run });
    this.post({ type: 'offline', ...(this.offline === undefined ? {} : { reason: this.offline }) });
    if (this.queued) this.post({ type: 'queued', count: this.queued });
    for (const entry of this.ring) {
      if (entry.seq > lastSeq) {
        this.post({ type: 'event', event: entry.event, session: entry.session, seq: entry.seq });
      }
    }
  }

  private async openScratch(content: string, language: string): Promise<void> {
    const document = await vscode.workspace.openTextDocument({ content, language });
    // Beside, not in place: the developer opened this to read it next to the
    // work, and replacing their editor would be the panel taking the window.
    await vscode.window.showTextDocument(document, {
      preview: true,
      viewColumn: vscode.ViewColumn.Beside,
    });
  }

  // ── outbound, rate-capped ─────────────────────────────────────────────────

  private post(message: HostMessage): void {
    if (!this.view) return;
    this.queue.push(message);
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      this.flush();
    }, FLUSH_MS);
  }

  private flush(): void {
    if (!this.queue.length) return;
    const messages = this.queue.splice(0, this.queue.length);
    const view = this.view;
    if (!view) return;
    // One `postMessage` per flush whatever the queue depth, so the cap holds
    // under a burst as well as under a steady stream.
    void view.webview.postMessage(
      messages.length === 1 ? messages[0] : { type: 'batch', messages },
    );
  }

  private async html(webview: vscode.Webview): Promise<string> {
    const root = vscode.Uri.joinPath(this.extensionUri, 'media', 'chat');
    const bytes = await vscode.workspace.fs.readFile(vscode.Uri.joinPath(root, 'index.html'));
    const nonce = randomBytes(16).toString('base64');
    const uri = (name: string): string =>
      webview.asWebviewUri(vscode.Uri.joinPath(root, name)).toString();

    // Placeholders rather than a template literal: the HTML is a real file so it
    // can be linted, diffed and read as HTML.
    return new TextDecoder()
      .decode(bytes)
      .replace(/\{\{nonce\}\}/g, nonce)
      .replace(/\{\{csp\}\}/g, webview.cspSource)
      .replace(/\{\{style\}\}/g, uri('chat.css'))
      .replace(/\{\{script\}\}/g, uri('chat.js'))
      .replace(/\{\{lang\}\}/g, vscode.env.language || 'en');
  }
}

// ── registration ────────────────────────────────────────────────────────────

export function register(
  context: vscode.ExtensionContext,
  host: ChatHost,
  log: vscode.LogOutputChannel,
): ChatViewProvider {
  const provider = new ChatViewProvider(context.extensionUri, host, log);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(ChatViewProvider.viewType, provider, {
      // Stated rather than defaulted, because it is a decision: see the header.
      webviewOptions: { retainContextWhenHidden: false },
    }),
    provider,
  );
  return provider;
}

// ── the string bundle ───────────────────────────────────────────────────────

/**
 * Every user-visible string in the panel, resolved here so `vscode.l10n` sees
 * the literals. `{0}` placeholders survive `t()` untouched when no arguments
 * are passed and are substituted webview-side.
 *
 * Keys ending `_one` / `_other` are a plural pair the renderer selects between
 * with `n === 1`. There is no third form and no ICU selector, which is a real
 * limitation for languages that need one — a Hindi translation will want
 * revisiting here, not in `chat.js`.
 */
function strings(): Record<string, string> {
  return {
    // composer
    placeholder: vscode.l10n.t('Ask dakcoder to build, audit or fix something. / for commands, @ for context.'),
    placeholderRunning: vscode.l10n.t('Type a correction — it is read before the next turn.'),
    send: vscode.l10n.t('Send'),
    stop: vscode.l10n.t('Stop'),
    stopHint: vscode.l10n.t('Stop now and discard the turn in flight.'),
    windDown: vscode.l10n.t('Stop after this turn'),
    windDownHint: vscode.l10n.t('Let the turn in flight finish, then stop.'),
    offlineDefault: vscode.l10n.t('The agent needs the IT 2.0 gateway to reach the model. Reconnecting.'),
    working: vscode.l10n.t('Working'),
    workingTool: vscode.l10n.t('Working · {0}'),
    waitingDecision: vscode.l10n.t('Waiting on your decision'),
    popupKeys: vscode.l10n.t('↑↓ to choose · ⏎ to run'),

    // The offline card. Two sentences, and the second one is the load-bearing
    // half: "the stream dropped" and "the run died" look identical from here,
    // and only one of them is a reason to start the work again.
    offlineTitle: vscode.l10n.t('Offline'),
    offlineAside: vscode.l10n.t('The event stream dropped and is being retried. The run keeps going on the runtime.'),

    // The console row. Every one of these is a whole phrase rather than a
    // fragment plus a number, because a translator handed "turn" and "{0}"
    // separately cannot reorder them, and several languages need to.
    consoleTurn: vscode.l10n.t('turn {0}'),
    consoleAttempt: vscode.l10n.t('attempt {0}'),
    consoleBlocked: vscode.l10n.t('blocked: {0}'),
    // Replaces the mode cell while a decision is outstanding: "coding" is true
    // but useless when the run is blocked on a person.
    consoleNeedsApproval: vscode.l10n.t('needs approval'),
    'mode.planner': vscode.l10n.t('planning'),
    'mode.scaffolder': vscode.l10n.t('scaffolding'),
    'mode.coder': vscode.l10n.t('coding'),
    'mode.verifier': vscode.l10n.t('verifying'),
    'mode.debugger': vscode.l10n.t('debugging'),

    // The changeset verb, and why a path is protected. The reason names the
    // better tool where there is one: a developer who knows to run govalid_gen
    // does not need the linter to tell them afterwards.
    'kind.create': vscode.l10n.t('create'),
    'kind.modify': vscode.l10n.t('modify'),
    'kind.delete': vscode.l10n.t('delete'),
    protectedGenerated: vscode.l10n.t('generated — run govalid_gen instead'),
    protectedCredentials: vscode.l10n.t('holds credentials'),
    protectedSchema: vscode.l10n.t('schema — apply the migration yourself'),
    protectedStructural: vscode.l10n.t('structural'),
    elapsed: vscode.l10n.t('{0}s'),

    // structure
    transcript: vscode.l10n.t('Conversation transcript'),
    skipToApproval: vscode.l10n.t('Skip to the approval request'),
    suggestions: vscode.l10n.t('Suggestions'),

    // The idle panel. The title repeats the composer placeholder's first
    // sentence deliberately: the placeholder disappears the moment anyone
    // types, and this is the half that has to survive that.
    emptyTitle: vscode.l10n.t('Ask dakcoder to build, audit or fix something.'),
    emptySubtitle: vscode.l10n.t('Verified by the compiler and the template linter before you see the diff.'),
    you: vscode.l10n.t('You'),
    assistant: vscode.l10n.t('dakcoder'),
    show: vscode.l10n.t('Show detail'),
    hide: vscode.l10n.t('Hide detail'),
    openInEditor: vscode.l10n.t('Open in editor'),
    copy: vscode.l10n.t('Copy'),
    copied: vscode.l10n.t('Copied to the clipboard.'),
    truncated: vscode.l10n.t('Truncated by the runtime.'),

    // steering
    queuedChip: vscode.l10n.t('Queued — read before the next turn'),
    queued_one: vscode.l10n.t('1 correction queued'),
    queued_other: vscode.l10n.t('{0} corrections queued'),
    steerApplied: vscode.l10n.t('Correction picked up'),

    // turns
    turn: vscode.l10n.t('Turn {0} · {1}'),
    attempt: vscode.l10n.t('attempt {0}'),

    // tools
    toolRunning: vscode.l10n.t('running'),
    toolOk: vscode.l10n.t('ok'),
    toolFailed: vscode.l10n.t('failed'),
    ms: vscode.l10n.t('{0} ms'),
    lines_one: vscode.l10n.t('1 line'),
    lines_other: vscode.l10n.t('{0} lines'),
    files_one: vscode.l10n.t('1 file'),
    files_other: vscode.l10n.t('{0} files'),
    changeset: vscode.l10n.t('Changeset'),
    protectedPath: vscode.l10n.t('protected'),
    fixHint: vscode.l10n.t('Suggested fix: {0}'),

    // plan
    plan_one: vscode.l10n.t('Plan · 1 step'),
    plan_other: vscode.l10n.t('Plan · {0} steps'),
    planGoal: vscode.l10n.t('Goal'),
    planScope: vscode.l10n.t('Files in scope'),
    planAccepts: vscode.l10n.t('Accepts: {0}'),
    planStatusUnknown: vscode.l10n.t('—'),
    planFootnote: vscode.l10n.t('Per-step status is shown as a dash because no field on the wire carries it. The runtime reports the plan text and a step count; inferring progress from anything else would be a guess presented as a fact.'),

    // gate
    gateInner: vscode.l10n.t('Inner gate'),
    gateFull: vscode.l10n.t('Full gate'),
    gateStage: vscode.l10n.t('Stage'),
    gateAttemptCol: vscode.l10n.t('Attempt {0}'),
    gatePassed: vscode.l10n.t('✓ passed'),
    gateFailed: vscode.l10n.t('✗ failed'),
    gateSkipped: vscode.l10n.t('— skipped: {0}'),
    gateNotRun: vscode.l10n.t('— not run'),
    gateAbsent: vscode.l10n.t('·'),
    gateSeconds: vscode.l10n.t('{0}s'),
    gateBlocked: vscode.l10n.t('Blocked by {0}.'),
    // Says what the runtime decided and why it stopped, so a stalled run does
    // not read as a hung one. The turn count is not in the sentence because
    // the console row above already carries it.
    gateBlockedWhy: vscode.l10n.t('The gate blocked on {0}. The run is waiting rather than trying the same change again.'),
    gateConverged: vscode.l10n.t('Converged on attempt {0}.'),
    gateOpen: vscode.l10n.t('Not converged yet.'),

    // compaction
    compaction: vscode.l10n.t('Context compacted · {0} → {1} tokens'),

    // approval
    approvalTitle: vscode.l10n.t('Approval needed · {0}'),
    approvalReason: vscode.l10n.t('Reason'),
    approvalPaths: vscode.l10n.t('Paths'),
    approvalProtected: vscode.l10n.t('Protected path — the runtime never auto-approves this.'),
    approvalUnconditional: vscode.l10n.t('This tool always asks, whatever the approval policy says.'),
    accept: vscode.l10n.t('Accept'),
    reject: vscode.l10n.t('Reject'),
    showDiff: vscode.l10n.t('Show diff'),
    editArgs: vscode.l10n.t('Edit arguments'),
    decidedAccept: vscode.l10n.t('Accepted'),
    decidedReject: vscode.l10n.t('Rejected'),
    decidedEdit: vscode.l10n.t('Edited and accepted'),

    /*
     * `timeout` and `gone` both land here. They differ in why the runtime took
     * the approval back, but not in what happened to the change, and the
     * receipt says the part that matters: it was recorded as a rejection.
     *
     * The outcome word alone is enough for accept; the other three need the
     * sentence, because in none of them did the developer see the result they
     * would have predicted from the button they pressed.
     */
    decidedReleased: vscode.l10n.t('Released before it was answered'),
    receiptReleased: vscode.l10n.t('{0} was released by the runtime before it was answered, and recorded as a rejection.'),
    receiptEdited: vscode.l10n.t('Corrected arguments sent to {0}.'),
    receiptAuto: vscode.l10n.t('Approved automatically by the approval policy.'),
    approvalCountdown: vscode.l10n.t('{0} seconds left before the runtime releases it and records a rejection.'),

    // meter
    meterContext: vscode.l10n.t('{0} / {1} context'),
    meterReasoning: vscode.l10n.t('{0} reasoning'),
    meterCache: vscode.l10n.t('cache {0}%'),
    meterCacheUnknown: vscode.l10n.t('cache not reported'),
    meterLeaked: vscode.l10n.t('{0} reasoning charged in a thinking-off mode'),
    meterIdle: vscode.l10n.t('No usage reported yet.'),

    // finish
    finishRunning: vscode.l10n.t('Running'),
    finishDone: vscode.l10n.t('Done'),
    finishUnverified: vscode.l10n.t('Unverified — the gate did not pass'),
    finishNoProgress: vscode.l10n.t('Stopped — no progress'),
    finishExhausted: vscode.l10n.t('Stopped — turn budget exhausted'),
    finishAborted: vscode.l10n.t('Stopped'),
    finishError: vscode.l10n.t('Error'),
    turns_one: vscode.l10n.t('1 turn'),
    turns_other: vscode.l10n.t('{0} turns'),

    // misc rows
    error: vscode.l10n.t('Error'),
    quota: vscode.l10n.t('Quota · {0} {1}/{2}'),
    quotaClosest: vscode.l10n.t('Closest limit: {0} at {1}%.'),
    quotaResets: vscode.l10n.t('{0} left in this window.'),

    // Coarse by design: a window resetting in "2h 41m" does not need seconds,
    // and a figure that changes every tick invites watching it.
    hoursMinutes: vscode.l10n.t('{0}h {1}m'),
    seconds_one: vscode.l10n.t('1 second'),
    seconds_other: vscode.l10n.t('{0} seconds'),
    minutes_one: vscode.l10n.t('1 minute'),
    minutes_other: vscode.l10n.t('{0} minutes'),
    hours_one: vscode.l10n.t('1 hour'),
    hours_other: vscode.l10n.t('{0} hours'),

    // announcements (one composed sentence per event)
    sayTool: vscode.l10n.t('{0} {1}.'),
    sayApproval: vscode.l10n.t('Approval needed for {0}. A skip link to the request is the first item in this panel.'),
    sayFinish: vscode.l10n.t('Run finished: {0}.'),
    sayQueued: vscode.l10n.t('Correction queued. It is read before the next turn.'),
    sayGate: vscode.l10n.t('{0} attempt {1}: {2}.'),
    sayPlan: vscode.l10n.t('Plan received.'),
    sayError: vscode.l10n.t('Error: {0}'),
    sayOffline: vscode.l10n.t('Offline. {0}'),
    sayOnline: vscode.l10n.t('Back online.'),

    // popup copy
    hintReference: vscode.l10n.t('sends a reference, not the file contents'),
    cmdScaffold: vscode.l10n.t('Scaffold a resource from the n-api-template contract'),
    cmdService: vscode.l10n.t('Scaffold a new service'),
    cmdAudit: vscode.l10n.t('Audit template compliance'),
    cmdLegacy: vscode.l10n.t('Audit legacy patterns'),
    cmdMigrate: vscode.l10n.t('Migrate to n-api-template'),
    cmdDebug: vscode.l10n.t('Debug the last failure'),
    cmdExplain: vscode.l10n.t('Explain the code or rule in question'),
    cmdFix: vscode.l10n.t('Fix the selected diagnostic'),
    cmdTest: vscode.l10n.t('Write or run tests'),
    cmdWire: vscode.l10n.t('Register with FX'),
    cmdCompact: vscode.l10n.t('Compact the context now'),
    cmdRule: vscode.l10n.t('Explain a rule by id'),
    memFile: vscode.l10n.t('A workspace file, by path'),
    memSymbol: vscode.l10n.t('A Go symbol, found through gopls'),
    memPackage: vscode.l10n.t('A package API surface'),
    memBuild: vscode.l10n.t('The last go build output'),
    memDiag: vscode.l10n.t('Diagnostics for the active file'),
  };
}

function slashCommands(s: Record<string, string>): SlashSpec[] {
  return [
    { name: 'scaffold', hint: s['cmdScaffold']! },
    { name: 'service', hint: s['cmdService']! },
    { name: 'audit', hint: s['cmdAudit']! },
    { name: 'legacy', hint: s['cmdLegacy']! },
    { name: 'migrate', hint: s['cmdMigrate']! },
    { name: 'debug', hint: s['cmdDebug']! },
    { name: 'explain', hint: s['cmdExplain']! },
    { name: 'fix', hint: s['cmdFix']! },
    { name: 'test', hint: s['cmdTest']! },
    { name: 'wire', hint: s['cmdWire']! },
    { name: 'compact', hint: s['cmdCompact']! },
    { name: 'rule', hint: s['cmdRule']! },
  ];
}

function mentionSpecs(s: Record<string, string>): MentionSpec[] {
  return [
    { trigger: '@', kind: 'file', hint: s['memFile']!, reference: false },
    { trigger: '@#', kind: 'symbol', hint: s['memSymbol']!, reference: true },
    { trigger: '@pkg:', kind: 'package', hint: s['memPackage']!, reference: true },
    { trigger: '@build', kind: 'build', hint: s['memBuild']!, reference: false },
    { trigger: '@diag', kind: 'diag', hint: s['memDiag']!, reference: false },
  ];
}

/** The `init` payload, built once per webview resolve. */
function initMessage(): HostMessage {
  const s = strings();
  return {
    type: 'init',
    strings: s,
    maxRows: RING,
    commands: slashCommands(s),
    mentions: mentionSpecs(s),
  };
}

// ── narrow, defensively ─────────────────────────────────────────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback;
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

const SLASH: ReadonlySet<string> = new Set<SlashCommand>([
  'scaffold',
  'service',
  'audit',
  'legacy',
  'migrate',
  'debug',
  'explain',
  'fix',
  'test',
  'wire',
  'compact',
  'rule',
]);

function isSlashCommand(value: unknown): value is SlashCommand {
  return typeof value === 'string' && SLASH.has(value);
}

const MENTIONS: ReadonlySet<string> = new Set<MentionKind>([
  'file',
  'symbol',
  'package',
  'build',
  'diag',
]);

function isMentionKind(value: unknown): value is MentionKind {
  return typeof value === 'string' && MENTIONS.has(value);
}
