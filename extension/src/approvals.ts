/**
 * Approval and diff review.
 *
 * **The diff is a real diff.** The proposed content is served by a
 * `TextDocumentContentProvider` on `dakcoder-proposed:` and handed to
 * `vscode.diff`, so the developer reviews Go in the Go editor — syntax
 * highlighting, folding, find, inline navigation, screen-reader support. A
 * webview rendering coloured `<pre>` blocks looks similar and is none of those
 * things, and reviewing a 200-line handler through one is how a bad accept
 * happens. The relative path is kept *inside* the virtual URI so the language is
 * detected from the extension and the tab is named after the real file.
 *
 * **Protection is decided server-side and only explained here.** Every
 * `ApprovalEvent` carries `protected: string[]` (and every `Mutation` a
 * `protected: boolean`), computed against the runtime's own matcher. Nothing in
 * this file re-derives that decision: the matcher is custom, a second
 * implementation disagrees exactly at the edges that matter, and it would be a
 * security-relevant constant duplicated across the seam with no test binding the
 * copies. What this file adds is the *sentence* — why `go.mod` is different from
 * `configs/` is different from `*_validator.go` — chosen from the path's shape
 * and defaulting to a generic sentence when the shape is unfamiliar. Choosing
 * the wrong sentence is a cosmetic miss; choosing the wrong verdict is not.
 *
 * **What the wire does not carry, and what we do instead.**
 * - No proposed *content* for template-driven tools (`resource_scaffold`,
 *   `project_scaffold`): the files do not exist until the templates run, and the
 *   approval carries only the spec and the paths. There is no diff to fake, so
 *   the changeset lists the paths and points the review at the spec, which is
 *   the artefact that actually determines the output.
 * - No per-path `kind` on an approval — `Mutation.kind` only arrives afterwards
 *   on `tool_result`. The verb shown is derived from the tool's own contract
 *   (`write_file` creates, `patch_file` modifies, `delete_file` deletes) and is
 *   omitted for a tool we do not recognise.
 * - No deadline. Nothing on `ApprovalEvent` or `GET /v1/approvals` says when the
 *   runtime will release it; the only authoritative number in the system is
 *   `seconds_left` in the reply to `POST /v1/approvals/{id}/extend`. So the
 *   countdown before the first extension is a local estimate anchored on
 *   `dakcoder.approvalTimeoutSeconds` from the moment we first *saw* the
 *   approval — which is later than the moment the runtime minted it, so the
 *   estimate warns slightly early. That is the safe direction: a review that is
 *   silently converted into a rejection is the failure worth engineering
 *   against.
 *
 * **Additive-only (C2).** Every switch on a tool name has a default arm, an
 * approval for an unknown tool is reviewable (paths, reason, arguments, accept,
 * reject, edit) minus the preview we cannot compute, and unknown fields on the
 * wire are ignored rather than rejected.
 */

import * as path from 'node:path';
import * as vscode from 'vscode';

import { HttpError, type RuntimeClient } from './client';
import type { ApprovalEvent } from './protocol';

export const PROPOSED_SCHEME = 'dakcoder-proposed';
const CHANGESET_VIEW = 'dakcoder.changeset';

const CTX_PENDING = 'dakcoder.approvalPending';
const CTX_CHANGESET = 'dakcoder.changesetOpen';
const CTX_EDITING = 'dakcoder.editingArgs';

/** Cheap enough at rest — it only runs while something is actually waiting. */
const POLL_MS = 5_000;

/** Above this, correcting arguments belongs in an editor rather than a one-line box. */
const INPUT_BOX_LIMIT = 160;

/**
 * Digits 1–4 are reserved for the approval card **product-wide**.
 *
 * The reservation is the point, not the convenience: if `2` were Reject here and
 * "revert everything" somewhere else, the muscle memory a developer builds in
 * the first week becomes the mechanism of the accident in the second. No other
 * surface in this extension may bind a bare digit. Escape is deliberately absent
 * — dismissing a card must never be an answer, because "I pressed Escape" and "I
 * rejected the change" are different sentences and the runtime only hears one.
 */
export const APPROVAL_DIGITS: ReadonlyArray<{ key: string; command: string }> = [
  { key: '1', command: 'dakcoder.approval.accept' },
  { key: '2', command: 'dakcoder.approval.reject' },
  { key: '3', command: 'dakcoder.approval.showDiff' },
  { key: '4', command: 'dakcoder.approval.editArgs' },
];

export type Decision = 'accept' | 'reject' | 'edit';

/** `timeout`/`gone` are outcomes the developer did not choose; they are reported, never inferred silently. */
export type Resolution = Decision | 'timeout' | 'gone';

export interface ApprovalOutcome {
  id: string;
  tool: string;
  decision: Resolution;
  /** True when policy answered without asking. The chat card says so. */
  auto: boolean;
  /** Present only for `edit`: the receipt, so the transcript can show it. */
  arguments?: Record<string, unknown>;
}

export interface Pending {
  approval: ApprovalEvent;
  /** Not on the wire — `ApprovalEvent` has no session id. Supplied by the caller. */
  sessionId?: string;
  seenAt: number;
  /** Local estimate; absent when `approvalTimeoutSeconds` is 0 (the default). */
  deadline?: number;
  warned: boolean;
  extensions: number;
  /** Per-file review state. A reviewer's mark, never a partial answer — see `accept`. */
  unchecked: Set<string>;
  paths: string[];
}

export interface ApprovalDeps {
  client: RuntimeClient;
  log: vscode.LogOutputChannel;
  /** The folder the runtime was spawned against; approval paths are relative to it. */
  workspaceRoot: () => vscode.Uri | undefined;
}

export function register(context: vscode.ExtensionContext, deps: ApprovalDeps): ApprovalService {
  const service = new ApprovalService(context, deps);
  context.subscriptions.push(service);
  return service;
}

export class ApprovalService implements vscode.Disposable {
  private readonly pending = new Map<string, Pending>();
  /** Corrected arguments, kept after the decision so the receipt survives the card. */
  private readonly receipts = new Map<string, string>();
  private readonly answered = new Set<string>();
  private readonly disposables: vscode.Disposable[] = [];

  private readonly changed = new vscode.EventEmitter<void>();
  private readonly resolved = new vscode.EventEmitter<ApprovalOutcome>();
  readonly contentChanged = new vscode.EventEmitter<vscode.Uri>();
  readonly decorationsChanged = new vscode.EventEmitter<vscode.Uri[]>();

  private readonly tree: vscode.TreeView<ChangesetNode>;
  private readonly nodes: ChangesetTree;
  private poll?: ReturnType<typeof setInterval>;
  private pollFailing = false;
  /** The approval the changeset view is currently showing. */
  private shown?: string;
  private editing?: { id: string; file: vscode.Uri };

  /** Fires whenever the pending set changes, so a chat card can re-render. */
  readonly onDidChange = this.changed.event;
  readonly onDidResolve = this.resolved.event;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly deps: ApprovalDeps,
  ) {
    this.nodes = new ChangesetTree(this);
    this.tree = vscode.window.createTreeView<ChangesetNode>(CHANGESET_VIEW, {
      treeDataProvider: this.nodes,
      // Manual, because a checkbox here is a review mark and must not imply a
      // parent/child rollup the wire cannot express.
      manageCheckboxStateManually: true,
      showCollapseAll: false,
    });

    this.disposables.push(
      this.tree,
      this.changed,
      this.resolved,
      this.contentChanged,
      this.decorationsChanged,
      vscode.workspace.registerTextDocumentContentProvider(PROPOSED_SCHEME, new ProposedContent(this)),
      vscode.window.registerFileDecorationProvider(new ProtectedBadges(this)),
      this.tree.onDidChangeCheckboxState((e) => this.onCheckbox(e)),
      cmd('dakcoder.approval.accept', (arg?: unknown) => this.run(arg, (p) => this.accept(p))),
      cmd('dakcoder.approval.reject', (arg?: unknown) => this.run(arg, (p) => this.reject(p))),
      cmd('dakcoder.approval.showDiff', (arg?: unknown) => this.run(arg, (p, rel) => this.showDiff(p, rel))),
      cmd('dakcoder.approval.editArgs', (arg?: unknown) => this.run(arg, (p) => this.editArgs(p))),
      cmd('dakcoder.approval.extend', (arg?: unknown) => this.run(arg, (p) => this.extend(p))),
      cmd('dakcoder.approval.openChangeset', (arg?: unknown) => this.run(arg, (p) => this.openChangeset(p))),
      cmd('dakcoder.approval.openAllDiffs', (arg?: unknown) => this.run(arg, (p) => this.openAllDiffs(p))),
      // Not routed through `run`: a receipt outlives the approval it belongs to,
      // so resolving "which pending approval did you mean" would find the wrong
      // one or nothing at all. It takes the id it was minted with.
      cmd('dakcoder.approval.showReceipt', (arg?: unknown) =>
        typeof arg === 'string' ? this.showReceipt(arg) : undefined,
      ),
      cmd('dakcoder.approval.submitArgs', () => this.submitArgs()),
      cmd('dakcoder.approval.cancelArgs', () => this.cancelArgs()),
    );
  }

  // ── the surface the session/chat module drives ────────────────────────────

  get all(): readonly ApprovalEvent[] {
    return [...this.pending.values()].map((p) => p.approval);
  }

  get(id: string): Pending | undefined {
    return this.pending.get(id);
  }

  receipt(id: string): string | undefined {
    return this.receipts.get(id);
  }

  /**
   * Take an approval off a `tool_pending` event.
   *
   * Nothing here steals focus or opens an editor. The approval arrives while the
   * developer is reading something else, and a diff that seizes the editor
   * mid-thought is how a "Show Diff" button gets clicked without being read. The
   * card, the tree badge and the timeout warning are the notification; opening
   * anything is the developer's move.
   */
  present(approval: ApprovalEvent, sessionId?: string): void {
    if (this.pending.has(approval.id) || this.answered.has(approval.id)) return;

    const paths = this.pathsOf(approval);
    const timeout = config().get<number>('approvalTimeoutSeconds', 0);
    const pending: Pending = {
      approval,
      ...(sessionId ? { sessionId } : {}),
      seenAt: Date.now(),
      // 0 means "wait indefinitely", which is the default and stays the default.
      ...(timeout > 0 ? { deadline: Date.now() + timeout * 1000 } : {}),
      warned: false,
      extensions: 0,
      unchecked: new Set<string>(),
      paths,
    };
    this.pending.set(approval.id, pending);
    this.deps.log.info(
      `approval ${approval.id} for ${approval.tool}: ${paths.length} path(s), ` +
        `${approval.protected.length} protected, unconditional=${approval.unconditional}`,
    );

    const auto = this.autoDecision(pending);
    if (auto) {
      void this.decide(pending, auto, undefined, true);
      return;
    }

    if (paths.length > 1) this.setChangeset(pending);
    this.refresh();
    this.ensurePolling();
  }

  /** The card's Accept. */
  async accept(pending: Pending): Promise<void> {
    if (pending.unchecked.size > 0) {
      // The wire has no partial accept: `POST /v1/approvals/{id}` answers the
      // *call*, and the call writes every file or none. Silently accepting all
      // seven after the developer unticked one would be the worst outcome here,
      // so the unticked files are named and the three answers that actually
      // exist are offered.
      const names = [...pending.unchecked].map((rel) => path.basename(rel)).join(', ');
      const accept = vscode.l10n.t('Accept everything');
      const edit = vscode.l10n.t('Edit arguments');
      const answer = await vscode.window.showWarningMessage(
        pending.unchecked.size === 1
          ? vscode.l10n.t('This is one call: the runtime writes every file or none. You left 1 file unticked ({0}).', names)
          : vscode.l10n.t('This is one call: the runtime writes every file or none. You left {0} files unticked ({1}).', pending.unchecked.size, names),
        { modal: true },
        accept,
        edit,
      );
      if (answer === edit) return this.editArgs(pending);
      if (answer !== accept) return;
    }
    await this.decide(pending, 'accept');
  }

  async reject(pending: Pending): Promise<void> {
    const unchecked = [...pending.unchecked];
    await this.decide(pending, 'reject');

    // A rejection carries no reason on the wire — `decide` takes a decision and
    // optional arguments, nothing else. Without a follow-up the agent retries
    // the same call, because nothing told it what was wrong. Steering is the
    // channel that exists, so offer it while the reason is still in the
    // developer's head.
    if (!pending.sessionId || unchecked.length === 0) return;
    const tell = vscode.l10n.t('Tell the agent why');
    const answer = await vscode.window.showInformationMessage(
      unchecked.length === 1
        ? vscode.l10n.t('Rejected. 1 file was unticked in the review.')
        : vscode.l10n.t('Rejected. {0} files were unticked in the review.', unchecked.length),
      tell,
    );
    if (answer !== tell) return;
    const note = await vscode.window.showInputBox({
      title: vscode.l10n.t('What was wrong with these files?'),
      value: vscode.l10n.t('Do not change these files: {0}', unchecked.join(', ')),
      ignoreFocusOut: true,
    });
    if (!note) return;
    try {
      await this.deps.client.steer(pending.sessionId, note);
    } catch (err) {
      void vscode.window.showWarningMessage(
        vscode.l10n.t('The note could not be queued: {0}', message(err)),
      );
    }
  }

  /**
   * Open the native diff for one file.
   *
   * The left side is the file on disk when it exists, so "open the real file" and
   * the editor's own gutter actions keep working; when the tool creates the file
   * there is nothing on disk, and an empty document from our own scheme is the
   * honest left-hand side rather than a phantom `file:` URI that opens as an
   * error.
   */
  async showDiff(pending: Pending, rel?: string): Promise<void> {
    const target = rel ?? pending.paths[0];
    if (!target) {
      void vscode.window.showInformationMessage(
        vscode.l10n.t('This approval names no files. Review the arguments instead.'),
      );
      return this.editArgs(pending);
    }
    if (pending.paths.length > 1 && !rel) return this.openChangeset(pending);

    if (!canPreview(pending.approval.tool)) {
      // Not a failure and not a gap to paper over: for a template-driven tool
      // the content does not exist yet, and the spec is the thing that decides
      // what it will be. Point the review at the spec.
      const review = vscode.l10n.t('Review the arguments');
      const list = vscode.l10n.t('List the files');
      const answer = await vscode.window.showInformationMessage(
        vscode.l10n.t(
          '{0} produces its files from templates when it runs, so there is no proposed content to diff yet. The arguments decide the output.',
          pending.approval.tool,
        ),
        review,
        list,
      );
      if (answer === review) return this.editArgs(pending);
      if (answer === list) return this.openChangeset(pending);
      return;
    }

    try {
      await this.proposedText(pending, target);
    } catch (err) {
      // A patch whose anchor is not in the file is worth catching *before* the
      // diff opens: the runtime will refuse the call, and "correct it now" is a
      // better answer than watching it fail a turn later.
      const edit = vscode.l10n.t('Edit arguments');
      const answer = await vscode.window.showWarningMessage(message(err), edit, vscode.l10n.t('Reject'));
      if (answer === edit) return this.editArgs(pending);
      if (answer === vscode.l10n.t('Reject')) return this.reject(pending);
      return;
    }

    const left = (await this.exists(target))
      ? this.fileUri(target)
      : proposedUri(pending.approval.id, 'original', target);
    const right = proposedUri(pending.approval.id, 'proposed', target);
    await vscode.commands.executeCommand('vscode.diff', left, right, this.diffTitle(pending, target), {
      preview: true,
    } satisfies vscode.TextDocumentShowOptions);
  }

  /** One reviewable list for one logical action — seven cards in sequence is the wrong ceremony. */
  async openChangeset(pending: Pending): Promise<void> {
    this.setChangeset(pending);
    // `focus: false`: the view reveals itself, the cursor stays where the
    // developer left it.
    const first = this.nodes.nodesFor(pending)[0];
    if (first) {
      try {
        await this.tree.reveal(first, { select: true, focus: false, expand: false });
      } catch {
        /* the view may not be visible yet; the badge still says there is work */
      }
    }
  }

  /**
   * All files at once in the multi-diff editor.
   *
   * `vscode.changes` is a built-in command rather than API, so a build that does
   * not have it must not take the review down with it — the tree is the fallback
   * and it is the durable surface anyway.
   */
  async openAllDiffs(pending: Pending): Promise<void> {
    if (!canPreview(pending.approval.tool)) return this.showDiff(pending);
    const resources: [vscode.Uri, vscode.Uri, vscode.Uri][] = [];
    for (const rel of pending.paths) {
      const left = (await this.exists(rel))
        ? this.fileUri(rel)
        : proposedUri(pending.approval.id, 'original', rel);
      resources.push([this.fileUri(rel), left, proposedUri(pending.approval.id, 'proposed', rel)]);
    }
    const title =
      resources.length === 1
        ? vscode.l10n.t('dakcoder proposes 1 file')
        : vscode.l10n.t('dakcoder proposes {0} files', resources.length);
    try {
      await vscode.commands.executeCommand('vscode.changes', title, resources);
    } catch (err) {
      this.deps.log.warn(`the multi-diff editor refused: ${message(err)}`);
      await this.openChangeset(pending);
    }
  }

  /**
   * Correct the agent instead of rejecting it.
   *
   * Rejecting and re-prompting usually costs a turn and produces the same
   * mistake, because the model has no new information. An edited argument is new
   * information, and it keeps the developer in the loop without spending a turn.
   */
  async editArgs(pending: Pending): Promise<void> {
    const pretty = JSON.stringify(pending.approval.arguments ?? {}, null, 2);
    const compact = JSON.stringify(pending.approval.arguments ?? {});
    const long = compact.length > INPUT_BOX_LIMIT || /\\n/.test(compact);

    if (!long) {
      const edited = await vscode.window.showInputBox({
        title: vscode.l10n.t('Correct the arguments for {0}', pending.approval.tool),
        value: compact,
        ignoreFocusOut: true,
        validateInput: (text) => (parseArgs(text) ? undefined : vscode.l10n.t('This is not a JSON object.')),
      });
      const parsed = edited ? parseArgs(edited) : undefined;
      if (parsed) await this.decide(pending, 'edit', parsed);
      return;
    }

    // Multi-line arguments (a file's content, a patch body, a scaffold spec) do
    // not fit an input box, and truncating them to make them fit would be worse
    // than useless. A real editor gets a real JSON editor: folding, find,
    // bracket matching, and a tab title that names the tool.
    const dir = vscode.Uri.joinPath(this.context.globalStorageUri, 'approval-args');
    await vscode.workspace.fs.createDirectory(dir);
    const file = vscode.Uri.joinPath(dir, `${pending.approval.tool}.${pending.approval.id}.args.json`);
    await vscode.workspace.fs.writeFile(file, new TextEncoder().encode(pretty));
    this.editing = { id: pending.approval.id, file };
    await vscode.commands.executeCommand('setContext', CTX_EDITING, true);

    const doc = await vscode.workspace.openTextDocument(file);
    await vscode.window.showTextDocument(doc, { preview: false });
    const submit = vscode.l10n.t('Send corrected arguments');
    const answer = await vscode.window.showInformationMessage(
      vscode.l10n.t('Edit the arguments, then send them back to the agent.'),
      submit,
      vscode.l10n.t('Cancel'),
    );
    if (answer === submit) await this.submitArgs();
    else if (answer) await this.cancelArgs();
  }

  private async submitArgs(): Promise<void> {
    const editing = this.editing;
    if (!editing) return;
    const pending = this.pending.get(editing.id);
    if (!pending) {
      await this.cancelArgs();
      return;
    }
    // Read the *document*, not the file: an unsaved buffer is the developer's
    // real intent, and demanding a save first is a step that exists only because
    // the implementation wanted one.
    const open = vscode.workspace.textDocuments.find((d) => d.uri.fsPath === editing.file.fsPath);
    const text = open ? open.getText() : new TextDecoder().decode(await vscode.workspace.fs.readFile(editing.file));
    const parsed = parseArgs(text);
    if (!parsed) {
      void vscode.window.showErrorMessage(
        vscode.l10n.t('The corrected arguments are not a JSON object, so they were not sent.'),
      );
      return;
    }
    await this.closeArgsEditor();
    await this.decide(pending, 'edit', parsed);
  }

  private async cancelArgs(): Promise<void> {
    await this.closeArgsEditor();
  }

  private async closeArgsEditor(): Promise<void> {
    const editing = this.editing;
    this.editing = undefined;
    await vscode.commands.executeCommand('setContext', CTX_EDITING, false);
    if (!editing) return;
    for (const group of vscode.window.tabGroups.all) {
      for (const tab of group.tabs) {
        const input: unknown = tab.input;
        if (input instanceof vscode.TabInputText && input.uri.fsPath === editing.file.fsPath) {
          await vscode.window.tabGroups.close(tab, true);
        }
      }
    }
    try {
      await vscode.workspace.fs.delete(editing.file);
    } catch {
      /* a scratch file that outlives the edit is harmless */
    }
  }

  /** Give the reviewer more time. The one call whose answer carries a real clock. */
  async extend(pending: Pending): Promise<void> {
    try {
      const { seconds_left, extensions } = await this.deps.client.extendApproval(pending.approval.id);
      // Re-anchor on the server's number: from here the countdown is measured,
      // not estimated.
      pending.deadline = Date.now() + seconds_left * 1000;
      pending.warned = false;
      pending.extensions = extensions;
      this.deps.log.info(`approval ${pending.approval.id} extended: ${seconds_left}s left, ${extensions} extension(s)`);
      void vscode.window.setStatusBarMessage(
        vscode.l10n.t('dakcoder: {0} more seconds to review.', Math.round(seconds_left)),
        5_000,
      );
    } catch (err) {
      if (err instanceof HttpError && err.isGone) return this.gone(pending);
      void vscode.window.showWarningMessage(
        vscode.l10n.t('More time could not be granted: {0}', message(err)),
      );
    }
  }

  async showReceipt(id: string): Promise<void> {
    if (!this.receipts.has(id)) return;
    const doc = await vscode.workspace.openTextDocument(receiptUri(id));
    await vscode.window.showTextDocument(doc, { preview: true, preserveFocus: true });
  }

  // ── the decision ──────────────────────────────────────────────────────────

  private async decide(
    pending: Pending,
    decision: Decision,
    args?: Record<string, unknown>,
    auto = false,
  ): Promise<void> {
    const { id, tool } = pending.approval;
    // Remove first: the poll must not see a decided approval still in our map
    // and report it as a timeout, and a double-click must not send two answers.
    this.forget(id);
    try {
      await this.deps.client.decide(id, decision, args);
    } catch (err) {
      if (err instanceof HttpError && err.isGone) return this.gone(pending);
      // Put it back — an approval that failed to send is still waiting, and
      // dropping the card here would leave the run blocked with nothing on
      // screen to answer.
      this.pending.set(id, pending);
      this.refresh();
      void vscode.window.showErrorMessage(
        vscode.l10n.t('The decision could not be sent: {0}', message(err)),
      );
      return;
    }

    this.deps.log.info(`approval ${id} (${tool}) → ${decision}${auto ? ' (policy)' : ''}`);
    if (decision === 'edit' && args) this.keepReceipt(id, tool, args);
    this.resolved.fire({ id, tool, decision, auto, ...(args ? { arguments: args } : {}) });

    if (decision === 'edit' && args) {
      const show = vscode.l10n.t('Show receipt');
      void vscode.window
        .showInformationMessage(vscode.l10n.t('Corrected arguments sent to {0}.', tool), show)
        .then((answer) => (answer === show ? this.showReceipt(id) : undefined));
    }
  }

  /**
   * The runtime already let go of this one.
   *
   * 410 means answered, timed out, or the run ended. The extension cannot tell
   * which, so it says exactly that and names the consequence it knows: an
   * unanswered approval is recorded as a rejection.
   */
  private gone(pending: Pending): void {
    const { id, tool } = pending.approval;
    this.forget(id);
    this.resolved.fire({ id, tool, decision: 'gone', auto: false });
    void vscode.window.showWarningMessage(
      vscode.l10n.t('The runtime is no longer waiting on {0}. An unanswered approval is recorded as a rejection.', tool),
    );
  }

  // ── policy ────────────────────────────────────────────────────────────────

  /**
   * Whether policy may answer without asking.
   *
   * Deliberately narrow. `requireApproval`'s `write_side`, `destructive` and
   * tool-list values classify *tools*, and that classification lives in the
   * runtime — it is what decides whether an approval is raised at all. Second-
   * guessing it here would mean two copies of a policy with no test binding
   * them, so for those values the extension answers nothing and the runtime's
   * own decision to ask stands.
   */
  private autoDecision(pending: Pending): Decision | undefined {
    const { approval } = pending;
    // `unconditional` is the runtime saying this class of call is never waivable
    // (a delete, a scaffold). Policy does not get a vote.
    if (approval.unconditional) return undefined;
    // The server's verdict, not ours.
    if (approval.protected.length > 0) return undefined;
    // Belt and braces, and deliberately cruder than the server's matcher: a
    // literal `configs` path segment. This can only ever refuse *more* than the
    // server does, never less, so it cannot disagree in the dangerous direction.
    // Nothing under configs/** is auto-approved regardless of size, because
    // those files hold credentials.
    if (pending.paths.some(inConfigs)) return undefined;

    const policy = config().get<string | string[]>('requireApproval', 'write_side');
    if (policy === 'all') return undefined;
    if (policy === 'none') return 'accept';
    if (config().get<boolean>('autoApproveTrivialPatches', false) && isTrivialPatch(approval)) return 'accept';
    return undefined;
  }

  // ── the timeout watch ─────────────────────────────────────────────────────

  private ensurePolling(): void {
    if (this.poll || this.pending.size === 0) return;
    this.poll = setInterval(() => void this.tick(), POLL_MS);
  }

  private stopPolling(): void {
    if (!this.poll) return;
    clearInterval(this.poll);
    this.poll = undefined;
  }

  private async tick(): Promise<void> {
    if (this.pending.size === 0) {
      this.stopPolling();
      return;
    }
    await this.reconcile();
    this.checkDeadlines();
  }

  /** What the runtime still holds, versus what we still show. */
  private async reconcile(): Promise<void> {
    let live: ApprovalEvent[];
    try {
      const body = await this.deps.client.approvals();
      live = (body.approvals ?? []).map(asApproval).filter((a): a is ApprovalEvent => !!a);
      this.pollFailing = false;
    } catch (err) {
      // The runtime restarting mid-review is a normal event, and a toast every
      // five seconds would be worse than the outage. Log the transition only.
      if (!this.pollFailing) {
        this.pollFailing = true;
        this.deps.log.warn(`pending approvals could not be polled: ${message(err)}`);
      }
      return;
    }

    const ids = new Set(live.map((a) => a.id));
    for (const pending of [...this.pending.values()]) {
      if (ids.has(pending.approval.id)) continue;
      const { id, tool } = pending.approval;
      this.forget(id);
      this.resolved.fire({ id, tool, decision: 'timeout', auto: false });
      void vscode.window.showWarningMessage(
        vscode.l10n.t('{0} was released by the runtime before it was answered, and recorded as a rejection.', tool),
      );
    }

    // Approvals raised before this window was listening — after a reload, or a
    // second window attaching to the same runtime. Without this they wait for a
    // reviewer who cannot see them.
    for (const approval of live) this.present(approval);
  }

  private checkDeadlines(): void {
    for (const pending of this.pending.values()) {
      if (!pending.deadline || pending.warned) continue;
      const remaining = Math.round((pending.deadline - Date.now()) / 1000);
      if (remaining > warnAt(pending)) continue;
      pending.warned = true;
      const more = vscode.l10n.t('Give me more time');
      const review = vscode.l10n.t('Review now');
      void vscode.window
        .showWarningMessage(
          remaining === 1
            ? vscode.l10n.t('{0} has about 1 second left before the runtime releases it and records a rejection.', pending.approval.tool)
            : vscode.l10n.t('{0} has about {1} seconds left before the runtime releases it and records a rejection.', pending.approval.tool, Math.max(0, remaining)),
          more,
          review,
        )
        .then((answer) => {
          if (answer === more) return this.extend(pending);
          if (answer === review) return this.showDiff(pending);
          return undefined;
        });
    }
  }

  // ── content ───────────────────────────────────────────────────────────────

  /**
   * The proposed text for one path.
   *
   * Throws a sentence a developer can act on rather than returning something
   * plausible: a preview that quietly shows "no changes" because the patch
   * anchor was missing is a lie with a diff editor around it.
   */
  async proposedText(pending: Pending, rel: string): Promise<string> {
    const args = (pending.approval.arguments ?? {}) as Record<string, unknown>;
    switch (pending.approval.tool) {
      case 'write_file':
        return typeof args.content === 'string' ? args.content : '';
      case 'delete_file':
        // Every line removed, which is what a delete is. The left-hand side
        // carries the file the developer is about to lose.
        return '';
      case 'patch_file': {
        const original = await this.diskText(rel);
        const old = typeof args.old === 'string' ? args.old : '';
        const replacement = typeof args.new === 'string' ? args.new : '';
        if (!old) throw new Error(vscode.l10n.t('This patch has no text to replace, so it cannot be previewed.'));
        const first = original.indexOf(old);
        if (first === -1) {
          throw new Error(
            vscode.l10n.t('The text this patch replaces is not in {0} on disk. The runtime will refuse this call rather than guess.', rel),
          );
        }
        const second = original.indexOf(old, first + old.length);
        if (second !== -1) {
          throw new Error(
            vscode.l10n.t('The text this patch replaces appears more than once in {0}. The runtime will refuse this call rather than guess.', rel),
          );
        }
        return original.slice(0, first) + replacement + original.slice(first + old.length);
      }
      default:
        // Additive-only: an unknown mutating tool is reviewable through its
        // arguments and its paths, just not previewable.
        throw new Error(
          vscode.l10n.t('{0} does not send proposed content with its approval, so there is nothing to diff.', pending.approval.tool),
        );
    }
  }

  async diskText(rel: string): Promise<string> {
    try {
      return new TextDecoder().decode(await vscode.workspace.fs.readFile(this.fileUri(rel)));
    } catch {
      return '';
    }
  }

  private async exists(rel: string): Promise<boolean> {
    try {
      await vscode.workspace.fs.stat(this.fileUri(rel));
      return true;
    } catch {
      return false;
    }
  }

  fileUri(rel: string): vscode.Uri {
    const root = this.deps.workspaceRoot();
    if (path.isAbsolute(rel)) return vscode.Uri.file(rel);
    return root ? vscode.Uri.joinPath(root, ...rel.split('/')) : vscode.Uri.file(rel);
  }

  private diffTitle(pending: Pending, rel: string): string {
    const name = path.basename(rel);
    const isProtected = pending.approval.protected.includes(rel);
    // The tab title has to be unmistakable at a glance in a row of eight tabs:
    // the file, who is proposing, which tool, and the one word that changes how
    // carefully this gets read.
    return isProtected
      ? vscode.l10n.t('{0} — dakcoder proposes ({1}) · protected', name, pending.approval.tool)
      : vscode.l10n.t('{0} — dakcoder proposes ({1})', name, pending.approval.tool);
  }

  // ── bookkeeping ───────────────────────────────────────────────────────────

  private pathsOf(approval: ApprovalEvent): string[] {
    const listed = (approval.paths ?? []).filter((p) => typeof p === 'string');
    if (listed.length > 0) return listed.map(toPosix);
    const args = (approval.arguments ?? {}) as Record<string, unknown>;
    return typeof args.path === 'string' ? [toPosix(args.path)] : [];
  }

  private keepReceipt(id: string, tool: string, args: Record<string, unknown>): void {
    this.receipts.set(id, `// ${tool} — corrected arguments\n${JSON.stringify(args, null, 2)}\n`);
    // Bounded: a long session must not accumulate every correction forever.
    while (this.receipts.size > 20) {
      const oldest = this.receipts.keys().next();
      if (oldest.done) break;
      this.receipts.delete(oldest.value);
    }
    this.contentChanged.fire(receiptUri(id));
  }

  private forget(id: string): void {
    const pending = this.pending.get(id);
    this.pending.delete(id);
    this.answered.add(id);
    if (this.shown === id) {
      this.shown = undefined;
      void vscode.commands.executeCommand('setContext', CTX_CHANGESET, false);
    }
    if (pending) this.decorationsChanged.fire(pending.paths.map((rel) => proposedUri(id, 'proposed', rel)));
    if (this.pending.size === 0) this.stopPolling();
    this.refresh();
  }

  private setChangeset(pending: Pending): void {
    this.shown = pending.approval.id;
    void vscode.commands.executeCommand('setContext', CTX_CHANGESET, true);
    this.nodes.refresh();
    this.tree.title = vscode.l10n.t('Proposed changes — {0}', pending.approval.tool);
    this.tree.badge = {
      value: pending.paths.length,
      tooltip:
        pending.paths.length === 1
          ? vscode.l10n.t('1 file waiting for review')
          : vscode.l10n.t('{0} files waiting for review', pending.paths.length),
    };
  }

  get shownApproval(): Pending | undefined {
    return this.shown ? this.pending.get(this.shown) : undefined;
  }

  private refresh(): void {
    void vscode.commands.executeCommand('setContext', CTX_PENDING, this.pending.size > 0);
    this.nodes.refresh();
    this.changed.fire();
  }

  private onCheckbox(e: vscode.TreeCheckboxChangeEvent<ChangesetNode>): void {
    for (const [node, state] of e.items) {
      const pending = this.pending.get(node.approvalId);
      if (!pending) continue;
      if (state === vscode.TreeItemCheckboxState.Checked) pending.unchecked.delete(node.rel);
      else pending.unchecked.add(node.rel);
    }
    this.changed.fire();
  }

  /**
   * Resolve whatever a command was invoked with.
   *
   * The same four commands are reached from the diff editor's title bar, the
   * changeset tree, a chat card and a keybinding, and each hands over something
   * different — a node, a URI, an id, or nothing at all. Keyboard reach is the
   * requirement that makes the "nothing at all" case matter: from the palette
   * there are no arguments, and a command that does nothing there is a command
   * that is not keyboard-reachable.
   */
  private async run(arg: unknown, fn: (pending: Pending, rel?: string) => Promise<void> | void): Promise<void> {
    const target = await this.resolve(arg);
    if (!target) return;
    await fn(target.pending, target.rel);
  }

  private async resolve(arg: unknown): Promise<{ pending: Pending; rel?: string } | undefined> {
    if (arg instanceof ChangesetNode) {
      const pending = this.pending.get(arg.approvalId);
      return pending ? { pending, rel: arg.rel } : undefined;
    }
    if (typeof arg === 'string') {
      const pending = this.pending.get(arg);
      if (pending) return { pending };
    }
    if (arg instanceof vscode.Uri) {
      const parsed = parseProposed(arg);
      const pending = parsed ? this.pending.get(parsed.id) : undefined;
      if (pending && parsed) return { pending, rel: parsed.rel };
    }

    const active = vscode.window.activeTextEditor?.document.uri;
    const fromEditor = active ? parseProposed(active) : undefined;
    if (fromEditor) {
      const pending = this.pending.get(fromEditor.id);
      if (pending) return { pending, rel: fromEditor.rel };
    }
    if (this.shown) {
      const pending = this.pending.get(this.shown);
      if (pending) return { pending };
    }
    if (this.pending.size === 1) {
      const only = this.pending.values().next();
      return only.done ? undefined : { pending: only.value };
    }
    if (this.pending.size === 0) {
      void vscode.window.showInformationMessage(vscode.l10n.t('Nothing is waiting for approval.'));
      return undefined;
    }

    const picked = await vscode.window.showQuickPick(
      [...this.pending.values()].map((pending) => ({
        label: pending.approval.tool,
        description: pending.paths.join(', '),
        detail: pending.approval.reason,
        id: pending.approval.id,
      })),
      { title: vscode.l10n.t('Which approval?'), matchOnDescription: true },
    );
    const pending = picked ? this.pending.get(picked.id) : undefined;
    return pending ? { pending } : undefined;
  }

  dispose(): void {
    this.stopPolling();
    for (const d of this.disposables) d.dispose();
    void vscode.commands.executeCommand('setContext', CTX_PENDING, false);
    void vscode.commands.executeCommand('setContext', CTX_CHANGESET, false);
    void vscode.commands.executeCommand('setContext', CTX_EDITING, false);
  }
}

// ── the virtual documents ───────────────────────────────────────────────────

type Side = 'proposed' | 'original' | 'receipt';

class ProposedContent implements vscode.TextDocumentContentProvider {
  readonly onDidChange: vscode.Event<vscode.Uri>;

  constructor(private readonly service: ApprovalService) {
    this.onDidChange = service.contentChanged.event;
  }

  async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const parsed = parseProposed(uri);
    if (!parsed) return '';
    if (parsed.side === 'receipt') return this.service.receipt(parsed.id) ?? '';

    const pending = this.service.get(parsed.id);
    if (!pending) {
      // The approval was answered while its diff was open. Saying so beats an
      // empty document that reads as "the change is gone".
      throw new Error(vscode.l10n.t('This approval is no longer waiting for a decision.'));
    }
    if (parsed.side === 'original') return this.service.diskText(parsed.rel);
    return this.service.proposedText(pending, parsed.rel);
  }
}

/**
 * Badges on the changeset entries.
 *
 * Scoped to `dakcoder-proposed:` on purpose. Decorating the workspace's `file:`
 * URIs would put dakcoder's marks all over the Explorer, where they would
 * outlive the review and be read as a property of the file rather than of a
 * pending call.
 */
class ProtectedBadges implements vscode.FileDecorationProvider {
  readonly onDidChangeFileDecorations: vscode.Event<vscode.Uri[]>;

  constructor(private readonly service: ApprovalService) {
    this.onDidChangeFileDecorations = service.decorationsChanged.event;
  }

  provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
    const parsed = parseProposed(uri);
    if (!parsed || parsed.side !== 'proposed') return undefined;
    const pending = this.service.get(parsed.id);
    if (!pending) return undefined;

    const isProtected = pending.approval.protected.includes(parsed.rel);
    const letter = kindLetter(pending.approval.tool);
    if (!isProtected && !letter) return undefined;
    return {
      // A letter, not just a colour: the badge still says which change this is
      // in a monochrome or high-contrast theme, and to a screen reader the
      // tooltip carries the whole sentence.
      badge: isProtected ? '!' : letter,
      tooltip: isProtected ? protectedReason(parsed.rel) : kindWord(pending.approval.tool),
      color: new vscode.ThemeColor(isProtected ? 'list.warningForeground' : 'list.deemphasizedForeground'),
      propagate: false,
    };
  }
}

// ── the changeset tree ──────────────────────────────────────────────────────

class ChangesetNode {
  constructor(
    readonly approvalId: string,
    readonly rel: string,
  ) {}
}

class ChangesetTree implements vscode.TreeDataProvider<ChangesetNode> {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.emitter.event;

  constructor(private readonly service: ApprovalService) {}

  refresh(): void {
    this.emitter.fire();
  }

  nodesFor(pending: { approval: ApprovalEvent; paths: string[] }): ChangesetNode[] {
    return pending.paths.map((rel) => new ChangesetNode(pending.approval.id, rel));
  }

  getChildren(element?: ChangesetNode): ChangesetNode[] {
    if (element) return [];
    const pending = this.service.shownApproval;
    return pending ? this.nodesFor(pending) : [];
  }

  /** Flat by design: seven files in one call is a list, not a hierarchy. */
  getParent(): undefined {
    return undefined;
  }

  getTreeItem(element: ChangesetNode): vscode.TreeItem {
    const pending = this.service.get(element.approvalId);
    const item = new vscode.TreeItem(path.basename(element.rel));
    // A stable id, so a refresh keeps the tick marks where the reviewer put them
    // and `reveal` can find a node it was not handed.
    item.id = `${element.approvalId}:${element.rel}`;
    item.resourceUri = proposedUri(element.approvalId, 'proposed', element.rel);
    item.description = describe(element.rel, pending?.approval);
    item.checkboxState = pending?.unchecked.has(element.rel)
      ? vscode.TreeItemCheckboxState.Unchecked
      : vscode.TreeItemCheckboxState.Checked;
    item.command = {
      command: 'dakcoder.approval.showDiff',
      title: vscode.l10n.t('Show the proposed change'),
      arguments: [element],
    };
    item.contextValue = 'dakcoder.changesetFile';

    const tooltip = new vscode.MarkdownString();
    tooltip.appendMarkdown(`**${element.rel}**\n\n`);
    if (pending) {
      tooltip.appendMarkdown(`${kindWord(pending.approval.tool)}\n\n`);
      if (pending.approval.protected.includes(element.rel)) {
        tooltip.appendMarkdown(`⚠ ${protectedReason(element.rel)}\n\n`);
      }
      if (pending.approval.reason) tooltip.appendMarkdown(`_${pending.approval.reason}_`);
    }
    item.tooltip = tooltip;
    return item;
  }
}

/** Words, not colours: the row says "protected" even in monochrome. */
function describe(rel: string, approval?: ApprovalEvent): string {
  const dir = path.dirname(rel);
  const parts = [dir === '.' ? '' : dir];
  if (approval?.protected.includes(rel)) parts.push(vscode.l10n.t('protected'));
  return parts.filter(Boolean).join(' · ');
}

// ── protected-path explanations ─────────────────────────────────────────────

/**
 * *Why* a path is protected, for a path the server already said is protected.
 *
 * The classes come from the plan: structural files that decide how the service
 * boots, credential files, schema, and generated code. Only the sentence is
 * chosen here — an unfamiliar shape still gets the generic sentence and stays
 * protected, because the verdict was never ours to make.
 */
function protectedReason(rel: string): string {
  const posix = toPosix(rel);
  const name = path.posix.basename(posix);
  const segments = posix.split('/');

  if (name === 'go.mod' || name === 'go.sum' || segments[0] === 'bootstrap') {
    return vscode.l10n.t(
      'Structural: this file decides how the service builds and wires itself, so a wrong edit breaks startup rather than one endpoint.',
    );
  }
  if (segments.includes('configs')) {
    return vscode.l10n.t('Credentials: files under configs/ hold secrets, and they are never auto-approved regardless of how small the change is.');
  }
  if (segments.includes('db') || name.endsWith('.sql')) {
    return vscode.l10n.t('Schema: a database change outlives the code change and cannot be reverted by editing a file back.');
  }
  if (name.endsWith('_validator.go')) {
    return vscode.l10n.t('Generated — the agent should run govalid_gen instead. A hand-edit here is overwritten by the next generation and is a rule violation you can catch faster than the linter can.');
  }
  return vscode.l10n.t('The runtime marked this path protected, so it is never auto-approved.');
}

/**
 * A cruder net than the server's matcher, and only ever more conservative.
 *
 * This is not a reimplementation of `PROTECTED_GLOBS` — it cannot approve
 * anything, only refuse. It exists because "nothing under configs/** is ever
 * auto-approved" is a rule worth holding even if a future matcher change,
 * a symlink or an odd relative path made the server's list disagree.
 */
function inConfigs(rel: string): boolean {
  return toPosix(rel).split('/').includes('configs');
}

/**
 * The strict half of `autoApproveTrivialPatches`, which is off by default.
 *
 * A one-line patch to a non-protected file is the case this exists for. Anything
 * with room to hide a second change in it is not trivial, whatever the tool
 * called it.
 */
function isTrivialPatch(approval: ApprovalEvent): boolean {
  if (approval.tool !== 'patch_file') return false;
  const args = (approval.arguments ?? {}) as Record<string, unknown>;
  const old = typeof args.old === 'string' ? args.old : undefined;
  const replacement = typeof args.new === 'string' ? args.new : undefined;
  if (old === undefined || replacement === undefined) return false;
  const small = (text: string): boolean => text.length <= 200 && text.split('\n').length <= 3;
  return small(old) && small(replacement);
}

// ── tool-shape helpers (contract, not server data) ──────────────────────────

function canPreview(tool: string): boolean {
  return tool === 'write_file' || tool === 'patch_file' || tool === 'delete_file';
}

function kindLetter(tool: string): string {
  switch (tool) {
    case 'write_file':
      return 'A';
    case 'patch_file':
      return 'M';
    case 'delete_file':
      return 'D';
    default:
      // No per-path kind on an approval, and no honest guess for a tool we do
      // not know: no letter.
      return '';
  }
}

function kindWord(tool: string): string {
  switch (tool) {
    case 'write_file':
      return vscode.l10n.t('will be created');
    case 'patch_file':
      return vscode.l10n.t('will be modified');
    case 'delete_file':
      return vscode.l10n.t('will be deleted');
    case 'resource_scaffold':
    case 'project_scaffold':
      return vscode.l10n.t('will be written from a template when the call runs');
    default:
      return vscode.l10n.t('will be changed by {0}', tool);
  }
}

// ── URIs ────────────────────────────────────────────────────────────────────

/**
 * The relative path is carried in the URI path, so the document gets the right
 * language from its extension and the tab is named after the real file. The URI
 * is stable for a given (approval, side, path) — a query that changed per open
 * would give the same review two tabs.
 */
function proposedUri(id: string, side: Side, rel: string): vscode.Uri {
  return vscode.Uri.from({ scheme: PROPOSED_SCHEME, path: `/${id}/${side}/${toPosix(rel)}` });
}

function receiptUri(id: string): vscode.Uri {
  return vscode.Uri.from({ scheme: PROPOSED_SCHEME, path: `/${id}/receipt/corrected-arguments.json` });
}

function parseProposed(uri: vscode.Uri): { id: string; side: Side; rel: string } | undefined {
  if (uri.scheme !== PROPOSED_SCHEME) return undefined;
  const parts = uri.path.split('/').filter(Boolean);
  if (parts.length < 3) return undefined;
  const [id, side, ...rest] = parts;
  if (side !== 'proposed' && side !== 'original' && side !== 'receipt') return undefined;
  return { id: id!, side, rel: rest.join('/') };
}

function toPosix(p: string): string {
  return p.replace(/\\/g, '/').replace(/^\.\//, '');
}

// ── small helpers ───────────────────────────────────────────────────────────

function config(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration('dakcoder');
}

function cmd(id: string, handler: (arg?: unknown) => unknown): vscode.Disposable {
  return vscode.commands.registerCommand(id, handler);
}

/** Warn late enough not to nag, early enough to be actionable. */
function warnAt(pending: Pending): number {
  const total = Math.max(0, ((pending.deadline ?? 0) - pending.seenAt) / 1000);
  return Math.max(15, Math.min(60, Math.round(total * 0.25)));
}

function parseArgs(text: string): Record<string, unknown> | undefined {
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : undefined;
  } catch {
    return undefined;
  }
}

/**
 * `GET /v1/approvals` is typed `unknown[]` on the client, and additive-only
 * means a future runtime may add fields. Narrow defensively and default what is
 * missing rather than dropping an approval a developer needs to answer.
 */
function asApproval(value: unknown): ApprovalEvent | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const raw = value as Record<string, unknown>;
  if (typeof raw.id !== 'string' || typeof raw.tool !== 'string') return undefined;
  return {
    id: raw.id,
    tool: raw.tool,
    arguments: (raw.arguments ?? {}) as Record<string, unknown>,
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    paths: Array.isArray(raw.paths) ? raw.paths.filter((p): p is string => typeof p === 'string') : [],
    protected: Array.isArray(raw.protected)
      ? raw.protected.filter((p): p is string => typeof p === 'string')
      : [],
    // Absent means "the runtime did not say it is unwaivable"; treating it as
    // waivable is the additive-safe reading, and every other gate still applies.
    unconditional: raw.unconditional === true,
  };
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
