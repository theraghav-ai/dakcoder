/**
 * Activation, and the wiring that makes the modules one product.
 *
 * **Nothing expensive happens here.** `onStartupFinished`, and then: no runtime
 * spawn, no `go version`, no network. The extension host is shared with every
 * other extension in the window, and an agent that costs 400 ms of everyone
 * else's startup is an agent people uninstall. The runtime starts on the first
 * task or on an explicit Doctor.
 *
 * **One state, many surfaces.** `RunState` is the single derivation of every
 * number a surface shows. Two surfaces computing the same figure independently
 * is how a panel ends up displaying two different context readings at once, so
 * the status bar, the chat and the trees all read from here and none of them
 * computes anything.
 */

import * as vscode from 'vscode';

import * as approvals from './approvals';
import * as auth from './auth';
import * as chat from './chat';
import { GatewayClient, HttpError } from './client';
import * as diagnostics from './diagnostics';
import * as doctor from './doctor';
import { API_VERSION, isResumable, type SessionSummary } from './protocol';
import { Runtime, RuntimeError } from './runtime';
import { RunState } from './session-state';
import { StatusBar } from './statusbar';
import * as trees from './trees';
import * as wizard from './wizard';

let disposeAll: vscode.Disposable[] = [];

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const started = Date.now();
  const log = vscode.window.createOutputChannel('dakcoder', { log: true });
  context.subscriptions.push(log);

  const config = () => vscode.workspace.getConfiguration('dakcoder');
  const gatewayUrl = () =>
    (config().get<string>('gatewayUrl') || config().get<string>('serverGatewayUrl') || '').replace(
      /\/+$/,
      '',
    );

  // ── auth first: everything else may need a token ─────────────────────────
  const gateway = new GatewayClient(gatewayUrl(), () => undefined);
  const authProvider = auth.register(context, gateway, log);
  // Rebound now that the provider exists, so every gateway call carries the
  // current access token without each caller remembering to ask.
  (gateway as unknown as { token: () => string | undefined }).token = authProvider.jwt;
  // The reactive half of the refresh. Without it a token that expires mid-session
  // surfaces as an unexplained 401 on whatever the developer did next.
  gateway.onUnauthorized = async (usedToken) => {
    const fresh = await authProvider.refreshAfter401(usedToken);
    return Boolean(fresh);
  };

  // ── the local runtime ────────────────────────────────────────────────────
  const workspaceRoot = () => vscode.workspace.workspaceFolders?.[0]?.uri;
  const runtime = new Runtime({
    workspace: workspaceRoot()?.fsPath ?? process.cwd(),
    gatewayUrl: gatewayUrl(),
    jwt: authProvider.jwt,
    accessToken: () => authProvider.accessToken(),
    storage: context.globalStorageUri,
    extensionPath: context.extensionUri.fsPath,
    log,
    pythonPath: config().get<string>('pythonPath') || undefined,
    // On by default, reversing the old `--no-prewarm`. A four-token probe in a
    // background thread costs nothing a developer can perceive and moves cold
    // start off the first request — the one they are watching.
    prewarm: true,
  });
  context.subscriptions.push(runtime);

  const state = new RunState({ client: runtime.client, log });
  context.subscriptions.push(state);

  const statusBar = new StatusBar(state, {
    quota: () => gateway.quota(),
    log,
    commands: {
      panel: 'dakcoder.chat.focus',
      quota: 'dakcoder.quota.focus',
      stop: 'dakcoder.stopTask',
    },
  });
  context.subscriptions.push(statusBar);

  // ── ensure the runtime, with one legible failure path ────────────────────
  let announced = false;
  async function ready(): Promise<boolean> {
    // Sign-in first, and asked for properly.
    //
    // The runtime authenticates to the gateway as the developer and refuses to
    // start without a JWT — correctly, since every model call goes through the
    // gateway and there is no local key. But spawning first and reading the
    // refusal off stderr turns the very first task after install into "the
    // runtime exited with 2", which is the worst possible first impression and
    // tells the developer nothing about what to do.
    if (!(await authProvider.accessToken())) {
      const signIn = vscode.l10n.t('Sign in');
      const picked = await vscode.window.showInformationMessage(
        vscode.l10n.t(
          'Sign in with GitLab to use dakcoder. It runs as you, so your quota and the audit trail are yours.',
        ),
        signIn,
      );
      if (picked !== signIn) return false;
      try {
        await vscode.authentication.getSession(auth.AUTH_PROVIDER_ID, [...auth.DAKCODER_SCOPES], {
          createIfNone: true,
        });
      } catch (err) {
        log.warn(`sign-in did not complete: ${String(err)}`);
        return false;
      }
      if (!(await authProvider.accessToken())) return false;
    }

    try {
      await runtime.ensure();
      announced = true;
      statusBar.clearGatewayOffline();
      return true;
    } catch (err) {
      const message = err instanceof RuntimeError ? err.message : String(err);
      const remedy = err instanceof RuntimeError ? err.remedy : undefined;
      statusBar.setGatewayOffline(message);
      const runDoctor = vscode.l10n.t('Run Doctor');
      const picked = await vscode.window.showErrorMessage(
        remedy ? `${message} ${remedy}` : message,
        runDoctor,
      );
      if (picked === runDoctor) await vscode.commands.executeCommand('dakcoder.doctor');
      return false;
    }
  }

  // ── the surfaces ─────────────────────────────────────────────────────────
  const approvalService = approvals.register(context, {
    client: runtime.client,
    log,
    workspaceRoot,
  });

  // The wire carries no "approval resolved" event, so the surface that answered
  // one has to say so. Without this an answered card sits on screen looking like
  // it is still blocking the run, and the status bar stays on "needs approval"
  // forever.
  context.subscriptions.push(
    approvalService.onDidResolve((outcome) => {
      state.resolveApproval(outcome.id);
      chatView.push({
        id: 0,
        type: 'approval_resolved',
        data: { id: outcome.id, tool: outcome.tool, decision: outcome.decision, auto: outcome.auto },
      });
    }),
  );

  const treeSet = trees.register({
    runtime: runtime.client,
    gateway,
    log,
    state: context.workspaceState,
    openSession: (session) => openSession(session),
    followUp: (session, note) => followUp(session, note),
  });
  context.subscriptions.push(treeSet);

  const doctorService = doctor.register(context, {
    runtime,
    gateway,
    // Assembled here rather than exported by the provider: Doctor wants a
    // reportable snapshot, and the provider's job is holding a live token.
    auth: async () => {
      const sessions = await authProvider.getSessions();
      const session = sessions[0];
      return session
        ? {
            signedIn: true,
            account: session.account.label,
            roles: [...session.scopes],
          }
        : { signedIn: false };
    },
  });

  const chatView = chat.register(
    context,
    {
      submit: (text, steering) => submit(text, steering),
      stop: () => vscode.commands.executeCommand('dakcoder.stopTask'),
      windDown: () => vscode.commands.executeCommand('dakcoder.windDown'),
      decide: async (id, decision) => {
        const pending = approvalService.get(id);
        // Gone means answered, timed out, or the run ended — all "too late"
        // rather than an error worth showing.
        if (!pending) return;
        if (decision === 'reject') await approvalService.reject(pending);
        else if (decision === 'edit') await approvalService.editArgs(pending);
        else await approvalService.accept(pending);
      },
      showDiff: (id) => {
        const pending = approvalService.get(id);
        if (pending) void approvalService.showDiff(pending);
      },
      editArguments: (id) => {
        const pending = approvalService.get(id);
        if (pending) void approvalService.editArgs(pending);
      },
      runSlashCommand: (command, argument) => slash(command, argument),
      complete: (kind, query) => complete(kind, query),
      openPath: (path) => openWorkspacePath(path),
    },
    log,
  );

  // Findings come from a local binary, not the model: `gotools lint --format
  // json` emits structured violations sub-second, offline, signed out, for zero
  // tokens. Regexing prose out of a tool_result would cost a run to learn less.
  context.subscriptions.push(
    diagnostics.register(context, {
      startTask: async (task, options) => {
        if (!(await ready())) return;
        const session = await runtime.client.startTask(task, {
          mode: options?.mode ?? 'debugger',
          acceptance: options?.acceptance ?? [],
        });
        state.hydrate(session);
        state.attach(session.id);
        treeSet.sessions.refresh();
      },
      workspaceRoot,
      extensionUri: context.extensionUri,
      log,
    }),
  );

  context.subscriptions.push(
    wizard.register(context, {
      log,
      workspaceRoot,
      // The spec travels as fenced JSON inside the task, because `POST /v1/tasks`
      // takes only {task, mode, acceptance}. The scaffolder is deterministic, so
      // the model's job here is to relay a spec it did not have to invent.
      scaffold: async (request) => {
        if (!(await ready())) return;
        const session = await runtime.client.startTask(request.task, { mode: 'scaffolder' });
        state.hydrate(session);
        state.attach(session.id);
        treeSet.sessions.refresh();
      },
      migrate: async (units) => {
        if (!(await ready())) return;
        const listed = units
          .map((u, i) => `${i + 1}. ${u.path} (${u.classification})`)
          .join('\n');
        const session = await runtime.client.startTask(
          `Migrate these units to the n-api-template contract, in this order:\n${listed}`,
          { mode: 'planner' },
        );
        state.hydrate(session);
        state.attach(session.id);
        treeSet.sessions.refresh();
      },
    }),
  );

  // ── the panel follows the one state ──────────────────────────────────────
  // Every number the panel shows is read from `state`; nothing here recomputes
  // one. That is the rule that stops two surfaces disagreeing on screen.
  context.subscriptions.push(
    // The session travels with the event: the renderer keys its rows by it, and
    // wire event ids are unique only within one session.
    state.onDidReceive((event) => chatView.push(event, state.sessionId ?? '')),
    state.onDidChange(() => {
      chatView.setRunState({
        phase: state.running ? 'running' : 'idle',
        mode: state.modeId as never,
        turn: state.turn,
        attempt: state.attempt,
        // Read from the one state, never recomputed. Two surfaces deriving the
        // same figure independently is how a panel shows two context readings
        // that disagree.
        blockedBy: state.gateLadder?.blockedBy,
        context: state.contextMeter(),
        tool: state.currentTool,
        startedAt: state.running ? Date.now() - state.elapsedMs : undefined,
      });
    }),
  );

  /**
   * Mention completions.
   *
   * Symbol and package mentions insert a *reference*, never the content behind
   * it. A `@pkg:` mention costs tens of tokens where attaching the package's
   * files would cost thousands, and the agent can fetch what it actually needs.
   */
  async function complete(kind: string, query: string): Promise<{ insert: string; label: string; detail?: string }[]> {
    const root = workspaceRoot();
    if (kind === 'file') {
      const found = await vscode.workspace.findFiles(
        `**/*${query || ''}*`,
        '{**/node_modules/**,**/vendor/**,**/.git/**}',
        20,
      );
      return found.map((uri) => {
        const rel = root ? vscode.workspace.asRelativePath(uri, false) : uri.fsPath;
        return { insert: `@${rel}`, label: rel.split('/').pop() ?? rel, detail: rel };
      });
    }
    if (kind === 'diag') {
      const active = vscode.window.activeTextEditor?.document.uri;
      if (!active) return [];
      const count = vscode.languages.getDiagnostics(active).length;
      const rel = vscode.workspace.asRelativePath(active, false);
      return [
        {
          insert: '@diag',
          label: vscode.l10n.t('Problems in this file'),
          detail:
            count === 1
              ? vscode.l10n.t('1 problem in {0}', rel)
              : vscode.l10n.t('{0} problems in {1}', count, rel),
        },
      ];
    }
    if (kind === 'build') {
      return [{ insert: '@build', label: vscode.l10n.t('Last build output'), detail: 'go build' }];
    }
    // `symbol` and `package` need gopls, which this build does not integrate.
    // Returning nothing is honest; offering entries that resolve to nothing on
    // the server would be worse than offering none.
    return [];
  }

  // ── running a task ───────────────────────────────────────────────────────

  /**
   * Send the composer's message.
   *
   * **One conversation is one session.** A message typed after a run has
   * finished continues the session it finished on; only the first message of a
   * conversation starts a new one. Starting a session per message is what the
   * first build did, and it produced three separate faults from one cause: the
   * Sessions tree filled with one-line rows, the model answered the second
   * question having never seen the first, and — because event ids restart at 1
   * for each session — the second run's rows landed on top of the first run's in
   * the panel and erased them.
   *
   * The runtime decides what the message means from the state it finds the
   * session in, so this does not check `running` first: a run can end between
   * the check and the post, and the whole point of steering is that a correction
   * typed at turn 12 arrives before turn 13 rather than after the run.
   */
  async function submit(text: string, _steering: boolean): Promise<void> {
    if (!(await ready())) return;
    const trimmed = text.trim();
    if (!trimmed) return;

    // Read before the round trip. The runtime decides what the message means
    // from the state it finds, and this is only used to interpret the answer:
    // `queued` on a session that was already idle is a leftover, not a depth.
    const wasRunning = state.running;
    try {
      if (state.sessionId) {
        const session = await runtime.client.message(state.sessionId, trimmed);
        const queued = wasRunning ? session.queued : 0;
        state.hydrate(session);
        // Re-attached only when the message started a run. A correction queued
        // against a live run leaves the existing stream doing the work.
        if (!wasRunning && session.status === 'running') state.attach(session.id);
        // The depth, from the server that holds the queue. The panel has shown a
        // "queued" chip since it was written and nothing ever filled it, so a
        // correction typed at turn 12 gave no sign it had been received.
        chatView.setQueued(queued);
        treeSet.sessions.refresh();
        void statusBar.refresh(true);
        return;
      }
      await startConversation(trimmed);
    } catch (err) {
      // The session may have been deleted, or trimmed off the end of the
      // runtime's 200-session table, or belong to a runtime that has since
      // restarted. None of those are the developer's problem: their message is
      // the start of a new conversation instead of a lost one.
      if (err instanceof HttpError && err.status === 404 && state.sessionId) {
        state.reset();
        try {
          await startConversation(trimmed);
          return;
        } catch (retry) {
          reportRunError(retry, log);
          return;
        }
      }
      reportRunError(err, log);
    }
  }

  async function startConversation(task: string): Promise<void> {
    const session = await runtime.client.startTask(task, {
      mode: modeFor(config().get<string>('defaultMode') ?? 'multi'),
    });
    chatView.showSession(session.id);
    state.hydrate(session);
    state.attach(session.id);
    treeSet.sessions.refresh();
    void statusBar.refresh(true);
  }

  async function openSession(session: SessionSummary): Promise<void> {
    // Before the hydrate, not after: hydrating replays the transcript through
    // the same `onDidReceive` the live stream uses, so a clear that arrived
    // afterwards would wipe the rows it had just drawn.
    chatView.showSession(session.id);
    state.hydrate(session);
    if (session.status === 'running') state.attach(session.id);
    else {
      const full = await runtime.client.session(session.id, true);
      state.hydrate(full);
    }
    await focusPanel();
  }

  /**
   * Reveal the panel, best effort.
   *
   * `dakcoder.chat.focus` is VS Code's, minted for the contributed view, and it
   * is not registered here — see the note by the command registrations. Reaching
   * it is a nicety on top of opening a session, so a failure to reveal must not
   * become the error the developer is shown instead of their conversation.
   */
  async function focusPanel(): Promise<void> {
    try {
      await vscode.commands.executeCommand('dakcoder.chat.focus');
    } catch (err) {
      log.warn(`the chat panel could not be revealed: ${String(err)}`);
    }
  }

  async function followUp(session: SessionSummary, note: string): Promise<void> {
    if (!(await ready())) return;
    try {
      // A finished run takes a follow-up, not a resume: re-running a successful
      // change would re-enter the gate loop on something that already passed.
      // Either way it continues *this* session — the row the developer clicked —
      // rather than whichever conversation the panel happens to be showing.
      //
      // The note used to travel as the old task re-typed with the follow-up
      // appended, because starting a new session was the only way to say
      // anything to a finished one and a new session remembered nothing. The
      // runtime keeps the conversation now, so the note is just the note.
      const next = isResumable(session.status)
        ? await runtime.client.resume(session.id, note)
        : await runtime.client.message(session.id, note);
      // The row the developer clicked need not be the conversation on screen.
      chatView.showSession(next.id);
      state.hydrate(next);
      state.attach(next.id);
      treeSet.sessions.refresh();
      await focusPanel();
    } catch (err) {
      reportRunError(err, log);
    }
  }

  function slash(command: string, argument: string): void {
    const routed: Record<string, string> = {
      scaffold: 'dakcoder.scaffoldResource',
      service: 'dakcoder.scaffoldService',
      audit: 'dakcoder.auditTemplate',
      legacy: 'dakcoder.auditLegacy',
      migrate: 'dakcoder.migrate',
      debug: 'dakcoder.debugLastFailure',
      compact: 'dakcoder.compactContext',
      test: 'dakcoder.runTests',
      wire: 'dakcoder.wireIntoFx',
      explain: 'dakcoder.explainRule',
      fix: 'dakcoder.fixDiagnostic',
      rule: 'dakcoder.explainRule',
    };
    const id = routed[command];
    if (id) void vscode.commands.executeCommand(id, argument);
    // No else: an unknown slash command is the developer typing, not an error.
  }

  async function openWorkspacePath(relative: string): Promise<void> {
    const root = workspaceRoot();
    if (!root) return;
    const uri = vscode.Uri.joinPath(root, relative);
    try {
      await vscode.window.showTextDocument(uri, { preview: true });
    } catch {
      log.warn(`could not open ${relative}`);
    }
  }

  // ── commands ─────────────────────────────────────────────────────────────

  const command = (id: string, run: (...args: unknown[]) => unknown) =>
    context.subscriptions.push(vscode.commands.registerCommand(id, run));

  command('dakcoder.newTask', async () => {
    const recent = (await runtime.client.sessions().catch(() => ({ sessions: [] }))).sessions
      .slice(0, 8)
      .map((s) => ({ label: s.task, description: s.status }));
    const picked = await vscode.window.showQuickPick(
      [{ label: vscode.l10n.t('$(edit) New task…'), description: '' }, ...recent],
      { placeHolder: vscode.l10n.t('What should dakcoder do?') },
    );
    if (!picked) return;
    const text =
      picked.description === ''
        ? await vscode.window.showInputBox({
            prompt: vscode.l10n.t('Describe the change. Be specific about the resource and the layer.'),
          })
        : picked.label;
    if (text) await submit(text, false);
  });

  command('dakcoder.stopTask', async () => {
    if (!state.sessionId) return;
    await runtime.client.abort(state.sessionId).catch(() => undefined);
  });

  command('dakcoder.windDown', async () => {
    if (!state.sessionId) return;
    await runtime.client.windDown(state.sessionId).catch(() => undefined);
  });

  command('dakcoder.compactContext', async () => {
    // Guarded on `running`, not just on having a session. `/compact` reaches the
    // runtime as an ordinary message, and a message to a session that has
    // finished is the next thing the developer wants said — so on an idle
    // session this would start a run whose task was the literal word "/compact".
    if (!state.sessionId || !state.running) {
      void vscode.window.showInformationMessage(
        vscode.l10n.t('There is no run in progress to compact.'),
      );
      return;
    }
    await runtime.client.message(state.sessionId, '/compact').catch(() => undefined);
  });

  command('dakcoder.restartRuntime', async () => {
    runtime.dispose();
    announced = false;
    if (await ready()) {
      void vscode.window.showInformationMessage(
        vscode.l10n.t('The dakcoder runtime restarted on port {0}.', String(runtime.port ?? 0)),
      );
    }
  });

  command('dakcoder.showOutput', () => log.show());
  // Declared in the manifest by the chat module, which owns the behaviour but
  // not the activation. A command in the palette with no registration throws
  // when invoked, and the palette is exactly where someone finds it.
  /**
   * Clear the panel *and* end the conversation.
   *
   * Both halves, because the transcript on screen and the session the next
   * message would continue are one thing to a developer. Clearing only the view
   * would leave the next message appended to a conversation they had just
   * watched disappear; clearing only the session would leave rows on screen that
   * nothing can be said to.
   */
  command('dakcoder.clearChat', () => {
    state.detach();
    state.reset();
    chatView.clear();
    void statusBar.refresh(true);
  });
  command('dakcoder.focusComposer', () => chatView.focusComposer());
  command('dakcoder.doctor', () => doctorService.run({ reveal: true }));
  // `dakcoder.chat.focus` is deliberately NOT registered here. VS Code mints
  // `<viewId>.focus` for every contributed view, and `dakcoder.chat` is one — so
  // registering it shadowed the built-in with a handler whose whole body was
  // `executeCommand('dakcoder.chat.focus')`. A command that calls itself: every
  // call recursed until the extension host tried to marshal a non-cloneable
  // argument to the main thread and gave up with "An object could not be
  // cloned", 165 times in one session's log. Opening a session from the tree
  // awaits this command, which is why it presented as navigation being broken.

  // ── activation is over; everything below is lazy ─────────────────────────
  disposeAll = context.subscriptions as vscode.Disposable[];
  const elapsed = Date.now() - started;
  log.info(`dakcoder activated in ${elapsed} ms (api ${API_VERSION})`);
  if (elapsed > 50) {
    // The budget is 50 ms. Logged rather than thrown, because a slow machine is
    // not a defect — but a regression here is invisible unless it is recorded.
    log.warn(`activation took ${elapsed} ms, over the 50 ms budget`);
  }
  void announced;
}

export function deactivate(): void {
  for (const item of disposeAll) {
    try {
      item.dispose();
    } catch {
      /* shutdown must not throw */
    }
  }
  disposeAll = [];
}

/** `multi` means "let the agent choose", which on the wire is the planner. */
function modeFor(setting: string): string {
  return setting === 'multi' ? 'planner' : setting;
}

function reportRunError(err: unknown, log: vscode.LogOutputChannel): void {
  if (err instanceof HttpError && err.isQuota) {
    // The server writes one human sentence for exactly this. Never dump the
    // JSON body into the chat.
    const wait = err.retryAfter ? humanDuration(err.retryAfter) : undefined;
    void vscode.window.showWarningMessage(
      wait ? `${err.detail} ${vscode.l10n.t('Try again in {0}.', wait)}` : err.detail,
      vscode.l10n.t('Show quota'),
    );
    return;
  }
  log.error(String(err));
  void vscode.window.showErrorMessage(
    err instanceof Error ? err.message : vscode.l10n.t('The task could not be started.'),
  );
}

function humanDuration(seconds: number): string {
  if (seconds < 60) return vscode.l10n.t('{0} seconds', Math.round(seconds));
  const minutes = Math.round(seconds / 60);
  return minutes === 1 ? vscode.l10n.t('1 minute') : vscode.l10n.t('{0} minutes', minutes);
}
