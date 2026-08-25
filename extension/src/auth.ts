/**
 * GitLab sign-in, as a real `vscode.AuthenticationProvider`.
 *
 * Registering through `vscode.authentication.registerAuthenticationProvider`
 * rather than rolling a bespoke prompt is what puts the account in the Accounts
 * menu, lets the platform own session lifetime, and makes `onDidChangeSessions`
 * mean something in a second window.
 *
 * **The gateway issues `state`; this file never invents one** (D-57). A state
 * the gateway has not seen cannot be checked by the only party in a position to
 * check it, so the CSRF property `state` exists to provide would be decorative.
 * The comparison performed here on the way back in is a *routing* filter — which
 * in-flight sign-in does this callback belong to — and `/v1/auth/exchange` is
 * the security boundary. Nothing here weakens if the local lookup is guessed;
 * everything weakens if the gateway's is.
 *
 * **No client secret ships.** Extension code is inspectable, so a secret in it
 * is an announcement rather than a control. Public PKCE client, S256, verifier
 * held in memory *and* mirrored into SecretStorage keyed by `state` — a window
 * reload during the browser round trip must not orphan a callback that is
 * already on its way back, because the developer has no way to tell that from
 * sign-in simply not working.
 *
 * **The refresh token is the only thing that reaches disk**, and only through
 * `context.secrets` (Windows Credential Manager, macOS Keychain, Secret
 * Service). The 15-minute access token lives in memory and dies with the host.
 *
 * **Refresh rotates, and reuse ends the family** (D-58). That makes the
 * single-flight guard load-bearing rather than an optimisation: two concurrent
 * refreshes presenting the same stored token are indistinguishable from a
 * stolen token being replayed, and the gateway would be right to kill both.
 */

import { createHash, randomBytes, randomUUID } from 'node:crypto';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import * as vscode from 'vscode';

import { GatewayClient, HttpError } from './client';

/** Must match `contributes.authentication[0].id` in package.json. */
export const AUTH_PROVIDER_ID = 'dakcoder';
export const AUTH_PROVIDER_LABEL = 'dakcoder (GitLab)';

/**
 * What sign-in asks GitLab for, per Part B §6.2 step 2.
 *
 * The gateway builds the authorize URL and `POST /v1/auth/exchange` does not
 * report which scopes were actually granted, so this is what a session is
 * *labelled* with and never a claim about what the token can do. Nothing in the
 * extension may branch on it.
 */
export const DAKCODER_SCOPES: readonly string[] = ['openid', 'profile', 'email', 'read_api'];

const SECRET_REFRESH = 'dakcoder.auth.refresh';
const SECRET_ACCOUNT = 'dakcoder.auth.account';
const SECRET_PKCE = 'dakcoder.auth.pkce.';
/** State values of flows still in the air, so orphans can be swept — see `sweep`. */
const PENDING_INDEX = 'dakcoder.auth.pending';

/** The gateway's state TTL. Anything older than this is unusable, so it is litter. */
const STATE_TTL_MS = 10 * 60_000;
/** §6.3: single-use, five minutes, then the listener closes. */
const FLOW_TIMEOUT_MS = 5 * 60_000;
/** §6.2 step 7. Refresh at 80% of lifetime, not at expiry. */
const REFRESH_AT = 0.8;
/** Treat a token this close to expiry as already gone; clocks disagree. */
const SKEW_MS = 30_000;

const LOOPBACK_HOSTS: ReadonlySet<string> = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

/** The exchange body, borrowed from the client rather than restated here (C2). */
type ExchangeResult = Awaited<ReturnType<GatewayClient['authExchange']>>;

interface StoredAccount {
  /** Stable across reloads, because `removeSession` is called with it. */
  sessionId: string;
  sub: string;
  label: string;
  /** From the exchange. Displayed by Doctor; never used to gate anything locally. */
  roles: string[];
}

interface PendingFlow {
  verifier: string;
  /** Sent again at exchange: the gateway binds state to the redirect URI (D-57). */
  redirectUri: string;
  resolve?: (session: vscode.AuthenticationSession) => void;
  reject?: (reason: unknown) => void;
}

type CallbackOutcome = 'ok' | 'denied' | 'failed' | 'ignored';

type RotateResult =
  | { kind: 'ok'; token: string }
  | { kind: 'rejected' }
  | { kind: 'unreachable'; error: unknown };

export class DakcoderAuthProvider
  implements vscode.AuthenticationProvider, vscode.UriHandler, vscode.Disposable
{
  private readonly changed =
    new vscode.EventEmitter<vscode.AuthenticationProviderAuthenticationSessionsChangeEvent>();
  readonly onDidChangeSessions = this.changed.event;

  private readonly secrets: vscode.SecretStorage;
  private readonly globals: vscode.Memento;
  private readonly extensionId: string;
  private readonly disposables: vscode.Disposable[] = [];

  private readonly pending = new Map<string, PendingFlow>();
  private account?: StoredAccount;
  /** In memory only, by design. A host restart is a re-refresh, not a re-login. */
  private access?: { token: string; expiresAt: number };
  private refreshing?: Promise<string | undefined>;
  private creating?: Promise<vscode.AuthenticationSession>;
  private proactive?: ReturnType<typeof setTimeout>;
  private loaded?: Promise<void>;
  /** One re-prompt for a dead credential, however many callers hit it at once. */
  private prompting = false;

  constructor(
    context: vscode.ExtensionContext,
    private readonly gateway: GatewayClient,
    private readonly log: vscode.LogOutputChannel,
  ) {
    this.secrets = context.secrets;
    this.globals = context.globalState;
    this.extensionId = context.extension.id;

    // Another window signing in or out is a session change here too, and the
    // Accounts menu in this window is wrong until it is told.
    this.disposables.push(
      this.secrets.onDidChange((e) => {
        if (e.key === SECRET_ACCOUNT) void this.reconcile();
      }),
    );

    void this.sweep();
  }

  // ── what the runtime and the clients ask for ──────────────────────────────

  /**
   * The current access token, or `undefined`.
   *
   * Synchronous on purpose: `Runtime` needs a value while building the child
   * environment and `Rest.headers` needs one per request, and neither can await.
   * The proactive timer is what keeps this from usually being stale; callers
   * that can await should prefer `accessToken()`.
   */
  readonly jwt = (): string | undefined => this.liveToken();

  /** Await-able form: refreshes rather than returning a token about to expire. */
  async accessToken(): Promise<string | undefined> {
    await this.ensureLoaded();
    if (!this.account) return undefined;
    return this.liveToken() ?? (await this.refresh());
  }

  /**
   * The reactive half of §6.2 step 7.
   *
   * `usedToken` is the token the failing request actually sent. Without it, ten
   * concurrent 401s that arrive after the first refresh has already landed would
   * each throw the fresh token away and ask for another — the single-flight
   * guard collapses simultaneous refreshes, not sequential ones.
   */
  async refreshAfter401(usedToken?: string): Promise<string | undefined> {
    await this.ensureLoaded();
    if (!this.account) return undefined;
    if (usedToken && this.access && this.access.token !== usedToken) return this.access.token;
    this.access = undefined;
    return this.refresh();
  }

  /** For Doctor's auth row: signed in, expiry, roles. No secret leaves here. */
  get status(): {
    signedIn: boolean;
    label?: string;
    roles: readonly string[];
    expiresInSeconds?: number;
  } {
    return {
      signedIn: !!this.account,
      label: this.account?.label,
      roles: this.account?.roles ?? [],
      expiresInSeconds: this.access
        ? Math.max(0, Math.round((this.access.expiresAt - Date.now()) / 1000))
        : undefined,
    };
  }

  // ── vscode.AuthenticationProvider ─────────────────────────────────────────

  async getSessions(scopes?: readonly string[]): Promise<vscode.AuthenticationSession[]> {
    await this.ensureLoaded();
    if (!this.account) return [];
    if (scopes?.length) {
      // Deliberately not a filter. The exchange reports no granted-scope field,
      // so matching a request against a set we assumed would be answering with
      // a fact the server never sent.
      this.log.debug(`getSessions asked for scopes: ${scopes.join(' ')}`);
    }
    const token = await this.accessToken();
    // `accessToken` may have discovered the credential is dead and signed out.
    if (!token || !this.account) return [];
    return [sessionOf(this.account, token)];
  }

  createSession(scopes: readonly string[]): Promise<vscode.AuthenticationSession> {
    // Two surfaces asking at once — the status bar and a task start — is normal,
    // and two browser windows for one sign-in is not something to explain away.
    this.creating ??= this.signIn(scopes).finally(() => {
      this.creating = undefined;
    });
    return this.creating;
  }

  async removeSession(sessionId: string): Promise<void> {
    await this.ensureLoaded();
    if (this.account && this.account.sessionId !== sessionId) {
      this.log.warn('ignoring a sign-out for a session this window does not hold');
      return;
    }
    const refresh = await this.secrets.get(SECRET_REFRESH);
    await this.forget(true);
    if (!refresh) return;
    // Revoked after the local state is already gone: a revoke that fails on a
    // flaky network must not leave the developer apparently still signed in to
    // the session they just ended.
    this.gateway.revoke(refresh).then(undefined, (err: unknown) => {
      this.log.warn(`the refresh token could not be revoked server-side: ${describe(err)}`);
    });
  }

  // ── commands ──────────────────────────────────────────────────────────────

  async signInCommand(): Promise<void> {
    try {
      const session = await vscode.authentication.getSession(AUTH_PROVIDER_ID, DAKCODER_SCOPES, {
        createIfNone: true,
      });
      void vscode.window.showInformationMessage(
        vscode.l10n.t('Signed in to dakcoder as {0}.', session.account.label),
      );
    } catch (err) {
      // Cancelling is an answer, not a failure. Anything else is worth showing.
      if (err instanceof vscode.CancellationError) return;
      void vscode.window.showErrorMessage(
        vscode.l10n.t('Sign-in did not complete: {0}', describe(err)),
      );
    }
  }

  async signOutCommand(): Promise<void> {
    await this.ensureLoaded();
    if (!this.account) {
      void vscode.window.showInformationMessage(vscode.l10n.t('You are not signed in to dakcoder.'));
      return;
    }
    const confirm = vscode.l10n.t('Sign Out');
    const choice = await vscode.window.showWarningMessage(
      vscode.l10n.t('Sign out of dakcoder as {0}?', this.account.label),
      {
        modal: true,
        detail: vscode.l10n.t('Running tasks will stop when their current turn ends.'),
      },
      confirm,
    );
    if (choice !== confirm) return;
    await this.removeSession(this.account.sessionId);
    void vscode.window.showInformationMessage(vscode.l10n.t('Signed out of dakcoder.'));
  }

  // ── the flow ──────────────────────────────────────────────────────────────

  private async signIn(requested: readonly string[]): Promise<vscode.AuthenticationSession> {
    if (requested.length) this.log.debug(`sign-in requested scopes: ${requested.join(' ')}`);
    return vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: vscode.l10n.t('Signing in to dakcoder…'),
        cancellable: true,
      },
      async (_progress, cancel) => {
        const loopback = this.preferLoopback()
          ? await startLoopback((params) => this.onCallback(params), this.log)
          : undefined;
        let state: string | undefined;
        try {
          const verifier = base64url(randomBytes(32));
          const challenge = base64url(createHash('sha256').update(verifier).digest());
          const redirectUri = loopback ? loopback.redirectUri : await this.uriHandlerRedirect();

          // The gateway mints and stores `state`; we only carry it (D-57).
          const started = await this.gateway.authStart(redirectUri, challenge);
          state = started.state;
          await this.rememberFlow(state, verifier, redirectUri);

          // Armed before the browser opens. An IdP with a live session can
          // round-trip before `openExternal` has even resolved.
          const arriving = this.awaitFlow(state, cancel);

          const target = safeExternal(started.authorize_url);
          if (!target) {
            throw new Error(
              vscode.l10n.t('The sign-in server returned an address this extension will not open.'),
            );
          }
          if (!(await vscode.env.openExternal(target))) {
            throw new Error(vscode.l10n.t('A browser could not be opened for sign-in.'));
          }
          return await arriving;
        } finally {
          loopback?.dispose();
          if (state) await this.forgetFlow(state);
        }
      },
    );
  }

  /** The `vscode://` half. Delivered here by `registerUriHandler`. */
  async handleUri(uri: vscode.Uri): Promise<void> {
    if (uri.path.replace(/\/$/, '') !== '/auth/callback') return;
    await this.onCallback(new URLSearchParams(uri.query));
  }

  /**
   * Both callback paths land here, and `state` is resolved before anything else
   * is even read. An unrecognised state is discarded silently and logged: it is
   * either litter from an abandoned flow or someone else's traffic, and in
   * neither case is there a person waiting to be shown an error.
   */
  private async onCallback(params: URLSearchParams): Promise<CallbackOutcome> {
    const state = params.get('state') ?? '';
    const flow = state ? (this.pending.get(state) ?? (await this.restoreFlow(state))) : undefined;
    if (!flow) {
      this.log.warn('discarded an auth callback whose state matches no sign-in in progress');
      return 'ignored';
    }

    const error = params.get('error');
    if (error) {
      // `error_description` is attacker-influenceable text. It goes to the log,
      // never into the loopback page and never verbatim into a message box.
      this.log.info(`the identity provider refused the sign-in: ${error}`);
      const denied = error === 'access_denied';
      flow.reject?.(
        denied
          ? new vscode.CancellationError()
          : new Error(vscode.l10n.t('The identity provider refused the sign-in.')),
      );
      await this.forgetFlow(state);
      return denied ? 'denied' : 'failed';
    }

    const code = params.get('code');
    if (!code) {
      this.log.warn('an auth callback carried a valid state but no authorization code');
      flow.reject?.(
        new Error(vscode.l10n.t('The sign-in callback carried no authorization code.')),
      );
      await this.forgetFlow(state);
      return 'failed';
    }
    return this.exchange(state, flow, code);
  }

  private async exchange(state: string, flow: PendingFlow, code: string): Promise<CallbackOutcome> {
    try {
      const result = await this.gateway.authExchange({
        code,
        code_verifier: flow.verifier,
        state,
        redirect_uri: flow.redirectUri,
      });
      const session = await this.persist(result);
      if (flow.resolve) flow.resolve(session);
      else {
        // The window reloaded mid-flow, so nobody is awaiting this and the
        // progress notification is gone. A message is the only way the
        // developer learns the sign-in they started actually landed.
        void vscode.window.showInformationMessage(
          vscode.l10n.t('Signed in to dakcoder as {0}.', session.account.label),
        );
      }
      return 'ok';
    } catch (err) {
      this.log.error(`the token exchange failed: ${describe(err)}`);
      if (flow.reject) flow.reject(err);
      else {
        void vscode.window.showWarningMessage(
          vscode.l10n.t('dakcoder could not finish signing you in. Run "dakcoder: Sign In" again.'),
        );
      }
      return 'failed';
    } finally {
      await this.forgetFlow(state);
    }
  }

  private awaitFlow(
    state: string,
    cancel: vscode.CancellationToken,
  ): Promise<vscode.AuthenticationSession> {
    return new Promise<vscode.AuthenticationSession>((resolve, reject) => {
      const flow = this.pending.get(state);
      if (!flow) {
        reject(new Error(vscode.l10n.t('The sign-in was dropped before the browser opened.')));
        return;
      }
      const timer = setTimeout(
        () => reject(new Error(vscode.l10n.t('Sign-in timed out after five minutes.'))),
        FLOW_TIMEOUT_MS,
      );
      const cancelled = cancel.onCancellationRequested(() => reject(new vscode.CancellationError()));
      const done = (): void => {
        clearTimeout(timer);
        cancelled.dispose();
      };
      flow.resolve = (session) => {
        done();
        resolve(session);
      };
      flow.reject = (reason) => {
        done();
        reject(reason);
      };
    });
  }

  // ── redirect selection ────────────────────────────────────────────────────

  /**
   * §6.3. Under Remote-SSH, dev containers and Codespaces the browser opens on a
   * different machine from the extension host, so a `vscode://` handler has
   * nothing to catch. Choosing by environment rather than by asking is the
   * point: the developer cannot be expected to know which side of that split
   * their browser landed on.
   */
  private preferLoopback(): boolean {
    return !!vscode.env.remoteName || vscode.env.uiKind === vscode.UIKind.Web;
  }

  private async uriHandlerRedirect(): Promise<string> {
    // `vscode.env.uriScheme`, not a literal "vscode": Insiders answers to
    // `vscode-insiders`, and a hardcoded scheme sends the callback to a build
    // the developer is not running — which presents as sign-in silently
    // hanging. The gateway's redirect allowlist has to carry every scheme we
    // ship for.
    const callback = vscode.Uri.parse(`${vscode.env.uriScheme}://${this.extensionId}/auth/callback`);
    return (await vscode.env.asExternalUri(callback)).toString(true);
  }

  // ── flow bookkeeping ──────────────────────────────────────────────────────

  private async rememberFlow(state: string, verifier: string, redirectUri: string): Promise<void> {
    this.pending.set(state, { verifier, redirectUri });
    await this.secrets.store(SECRET_PKCE + state, JSON.stringify({ verifier, redirectUri }));
    // The index holds only the state — a single-use nonce the gateway already
    // knows — not a credential, so `globalState` is the right home for it and
    // the verifier stays in SecretStorage. Without an index there is no way to
    // find orphaned entries later, because SecretStorage cannot be enumerated.
    const index = this.pendingIndex().concat({ state, at: Date.now() });
    await this.globals.update(PENDING_INDEX, index);
  }

  private async restoreFlow(state: string): Promise<PendingFlow | undefined> {
    const raw = await this.secrets.get(SECRET_PKCE + state);
    if (!raw) return undefined;
    try {
      const parsed = JSON.parse(raw) as { verifier?: unknown; redirectUri?: unknown };
      if (typeof parsed.verifier !== 'string' || typeof parsed.redirectUri !== 'string') {
        return undefined;
      }
      const flow: PendingFlow = { verifier: parsed.verifier, redirectUri: parsed.redirectUri };
      this.pending.set(state, flow);
      return flow;
    } catch {
      return undefined;
    }
  }

  private async forgetFlow(state: string): Promise<void> {
    this.pending.delete(state);
    await this.secrets.delete(SECRET_PKCE + state);
    const index = this.pendingIndex().filter((entry) => entry.state !== state);
    await this.globals.update(PENDING_INDEX, index);
  }

  private pendingIndex(): { state: string; at: number }[] {
    const raw = this.globals.get<unknown>(PENDING_INDEX, []);
    if (!Array.isArray(raw)) return [];
    return raw.filter(
      (entry): entry is { state: string; at: number } =>
        !!entry &&
        typeof (entry as { state?: unknown }).state === 'string' &&
        typeof (entry as { at?: unknown }).at === 'number',
    );
  }

  /** A flow abandoned by a crash leaves a verifier in the keychain forever. */
  private async sweep(): Promise<void> {
    const index = this.pendingIndex();
    const now = Date.now();
    const live = index.filter((entry) => now - entry.at < STATE_TTL_MS);
    if (live.length === index.length) return;
    const keep = new Set(live.map((entry) => entry.state));
    for (const entry of index) {
      if (!keep.has(entry.state)) await this.secrets.delete(SECRET_PKCE + entry.state);
    }
    await this.globals.update(PENDING_INDEX, live);
  }

  // ── tokens ────────────────────────────────────────────────────────────────

  private async persist(result: ExchangeResult): Promise<vscode.AuthenticationSession> {
    const account: StoredAccount = {
      sessionId: randomUUID(),
      sub: result.sub,
      label: result.name ?? result.email ?? result.sub,
      roles: result.roles ?? [],
    };
    // Refresh token first, account record last: `SECRET_ACCOUNT` is what other
    // windows watch, so a window that reacts to it finds a usable credential
    // rather than an account it cannot mint a token for.
    await this.secrets.store(SECRET_REFRESH, result.refresh_token);
    this.account = account;
    this.setAccess(result.access_token, result.expires_in);
    await this.secrets.store(SECRET_ACCOUNT, JSON.stringify(account));

    const session = sessionOf(account, result.access_token);
    this.changed.fire({ added: [session], removed: [], changed: [] });
    this.log.info(`signed in as ${account.label}`);
    return session;
  }

  private setAccess(token: string, expiresIn: number): void {
    const lifetime = Math.max(60, expiresIn) * 1000;
    this.access = { token, expiresAt: Date.now() + lifetime };
    if (this.proactive) clearTimeout(this.proactive);
    // Proactive at 80% of lifetime rather than on expiry: a token that dies
    // mid-turn turns a forty-minute run into a 401 the developer has to
    // interpret, and the runtime received its copy in an environment variable
    // at spawn — it cannot re-read this one on its own.
    this.proactive = setTimeout(() => {
      void this.refresh().catch((err: unknown) => {
        this.log.warn(`the proactive refresh failed: ${describe(err)}`);
      });
    }, Math.max(5_000, lifetime * REFRESH_AT));
  }

  private liveToken(): string | undefined {
    if (!this.access) return undefined;
    return this.access.expiresAt - Date.now() > SKEW_MS ? this.access.token : undefined;
  }

  /** Single-flight (§6.2 step 7); D-58 is why it is more than an optimisation. */
  private refresh(): Promise<string | undefined> {
    this.refreshing ??= this.refreshOnce().finally(() => {
      this.refreshing = undefined;
    });
    return this.refreshing;
  }

  private async refreshOnce(): Promise<string | undefined> {
    const stored = await this.secrets.get(SECRET_REFRESH);
    if (!stored) {
      await this.forget(true);
      return undefined;
    }

    const first = await this.rotate(stored);
    if (first.kind === 'ok') return first.token;
    if (first.kind === 'unreachable') {
      // Cannot tell whether the credential is bad or the network is. The safe
      // reading of "cannot tell" is to keep the refresh token and fail only this
      // attempt: discarding it would sign a developer out because a proxy hiccuped.
      this.log.warn(`the token refresh could not be completed: ${describe(first.error)}`);
      return this.liveToken();
    }

    // Rejected. Another window may have rotated between our read and our POST,
    // in which case what we presented was a legitimately spent token rather
    // than a dead family — re-read and retry exactly once before concluding.
    const current = await this.secrets.get(SECRET_REFRESH);
    if (current && current !== stored) {
      const second = await this.rotate(current);
      if (second.kind === 'ok') return second.token;
      if (second.kind === 'unreachable') {
        this.log.warn(`the token refresh could not be completed: ${describe(second.error)}`);
        return this.liveToken();
      }
    }

    // A blocked GitLab account arrives here within one token lifetime (D-58).
    // That is an expected end state rather than a fault, so it gets a re-prompt
    // and not an error dump.
    this.log.info('the stored refresh token was refused; signing out locally');
    await this.forget(true);
    this.promptReauth();
    return undefined;
  }

  private async rotate(refreshToken: string): Promise<RotateResult> {
    try {
      const rotated = await this.gateway.authRefresh(refreshToken);
      // Stored before the access token is handed out: the old refresh token is
      // already spent, so losing the rotated one costs a full sign-in.
      await this.secrets.store(SECRET_REFRESH, rotated.refresh_token);
      this.setAccess(rotated.access_token, rotated.expires_in);
      return { kind: 'ok', token: rotated.access_token };
    } catch (err) {
      return isCredentialRejection(err) ? { kind: 'rejected' } : { kind: 'unreachable', error: err };
    }
  }

  private promptReauth(): void {
    if (this.prompting) return;
    this.prompting = true;
    const signIn = vscode.l10n.t('Sign In');
    void vscode.window
      .showWarningMessage(
        vscode.l10n.t('Your dakcoder sign-in is no longer valid. Sign in again to continue.'),
        signIn,
      )
      .then(
        (choice) => {
          this.prompting = false;
          if (choice === signIn) void vscode.commands.executeCommand('dakcoder.signIn');
        },
        () => {
          this.prompting = false;
        },
      );
  }

  private async forget(notify: boolean): Promise<void> {
    const removed = this.account;
    this.account = undefined;
    this.access = undefined;
    if (this.proactive) clearTimeout(this.proactive);
    this.proactive = undefined;
    await this.secrets.delete(SECRET_REFRESH);
    await this.secrets.delete(SECRET_ACCOUNT);
    if (!notify || !removed) return;
    // The token is already gone and consumers of this event key off id/account,
    // so an empty string is the honest value rather than a stale credential.
    this.changed.fire({ added: [], removed: [sessionOf(removed, '')], changed: [] });
  }

  // ── cross-window ──────────────────────────────────────────────────────────

  private ensureLoaded(): Promise<void> {
    this.loaded ??= this.load();
    return this.loaded;
  }

  private async load(): Promise<void> {
    this.account = parseAccount(await this.secrets.get(SECRET_ACCOUNT));
  }

  /**
   * Another window changed the stored account. Only a change of *identity* is an
   * event worth firing; our own writes land here too and must be a no-op, which
   * is why `this.account` is set before the secret is stored.
   */
  private async reconcile(): Promise<void> {
    this.loaded ??= Promise.resolve();
    const before = this.account;
    const after = parseAccount(await this.secrets.get(SECRET_ACCOUNT));
    this.account = after;
    if (before?.sessionId === after?.sessionId) return;

    if (!after) {
      this.access = undefined;
      if (this.proactive) clearTimeout(this.proactive);
      this.proactive = undefined;
      if (before) this.changed.fire({ added: [], removed: [sessionOf(before, '')], changed: [] });
      return;
    }

    // A different account, or a fresh sign-in elsewhere. Whatever token this
    // window is holding belongs to the previous identity.
    this.access = undefined;
    const token = await this.accessToken();
    if (!token || !this.account) return;
    this.changed.fire({
      added: [sessionOf(this.account, token)],
      removed: before ? [sessionOf(before, '')] : [],
      changed: [],
    });
  }

  dispose(): void {
    if (this.proactive) clearTimeout(this.proactive);
    for (const flow of this.pending.values()) flow.reject?.(new vscode.CancellationError());
    this.pending.clear();
    for (const disposable of this.disposables) disposable.dispose();
    this.changed.dispose();
  }
}

// ── the loopback listener (§6.3) ────────────────────────────────────────────

interface Loopback extends vscode.Disposable {
  readonly redirectUri: string;
}

/**
 * A single-use redirect target on 127.0.0.1 with an ephemeral port.
 *
 * Bound to the loopback interface explicitly rather than to every interface: on
 * a shared or conference network, `0.0.0.0` would put an authorization-code
 * catcher on the wire. The port is ephemeral because a fixed one collides with
 * the second VS Code window, and RFC 8252 is the reason an IdP may accept any
 * port on a loopback redirect.
 */
async function startLoopback(
  onCallback: (params: URLSearchParams) => Promise<CallbackOutcome>,
  log: vscode.LogOutputChannel,
): Promise<Loopback> {
  const server = http.createServer((req, res) => {
    // A relative request-target needs a base to parse against. The base is
    // discarded afterwards, so it does not have to be the real one.
    const url = new URL(req.url ?? '/', 'http://127.0.0.1');
    if (url.pathname !== '/callback') {
      res.writeHead(404).end();
      return;
    }
    void onCallback(url.searchParams).then(
      (outcome) => {
        if (outcome === 'ignored') {
          // Not our callback. Answering 404 and *staying open* matters: closing
          // on an unrecognised request would let any local process end a
          // sign-in by hitting the port once.
          res.writeHead(404).end();
          return;
        }
        res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' }).end(page(outcome));
        close();
      },
      (err: unknown) => {
        log.error(`the loopback callback handler threw: ${describe(err)}`);
        res.writeHead(500).end();
        close();
      },
    );
  });

  let closed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    // Keep-alive sockets outlive `close()` on their own, and a listener that
    // lingers past its flow is exactly what "single-use" is meant to prevent.
    server.closeAllConnections?.();
    server.close();
  };
  const timer = setTimeout(close, FLOW_TIMEOUT_MS);

  const port = await new Promise<number>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address() as AddressInfo | null;
      if (address) resolve(address.port);
      else reject(new Error(vscode.l10n.t('The sign-in listener did not report a port.')));
    });
  });

  log.info(`sign-in will use the loopback redirect on 127.0.0.1:${port}`);
  return { redirectUri: `http://127.0.0.1:${port}/callback`, dispose: close };
}

/**
 * What the browser shows after the redirect.
 *
 * Every string is fixed and escaped, and nothing from the query string is
 * rendered: reflecting `error_description` here would be an injection on a page
 * served from the developer's own loopback. No colour is declared beyond
 * `color-scheme`, so the page follows the browser instead of pretending to be
 * VS Code chrome whose theme it cannot read.
 */
function page(outcome: Exclude<CallbackOutcome, 'ignored'>): string {
  const heading =
    outcome === 'ok'
      ? vscode.l10n.t('Signed in to dakcoder.')
      : outcome === 'denied'
        ? vscode.l10n.t('Sign-in was cancelled.')
        : vscode.l10n.t('Sign-in did not complete.');
  const body =
    outcome === 'ok'
      ? vscode.l10n.t('You can close this tab and return to VS Code.')
      : vscode.l10n.t('You can close this tab. Run "dakcoder: Sign In" in VS Code to try again.');
  return [
    '<!doctype html>',
    `<html lang="${escapeHtml(vscode.env.language)}"><head><meta charset="utf-8">`,
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${escapeHtml(heading)}</title>`,
    '<style>:root{color-scheme:light dark}',
    'body{font:1rem/1.5 system-ui,sans-serif;margin:4rem auto;max-width:32rem;padding:0 1rem}',
    '</style></head><body>',
    `<h1>${escapeHtml(heading)}</h1><p>${escapeHtml(body)}</p>`,
    '</body></html>',
  ].join('');
}

// ── helpers ─────────────────────────────────────────────────────────────────

function sessionOf(account: StoredAccount, accessToken: string): vscode.AuthenticationSession {
  return {
    id: account.sessionId,
    accessToken,
    account: { id: account.sub, label: account.label },
    scopes: DAKCODER_SCOPES,
  };
}

function parseAccount(raw: string | undefined): StoredAccount | undefined {
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as Partial<StoredAccount>;
    if (typeof parsed.sessionId !== 'string' || typeof parsed.sub !== 'string') return undefined;
    return {
      sessionId: parsed.sessionId,
      sub: parsed.sub,
      label: typeof parsed.label === 'string' ? parsed.label : parsed.sub,
      roles: Array.isArray(parsed.roles) ? parsed.roles.filter((r) => typeof r === 'string') : [],
    };
  } catch {
    // A corrupt record is indistinguishable from no record, and the recovery
    // for both is the same sign-in.
    return undefined;
  }
}

/**
 * Whether the gateway rejected the *credential*, as opposed to being unable to
 * answer at all. The distinction decides whether a developer keeps their session
 * through a proxy outage or gets signed out by one.
 */
function isCredentialRejection(err: unknown): boolean {
  return err instanceof HttpError && (err.status === 400 || err.status === 401 || err.status === 403);
}

/** RFC 7636 uses unpadded base64url for both the verifier and the challenge. */
function base64url(bytes: Buffer): string {
  return bytes.toString('base64url');
}

/**
 * Refuse to hand `openExternal` anything but a web address.
 *
 * The authorize URL arrives from our own gateway, but `openExternal` on a
 * `file:` URL or a registered custom scheme launches a local application, and a
 * misconfigured or compromised gateway should not be one response away from that.
 */
function safeExternal(url: string): vscode.Uri | undefined {
  let uri: vscode.Uri;
  try {
    uri = vscode.Uri.parse(url, true);
  } catch {
    return undefined;
  }
  if (uri.scheme === 'https') return uri;
  // A gateway running on loopback during development is the one http exception.
  if (uri.scheme === 'http' && LOOPBACK_HOSTS.has(uri.authority.split(':')[0])) return uri;
  return undefined;
}

function escapeHtml(text: string): string {
  const replacements: Record<string, string> = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  };
  return text.replace(/[&<>"']/g, (ch) => replacements[ch] ?? ch);
}

/** Never renders a token: `HttpError.detail` is a server-written sentence. */
function describe(err: unknown): string {
  if (err instanceof HttpError) return `${err.status} ${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

// ── wiring ──────────────────────────────────────────────────────────────────

/**
 * Register the provider, its URI handler and its two commands.
 *
 * The `GatewayClient` is passed in rather than built here because its bearer
 * token comes from this provider — the assembler breaks that cycle with a
 * closure; see the module notes.
 */
export function register(
  context: vscode.ExtensionContext,
  gateway: GatewayClient,
  log: vscode.LogOutputChannel,
): DakcoderAuthProvider {
  const provider = new DakcoderAuthProvider(context, gateway, log);
  context.subscriptions.push(
    provider,
    vscode.authentication.registerAuthenticationProvider(
      AUTH_PROVIDER_ID,
      AUTH_PROVIDER_LABEL,
      provider,
      { supportsMultipleAccounts: false },
    ),
    vscode.window.registerUriHandler(provider),
    vscode.commands.registerCommand('dakcoder.signIn', () => provider.signInCommand()),
    vscode.commands.registerCommand('dakcoder.signOut', () => provider.signOutCommand()),
  );
  return provider;
}
