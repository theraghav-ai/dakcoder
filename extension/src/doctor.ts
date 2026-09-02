/**
 * Doctor — the Go toolchain preflight.
 *
 * Local-first (D2) means the developer's own machine compiles the code, which
 * makes the toolchain the single largest adoption risk in the programme and the
 * one risk a good preflight can eliminate outright. Three rules shape this file.
 *
 * **Every failure carries a remedy.** A diagnosis nobody can act on is a slower
 * way of saying "it is broken". Each check may attach a `fix` the tree runs in
 * one click — `go env -w`, `go install`, a directory that needs creating, a git
 * config line placed on the clipboard.
 *
 * **The whole report is one copy-paste.** The first thing a blocked developer
 * does is paste it into a support chat, so it is plain text, ordered, and
 * self-describing — and it redacts the credentials that ride inside proxy URLs,
 * because a support chat is not a secret store.
 *
 * **A missing prerequisite skips its dependants rather than failing them.** Ten
 * red rows that all mean "Go is not installed" hide the one row that matters.
 *
 * Evidence lives in a `LogOutputChannel`: every command line, its exit status,
 * its raw output. The tree says what is wrong; the log proves it.
 */

import { promises as fsp } from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

import { GatewayClient, HttpError } from './client';
import { API_VERSION } from './protocol';
import { Runtime, run } from './runtime';

// ── the shape of a check ────────────────────────────────────────────────────

/**
 * `fail` means the agent cannot do its job until this is fixed; `warn` means
 * degraded but usable. Nothing else distinguishes blocking from advisory —
 * a separate `blocking` field would let the two disagree, and then the tree and
 * the daily preflight would disagree about the same check.
 */
export type CheckState = 'pass' | 'fail' | 'warn' | 'skip';

export interface Remedy {
  readonly label: string;
  /** Performs the fix only. Re-running the checks is the caller's job. */
  run(): Promise<void>;
}

export interface Check {
  readonly name: string;
  readonly state: CheckState;
  readonly detail: string;
  readonly fix?: Remedy;
}

export interface Section {
  readonly title: string;
  readonly checks: Check[];
}

export interface DoctorReport {
  readonly at: Date;
  readonly sections: Section[];
  readonly failed: number;
  readonly warned: number;
}

/**
 * What Doctor needs to know about sign-in.
 *
 * Deliberately not the auth module's session type: Doctor must never hold, log
 * or render a token, and a structural interface that has no field for one
 * cannot leak one by accident. `expiresAt` is epoch milliseconds.
 */
export interface AuthSnapshot {
  signedIn: boolean;
  account?: string;
  expiresAt?: number;
  roles?: string[];
}

export interface DoctorDeps {
  runtime: Runtime;
  gateway: GatewayClient;
  auth(): Promise<AuthSnapshot>;
}

// ── constants ───────────────────────────────────────────────────────────────

/** The template is `go 1.25.0`; anything older cannot build a scaffolded service. */
const MIN_GO: readonly [number, number, number] = [1, 25, 0];

const GITLAB_HOST = 'gitlab.cept.gov.in';
const GOPRIVATE_PATTERN = `${GITLAB_HOST}/*`;
/** Small, public inside the network, and every developer already has access. */
const PROBE_REPO = `https://${GITLAB_HOST}/it-2.0-common/n-api-server.git`;
const TOKEN_PAGE = `https://${GITLAB_HOST}/-/user_settings/personal_access_tokens`;

const GOPLS_PACKAGE = 'golang.org/x/tools/gopls@latest';
/** SOP.md §2 names `@latest` rather than a pin, so there is no version to compare against. */
const GOVALID_PACKAGE = `${GITLAB_HOST}/it-2.0-common/n-api-validation/cmd/govalid@latest`;
const GOLANGCI_PACKAGE = 'github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest';
const GOVULNCHECK_PACKAGE = 'golang.org/x/vuln/cmd/govulncheck@latest';
const BUF_PACKAGE = 'github.com/bufbuild/buf/cmd/buf@latest';

const EXTENSION_ID = 'dop.dakcoder-go';
const LAST_PREFLIGHT = 'dakcoder.doctor.lastPreflight';

/**
 * Every probe is bounded. `git ls-remote` against a host with no usable
 * credential can sit on a credential prompt indefinitely, and a Doctor that
 * hangs is worse than a Doctor that reports a timeout — the developer cannot
 * even tell which check is stuck.
 */
const PROBE_TIMEOUT_MS = 20_000;

// ── the checks ──────────────────────────────────────────────────────────────

export class Doctor implements vscode.Disposable {
  readonly log: vscode.LogOutputChannel;
  private readonly tree = new DoctorTree();
  private readonly view: vscode.TreeView<DoctorNode>;
  private readonly disposables: vscode.Disposable[] = [];
  private report?: DoctorReport;
  private running?: Promise<DoctorReport>;

  constructor(
    private readonly deps: DoctorDeps,
    private readonly memento: vscode.Memento,
  ) {
    this.log = vscode.window.createOutputChannel(vscode.l10n.t('dakcoder Doctor'), { log: true });
    this.view = vscode.window.createTreeView('dakcoder.doctor', {
      treeDataProvider: this.tree,
      showCollapseAll: true,
    });
    this.disposables.push(this.log, this.view);
  }

  get lastReport(): DoctorReport | undefined {
    return this.report;
  }

  // ── entry points ──────────────────────────────────────────────────────────

  /**
   * Run every check.
   *
   * Concurrent invocations share one run: the developer clicking Refresh while
   * the daily preflight is in flight must not spawn a second `go env` storm.
   */
  async check(): Promise<DoctorReport> {
    this.running ??= this.runAll().finally(() => {
      this.running = undefined;
    });
    return this.running;
  }

  /**
   * The silent run before the first task of the day.
   *
   * Cached on the *local* date, because "the first task of a day" means the
   * developer's day. The date is remembered globally but the report is not, so
   * a new window runs the matrix once — deliberate: a new window can mean a new
   * shell, and a stale pass from this morning's environment is worse than a
   * second `go env`.
   *
   * Nothing is shown unless something blocks, and even then it is a
   * notification with an action rather than a stolen focus — the developer
   * asked to start a task, not to read a report.
   */
  async preflight(): Promise<DoctorReport | undefined> {
    const today = localDate(new Date());
    if (this.memento.get<string>(LAST_PREFLIGHT) === today && this.report) return this.report;

    const report = await this.check();
    await this.memento.update(LAST_PREFLIGHT, today);
    if (report.failed > 0) {
      const show = vscode.l10n.t('Show Doctor');
      const message =
        report.failed === 1
          ? vscode.l10n.t('dakcoder: 1 preflight check is failing.')
          : vscode.l10n.t('dakcoder: {0} preflight checks are failing.', report.failed);
      void vscode.window.showWarningMessage(message, show).then((picked) => {
        if (picked === show) void this.reveal();
      });
    }
    return report;
  }

  /**
   * The visible run: progress while it works, and the tree brought forward when
   * the developer asked for it by name.
   */
  async run(opts: { reveal?: boolean } = {}): Promise<DoctorReport> {
    const report = await vscode.window.withProgress(
      {
        // Window, not Notification: a preflight is not important enough to sit
        // in front of the editor, and Notification progress pulls the eye away.
        location: vscode.ProgressLocation.Window,
        title: vscode.l10n.t('dakcoder: checking your toolchain…'),
      },
      () => this.check(),
    );
    if (opts.reveal) await this.reveal();
    return report;
  }

  /** What the `dakcoder.doctor` command calls: run, then bring the tree forward. */
  checkAndReveal(): Promise<DoctorReport> {
    return this.run({ reveal: true });
  }

  private async reveal(): Promise<void> {
    // Only ever from an explicit command. The daily preflight never focuses a
    // view: taking focus off the editor for a background check is the fastest
    // way to make a developer disable the feature.
    await vscode.commands.executeCommand('dakcoder.doctor.focus');
  }

  async applyFix(check: Check | undefined): Promise<void> {
    if (!check?.fix) return;
    try {
      await check.fix.run();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.log.error(`${check.name}: ${redact(message)}`);
      void vscode.window.showErrorMessage(
        vscode.l10n.t('{0} did not succeed: {1}', check.fix.label, redact(firstLine(message))),
      );
      return;
    }
    // Re-checking is how the developer sees the fix landed. Doing it here rather
    // than inside each remedy keeps the remedies single-purpose and testable.
    await this.run();
  }

  async copyReport(): Promise<void> {
    const report = this.report ?? (await this.check());
    await vscode.env.clipboard.writeText(this.renderReport(report));
    void vscode.window.showInformationMessage(
      vscode.l10n.t('The Doctor report is on your clipboard, ready to paste into a support chat.'),
    );
  }

  dispose(): void {
    for (const d of this.disposables) d.dispose();
    this.disposables.length = 0;
  }

  // ── the run ───────────────────────────────────────────────────────────────

  private async runAll(): Promise<DoctorReport> {
    const at = new Date();
    this.log.info(`doctor: starting on ${process.platform} at ${at.toISOString()}`);

    const go = this.goBinary();
    const versionProbe = await this.probe(go, ['version']);
    const goCheck = this.goVersionCheck(go, versionProbe);
    // One `go env -json` for the whole run: the toolchain is authoritative about
    // its own configuration, and asking it once means the report can never show
    // two variables read at two different moments.
    const env = versionProbe.ok ? await this.goEnv(go) : {};
    const goUsable = versionProbe.ok;
    const root = workspaceRoot();

    const toolchain: Check[] = [goCheck];
    if (goUsable) {
      toolchain.push(
        await this.checkGoDir('GOPATH', env.GOPATH ?? '', true),
        await this.checkGoDir('GOBIN', env.GOBIN ?? '', false, env.GOPATH),
        await this.checkGoDir('GOMODCACHE', env.GOMODCACHE ?? '', true),
      );
    } else {
      toolchain.push(
        skipped('GOPATH', goMissing()),
        skipped('GOBIN', goMissing()),
        skipped('GOMODCACHE', goMissing()),
      );
    }

    const privateModules: Check[] = goUsable
      ? [this.checkGoprivate(go, env), this.checkSumVerification(go, env), await this.checkGitCredential()]
      : [
          skipped('GOPRIVATE', goMissing()),
          skipped(vscode.l10n.t('Checksum verification'), goMissing()),
          await this.checkGitCredential(),
        ];

    const tools: Check[] = goUsable
      ? await this.checkTools(env, root)
      : [
          skipped('gopls', goMissing()),
          skipped('govalid', goMissing()),
          skipped('golangci-lint', goMissing()),
          skipped('govulncheck', goMissing()),
          skipped('buf', goMissing()),
        ];

    const module = await this.checkModule(root);
    const runtime = await this.checkRuntime();
    const identity = await this.checkGatewayAndIdentity();

    const sections: Section[] = [
      { title: vscode.l10n.t('Go toolchain'), checks: toolchain },
      { title: vscode.l10n.t('Private modules'), checks: privateModules },
      { title: vscode.l10n.t('Go tools'), checks: tools },
      { title: vscode.l10n.t('This module'), checks: module },
      { title: vscode.l10n.t('Local runtime'), checks: runtime },
      { title: vscode.l10n.t('Gateway and identity'), checks: identity },
      { title: vscode.l10n.t('Environment'), checks: [this.checkProxy()] },
    ];

    let failed = 0;
    let warned = 0;
    for (const section of sections) {
      for (const check of section.checks) {
        if (check.state === 'fail') failed += 1;
        if (check.state === 'warn') warned += 1;
        this.log.info(`  [${check.state}] ${check.name} — ${redact(check.detail)}`);
      }
    }

    const report: DoctorReport = { at, sections, failed, warned };
    this.report = report;
    this.tree.set(report);
    this.view.description =
      failed === 0 && warned === 0
        ? vscode.l10n.t('all clear')
        : failed === 1
          ? vscode.l10n.t('1 failing')
          : failed > 1
            ? vscode.l10n.t('{0} failing', failed)
            : warned === 1
              ? vscode.l10n.t('1 warning')
              : vscode.l10n.t('{0} warnings', warned);
    return report;
  }

  // ── Go toolchain ──────────────────────────────────────────────────────────

  private goVersionCheck(go: string, probe: ProbeResult): Check {
    const name = vscode.l10n.t('Go compiler');
    if (!probe.ok) {
      return {
        name,
        state: 'fail',
        detail: vscode.l10n.t(
          '"{0}" could not be run. dakcoder compiles and tests on this machine, so nothing works without it.',
          go,
        ),
        fix: this.pointAtGoBinary(),
      };
    }

    const found = parseGoVersion(probe.out);
    if (!found) {
      // A `go version` we cannot parse is a warning, never a block: refusing to
      // work because of an unrecognised version string would be the extension
      // failing on its own strictness rather than on a real problem.
      return {
        name,
        state: 'warn',
        detail: vscode.l10n.t('installed, but its version could not be read from "{0}"', firstLine(probe.out)),
      };
    }

    const wanted = MIN_GO.join('.');
    if (compareVersions(found, MIN_GO) < 0) {
      return {
        name,
        state: 'fail',
        detail: vscode.l10n.t(
          'go{0} is installed; the n-api-template declares go {1}, so this toolchain cannot build a scaffolded service. Install Go {1} or newer from the internal distribution.',
          found.join('.'),
          wanted,
        ),
        fix: this.pointAtGoBinary(),
      };
    }
    return { name, state: 'pass', detail: vscode.l10n.t('go{0} — meets the go {1} the template declares', found.join('.'), wanted) };
  }

  /**
   * A directory check that also proves it is writable.
   *
   * Writability is proved by writing, not by `access(W_OK)`: on Windows the
   * access bits do not reflect ACLs, so `W_OK` succeeds on directories the
   * build will later fail to write into — which surfaces as an unreadable
   * error deep inside `go build`.
   */
  private async checkGoDir(
    name: string,
    value: string,
    required: boolean,
    gopath?: string,
  ): Promise<Check> {
    if (!value) {
      if (!required) {
        return {
          name,
          state: 'skip',
          detail: gopath
            ? vscode.l10n.t('unset — go installs binaries into {0}', path.join(gopath, 'bin'))
            : vscode.l10n.t('unset — go uses its default'),
        };
      }
      return { name, state: 'fail', detail: vscode.l10n.t('unset, and the toolchain reports no default') };
    }

    const probeFile = path.join(value, '.dakcoder-write-probe');
    try {
      await fsp.writeFile(probeFile, '');
      await fsp.rm(probeFile, { force: true });
      return { name, state: 'pass', detail: vscode.l10n.t('{0} — writable', value) };
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === 'ENOENT') {
        return {
          name,
          state: 'fail',
          detail: vscode.l10n.t('{0} does not exist', value),
          fix: {
            label: vscode.l10n.t('Create the directory'),
            run: async () => {
              await fsp.mkdir(value, { recursive: true });
              this.log.info(`created ${value}`);
            },
          },
        };
      }
      return {
        name,
        state: 'fail',
        detail: vscode.l10n.t('{0} is not writable ({1}). go build and go install will fail.', value, code ?? 'unknown'),
      };
    }
  }

  // ── private modules ───────────────────────────────────────────────────────

  private checkGoprivate(go: string, env: Record<string, string>): Check {
    const name = 'GOPRIVATE';
    const value = env.GOPRIVATE ?? '';
    if (coversHost(value)) {
      return { name, state: 'pass', detail: vscode.l10n.t('{0} — the IT 2.0 modules bypass the public proxy', value) };
    }
    const merged = mergePattern(value, GOPRIVATE_PATTERN);
    return {
      name,
      state: 'fail',
      detail: value
        ? vscode.l10n.t(
            '{0} does not cover {1}. Every go get of an IT 2.0 module goes to the public proxy, which cannot see it.',
            value,
            GITLAB_HOST,
          )
        : vscode.l10n.t(
            'unset. Every go get of an IT 2.0 module goes to the public proxy, which cannot see it.',
          ),
      fix: {
        label: vscode.l10n.t('Set GOPRIVATE'),
        run: () => this.goEnvWrite(go, 'GOPRIVATE', merged),
      },
    };
  }

  /**
   * Private modules must be excluded from checksum verification.
   *
   * The variable that does this has been spelled `GONOSUMDB` and `GONOSUMCHECK`
   * across toolchain generations, so both are read from wherever the toolchain
   * reports them rather than assuming one exists — and the *remedy* is always
   * `GOPRIVATE`, which every supported toolchain understands and which is the
   * documented default for the no-proxy and no-sumdb sets. Writing a variable
   * this Go might not know would fail with "unknown go command variable", which
   * is a worse outcome than the problem.
   */
  private checkSumVerification(go: string, env: Record<string, string>): Check {
    const name = vscode.l10n.t('Checksum verification');
    const noSum = env.GONOSUMDB || env.GONOSUMCHECK || process.env.GONOSUMDB || process.env.GONOSUMCHECK || '';
    const goflags = env.GOFLAGS ?? '';
    const sumdb = env.GOSUMDB ?? '';

    if (sumdb === 'off') {
      return {
        name,
        state: 'warn',
        detail: vscode.l10n.t(
          'GOSUMDB=off disables checksum verification for every module, not only the private ones. GOPRIVATE={0} is the narrower setting.',
          GOPRIVATE_PATTERN,
        ),
        fix: {
          label: vscode.l10n.t('Set GOPRIVATE instead'),
          run: () => this.goEnvWrite(go, 'GOPRIVATE', mergePattern(env.GOPRIVATE ?? '', GOPRIVATE_PATTERN)),
        },
      };
    }

    if (coversHost(env.GOPRIVATE ?? '') || coversHost(noSum)) {
      const via = coversHost(env.GOPRIVATE ?? '') ? 'GOPRIVATE' : 'GONOSUMDB';
      return {
        name,
        state: 'pass',
        detail: goflags
          ? vscode.l10n.t('{0} excludes {1} from sum.golang.org (GOFLAGS={2})', via, GITLAB_HOST, goflags)
          : vscode.l10n.t('{0} excludes {1} from sum.golang.org', via, GITLAB_HOST),
      };
    }

    return {
      name,
      state: 'fail',
      detail: vscode.l10n.t(
        'Nothing excludes {0} from sum.golang.org, so go mod download will fail verification on every IT 2.0 module.',
        GITLAB_HOST,
      ),
      fix: {
        label: vscode.l10n.t('Set GOPRIVATE'),
        run: () => this.goEnvWrite(go, 'GOPRIVATE', mergePattern(env.GOPRIVATE ?? '', GOPRIVATE_PATTERN)),
      },
    };
  }

  /**
   * Prove the developer can actually fetch a private module.
   *
   * `GOPRIVATE` being right and git being able to authenticate are different
   * facts, and only the second one is what fails at 5 p.m. on day one. A real
   * `ls-remote` is the only honest test.
   */
  private async checkGitCredential(): Promise<Check> {
    const name = vscode.l10n.t('Git credential for {0}', GITLAB_HOST);
    const restore = suppressGitPrompts();
    let probe: ProbeResult = { ok: false, out: '', missing: false };
    try {
      // Named with its extension on Windows: spawn runs without a shell, and
      // an extensionless name is the case that behaves differently there.
      probe = await this.probe(binaryName('git'), ['ls-remote', '--heads', PROBE_REPO, 'HEAD']);
    } finally {
      restore();
    }

    if (probe.ok) {
      return { name, state: 'pass', detail: vscode.l10n.t('a probe fetch of it-2.0-common/n-api-server succeeded') };
    }
    if (probe.missing) {
      return {
        name,
        state: 'fail',
        detail: vscode.l10n.t('git could not be run. The Go toolchain fetches private modules through git.'),
      };
    }
    return {
      name,
      state: 'fail',
      detail: vscode.l10n.t(
        'a probe fetch of it-2.0-common/n-api-server failed: {0}. Go cannot download any IT 2.0 module until git can authenticate.',
        firstLine(probe.error ?? ''),
      ),
      fix: {
        label: vscode.l10n.t('Show how to authenticate'),
        run: () => this.offerGitCredential(),
      },
    };
  }

  /**
   * Hand over the exact `git config` line — on the clipboard, never executed.
   *
   * The HTTPS form embeds a personal access token, and running it for the
   * developer would put that token into this process's argv, the log, and any
   * crash report that follows. Copying it means the secret only ever exists
   * where the developer put it.
   */
  private async offerGitCredential(): Promise<void> {
    interface Option extends vscode.QuickPickItem {
      line: string;
      openTokenPage: boolean;
    }
    const options: Option[] = [
      {
        label: vscode.l10n.t('Personal access token over HTTPS'),
        detail: vscode.l10n.t('Works everywhere, including behind the corporate proxy. Needs read_api and read_repository.'),
        line: `git config --global url."https://oauth2:<TOKEN>@${GITLAB_HOST}/".insteadOf "https://${GITLAB_HOST}/"`,
        openTokenPage: true,
      },
      {
        label: vscode.l10n.t('SSH key'),
        detail: vscode.l10n.t('Needs port 22 open to the GitLab host, which some office networks block.'),
        line: `git config --global url."git@${GITLAB_HOST}:".insteadOf "https://${GITLAB_HOST}/"`,
        openTokenPage: false,
      },
    ];

    const picked = await vscode.window.showQuickPick(options, {
      title: vscode.l10n.t('How do you authenticate to {0}?', GITLAB_HOST),
      placeHolder: vscode.l10n.t('The matching git config line goes on your clipboard'),
    });
    if (!picked) return;

    await vscode.env.clipboard.writeText(picked.line);
    if (picked.openTokenPage) {
      await vscode.env.openExternal(vscode.Uri.parse(TOKEN_PAGE));
      void vscode.window.showInformationMessage(
        vscode.l10n.t(
          'The git config line is on your clipboard. Replace <TOKEN> with the token you create, then run it — dakcoder never handles the token itself.',
        ),
      );
      return;
    }
    void vscode.window.showInformationMessage(
      vscode.l10n.t('The git config line is on your clipboard. Run it once, then run Doctor again.'),
    );
  }

  // ── Go tools ──────────────────────────────────────────────────────────────

  private async checkTools(env: Record<string, string>, root: string | undefined): Promise<Check[]> {
    const gopls = await this.checkGopls(env);
    const govalid = await this.checkTool({
      name: 'govalid',
      env,
      versionArgs: ['--version'],
      optional: false,
      pkg: GOVALID_PACKAGE,
      missing: vscode.l10n.t(
        'not installed. Request validators are generated by govalid; without it the agent cannot regenerate them after a validate tag changes.',
      ),
    });
    const golangci = await this.checkTool({
      name: 'golangci-lint',
      env,
      versionArgs: ['--version'],
      optional: true,
      pkg: GOLANGCI_PACKAGE,
      missing: vscode.l10n.t('not installed. Lint is advisory, so the gate skips it rather than failing.'),
    });
    const govulncheck = await this.checkTool({
      name: 'govulncheck',
      env,
      versionArgs: ['-version'],
      optional: true,
      pkg: GOVULNCHECK_PACKAGE,
      missing: vscode.l10n.t('not installed. The vulnerability stage is skipped rather than failed.'),
    });
    const buf = await this.checkBuf(env, root);
    return [gopls, govalid, golangci, govulncheck, buf];
  }

  /**
   * gopls is discovered, never bundled.
   *
   * A bundled gopls that disagrees with the developer's toolchain version
   * produces wrong analysis rather than no analysis, and wrong analysis is far
   * harder to notice. Preference order: the explicit override, then the
   * golang.go extension's configured copy, then GOBIN/GOPATH, then PATH.
   */
  private async checkGopls(env: Record<string, string>): Promise<Check> {
    const name = 'gopls';
    const override = configString('dakcoder.goplsPath');
    const alternate = vscode.workspace.getConfiguration('go').get<Record<string, string>>('alternateTools')?.gopls;
    const candidate =
      override || alternate || (await this.locate('gopls', env)) || binaryName('gopls');

    const probe = await this.probe(candidate, ['version']);
    if (probe.ok) {
      return {
        name,
        state: 'pass',
        detail: vscode.l10n.t('{0} — {1}', candidate, firstLine(probe.out) || vscode.l10n.t('version not reported')),
      };
    }
    return {
      name,
      state: 'warn',
      detail: vscode.l10n.t(
        'not found. go_symbols and go_diagnostics are unavailable, so the agent falls back to reading files — slower, and it costs context.',
      ),
      fix: {
        label: vscode.l10n.t('Install gopls'),
        run: () => this.goInstall(GOPLS_PACKAGE, 'gopls'),
      },
    };
  }

  private async checkTool(opts: {
    name: string;
    env: Record<string, string>;
    versionArgs: string[];
    optional: boolean;
    pkg: string;
    missing: string;
  }): Promise<Check> {
    const found = (await this.locate(opts.name, opts.env)) ?? binaryName(opts.name);
    const probe = await this.probe(found, opts.versionArgs);
    if (probe.ok) {
      return {
        name: opts.name,
        state: 'pass',
        // Some of these binaries answer no version flag at all. Saying so beats
        // printing an empty string that reads like a truncated report.
        detail: vscode.l10n.t('{0} — {1}', found, firstLine(probe.out) || vscode.l10n.t('version not reported')),
      };
    }
    if (!probe.missing) {
      return {
        name: opts.name,
        state: 'warn',
        detail: vscode.l10n.t('{0} is installed but did not answer {1}', found, opts.versionArgs.join(' ')),
      };
    }
    return {
      name: opts.name,
      state: opts.optional ? 'warn' : 'fail',
      detail: opts.missing,
      fix: {
        label: vscode.l10n.t('Install {0}', opts.name),
        run: () => this.goInstall(opts.pkg, opts.name),
      },
    };
  }

  /** buf only matters where protobuf does; asking for it elsewhere is noise. */
  private async checkBuf(env: Record<string, string>, root: string | undefined): Promise<Check> {
    const name = 'buf';
    if (!root || !(await exists(path.join(root, 'buf.yaml')))) {
      return { name, state: 'skip', detail: vscode.l10n.t('no buf.yaml in this workspace — not needed') };
    }
    return this.checkTool({
      name,
      env,
      versionArgs: ['--version'],
      optional: false,
      pkg: BUF_PACKAGE,
      missing: vscode.l10n.t('not installed, and this module has a buf.yaml — protobuf generation will fail.'),
    });
  }

  // ── the module under work ─────────────────────────────────────────────────

  private async checkModule(root: string | undefined): Promise<Check[]> {
    const identity = vscode.l10n.t('Module identity');
    const generation = vscode.l10n.t('Template generation');
    if (!root) {
      const reason = vscode.l10n.t('no folder is open');
      return [skipped(identity, reason), skipped(generation, reason)];
    }

    const goModPath = path.join(root, 'go.mod');
    let text: string;
    try {
      text = await fsp.readFile(goModPath, 'utf8');
    } catch {
      const reason = vscode.l10n.t('no go.mod here. dakcoder works on a Go module built on the n-api-template.');
      return [{ name: identity, state: 'warn', detail: reason }, skipped(generation, reason)];
    }

    const modulePath = /^\s*module\s+(\S+)/m.exec(text)?.[1];
    const declared = /^\s*go\s+(\d+(?:\.\d+)*)/m.exec(text)?.[1];
    const identityCheck: Check = modulePath
      ? {
          name: identity,
          state: 'pass',
          detail: declared
            ? vscode.l10n.t('{0} — declares go {1}', modulePath, declared)
            : vscode.l10n.t('{0} — no go directive', modulePath),
        }
      : { name: identity, state: 'warn', detail: vscode.l10n.t('go.mod has no module directive') };

    // The api-* → n-api-* library swap is the single most reliable signal that
    // a service predates the template. Detected from go.mod rather than from
    // imports: go.mod is one file and cannot disagree with itself.
    const legacy = new RegExp(`${escapeRegExp(GITLAB_HOST)}/it-2\\.0-common/(api-[\\w-]+)`, 'g');
    const legacyLibs = [...new Set([...text.matchAll(legacy)].map((m) => m[1]))];
    const modern = new RegExp(`${escapeRegExp(GITLAB_HOST)}/it-2\\.0-common/(n-api-[\\w-]+)`, 'g');
    const modernLibs = [...new Set([...text.matchAll(modern)].map((m) => m[1]))];

    let generationCheck: Check;
    if (legacyLibs.length) {
      generationCheck = {
        name: generation,
        state: 'warn',
        detail: vscode.l10n.t(
          'this module uses the api-* library generation ({0}). The template is n-api-*, and the rules the agent enforces assume it.',
          legacyLibs.join(', '),
        ),
        fix: {
          label: vscode.l10n.t('Audit legacy patterns'),
          run: () => runCommandIfPresent('dakcoder.auditLegacyPatterns'),
        },
      };
    } else if (modernLibs.length) {
      generationCheck = {
        name: generation,
        state: 'pass',
        detail: vscode.l10n.t('n-api-* ({0})', modernLibs.join(', ')),
      };
    } else {
      generationCheck = {
        name: generation,
        state: 'skip',
        detail: vscode.l10n.t('no it-2.0-common libraries in go.mod — not a template service'),
      };
    }
    return [identityCheck, generationCheck];
  }

  // ── the local runtime ─────────────────────────────────────────────────────

  private async checkRuntime(): Promise<Check[]> {
    const health = vscode.l10n.t('dakcoderd');
    const model = vscode.l10n.t('Model endpoint');

    try {
      await this.deps.runtime.ensure();
      const info = await this.deps.runtime.client.health();
      const sessions =
        info.sessions.total === 1
          ? vscode.l10n.t('1 session known')
          : vscode.l10n.t('{0} sessions known', info.sessions.total);
      const healthCheck: Check =
        info.api_version === API_VERSION
          ? {
              name: health,
              state: 'pass',
              // The API version is what matters across this seam, not the build
              // number: the .vsix and the wheel ship through different channels
              // and will legitimately differ.
              detail: vscode.l10n.t(
                'v{0} on port {1}, speaking API {2} — {3}',
                info.version,
                this.deps.runtime.port ?? 0,
                info.api_version,
                sessions,
              ),
            }
          : {
              name: health,
              state: 'fail',
              detail: vscode.l10n.t(
                'the runtime speaks API {0}; this extension speaks {1}. Everything will half-work until they match.',
                info.api_version,
                API_VERSION,
              ),
              fix: {
                label: vscode.l10n.t('Restart the runtime'),
                run: async () => {
                  this.deps.runtime.dispose();
                  await this.deps.runtime.ensure();
                },
              },
            };

      const modelCheck: Check = info.ready.prewarmed
        ? {
            name: model,
            state: 'pass',
            detail:
              info.ready.latency_ms === undefined
                ? vscode.l10n.t('reachable through {0} — prewarmed', info.gateway)
                : vscode.l10n.t(
                    'reachable through {0} — prewarmed in {1} ms',
                    info.gateway,
                    String(Math.round(info.ready.latency_ms)),
                  ),
          }
        : {
            name: model,
            state: 'warn',
            detail: vscode.l10n.t(
              'not prewarmed: {0}. The first request pays the cold start. Proxy variables in force: {1}',
              info.ready.reason ?? vscode.l10n.t('no reason reported'),
              proxySummary(),
            ),
          };

      return [healthCheck, modelCheck, this.checkPosture()];
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const remedy = (err as { remedy?: string }).remedy;
      return [
        {
          name: health,
          state: 'fail',
          detail: remedy
            ? vscode.l10n.t('{0} — {1}', firstLine(message), remedy)
            : firstLine(message),
          fix: {
            label: vscode.l10n.t('Restart the runtime'),
            run: async () => {
              this.deps.runtime.dispose();
              await this.deps.runtime.ensure();
            },
          },
        },
        skipped(model, vscode.l10n.t('the runtime is not running')),
        this.checkPosture(),
      ];
    }
  }

  /**
   * The line that must read "model access: via gateway (no local key)".
   *
   * Anything else here is a defect, and surfacing it in a report developers
   * paste into support chats is how we would find out — an invariant nobody
   * checks is a comment. The runtime deletes these variables from its child
   * environment, so one sitting in the developer's shell is a warning about
   * their machine, not a breach of the invariant.
   */
  private checkPosture(): Check {
    const name = vscode.l10n.t('Credential posture');
    const posture = vscode.l10n.t('model access: via gateway (no local key)');

    // Reading an unregistered setting on purpose: it is not contributed, so the
    // only way it exists is that someone added it by hand, and that is exactly
    // the unmetered bypass this check is looking for.
    const settings = vscode.workspace.getConfiguration('dakcoder');
    const forbiddenSettings = ['modelApiKey', 'modelBaseUrl'].filter((key) => !!settings.get<string>(key));
    if (forbiddenSettings.length) {
      return {
        name,
        state: 'fail',
        detail: vscode.l10n.t(
          'dakcoder.{0} is set in your settings. There is no such setting: a client-side model key or URL would bypass quota and audit. Remove it.',
          forbiddenSettings[0],
        ),
      };
    }

    // Matched by shape, not enumerated: a role can carry its own endpoint and
    // key (`DAKCODER_MODEL_PLANNER_API_KEY`), so a fixed list would go quietly
    // out of date every time an operator adds a role.
    const inShell = Object.keys(process.env).filter(
      (key) =>
        !!process.env[key] &&
        (/^DAKCODER_MODEL(_|$)/.test(key) ||
          ['OPENAI_API_KEY', 'LITELLM_API_KEY', 'ANTHROPIC_API_KEY', 'AZURE_OPENAI_API_KEY'].includes(
            key,
          )),
    );

    if (inShell.length) {
      return {
        name,
        state: 'warn',
        detail: vscode.l10n.t(
          '{0}, but {1} is set in this shell. It is deleted from the runtime environment at spawn; it belongs to another tool.',
          posture,
          inShell[0],
        ),
      };
    }
    return { name, state: 'pass', detail: posture };
  }

  // ── gateway, auth, quota ──────────────────────────────────────────────────

  /**
   * Three separate checks on purpose.
   *
   * "Gateway unreachable", "not signed in" and "quota exhausted" have nothing
   * in common but their symptom, and one merged "connection failed" row sends
   * two thirds of the people who read it down the wrong remedy.
   */
  private async checkGatewayAndIdentity(): Promise<Check[]> {
    const gatewayName = vscode.l10n.t('Gateway');
    const authName = vscode.l10n.t('Sign-in');
    const quotaName = vscode.l10n.t('Quota');
    const checks: Check[] = [];

    let reachable = false;
    try {
      const health = await this.deps.gateway.health();
      reachable = true;
      checks.push({
        name: gatewayName,
        state: 'pass',
        detail: vscode.l10n.t('{0} answered', this.deps.gateway.baseUrl),
      });

      /*
       * Which identity provider the gateway is actually running.
       *
       * `dev` is a local stand-in that accepts any authorization code. A host
       * running it does not publish `/v1/auth/`, because published that is an
       * open door onto the shared model budget — so sign-in cannot complete and
       * credentials are minted by an administrator instead.
       *
       * This is a warning rather than a failure: the deployment is working as
       * designed, and the developer's job is to know it, not to fix it. Without
       * the row, "Sign in" failing looks like a bug in the extension.
       */
      const identity = identityOf(health);
      if (identity === 'dev') {
        checks.push({
          name: vscode.l10n.t('Identity provider'),
          state: 'warn',
          detail: vscode.l10n.t(
            'the gateway is using a local development identity provider, so sign-in is not published on this host. Ask an administrator for a token and run "dakcoder: Enter Gateway Token".',
          ),
          fix: {
            label: vscode.l10n.t('Enter a token'),
            run: () => runCommandIfPresent('dakcoder.enterToken'),
          },
        });
      } else if (identity) {
        checks.push({
          name: vscode.l10n.t('Identity provider'),
          state: 'pass',
          detail: vscode.l10n.t('{0}', identity),
        });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      checks.push({
        name: gatewayName,
        state: 'fail',
        detail: vscode.l10n.t(
          '{0} did not answer: {1}. The agent proxies every model call through the gateway, so it cannot run offline — by design. Proxy variables in force: {2}',
          this.deps.gateway.baseUrl,
          firstLine(redact(message)),
          proxySummary(),
        ),
      });
    }

    const signIn: Remedy = {
      label: vscode.l10n.t('Sign in'),
      run: () => runCommandIfPresent('dakcoder.signIn'),
    };

    let auth: AuthSnapshot = { signedIn: false };
    try {
      auth = await this.deps.auth();
    } catch (err) {
      this.log.warn(`auth snapshot failed: ${redact(String(err))}`);
    }

    if (!auth.signedIn) {
      checks.push({
        name: authName,
        state: 'fail',
        detail: vscode.l10n.t('not signed in. dakcoder runs as you, so quota and audit are yours.'),
        fix: signIn,
      });
      checks.push(skipped(quotaName, vscode.l10n.t('needs a signed-in session')));
      return checks;
    }

    const account = auth.account ?? vscode.l10n.t('unknown account');
    // Roles come from the gateway's token, and an empty list is a real answer.
    // Inventing a default role here would misreport what the server will allow.
    const roles = auth.roles?.length
      ? auth.roles.join(', ')
      : vscode.l10n.t('no roles reported');
    const secondsLeft = auth.expiresAt === undefined ? undefined : Math.round((auth.expiresAt - Date.now()) / 1000);

    if (secondsLeft !== undefined && secondsLeft <= 0) {
      checks.push({
        name: authName,
        state: 'warn',
        detail: vscode.l10n.t('{0} — the access token has expired; it refreshes on the next request', account),
        fix: signIn,
      });
    } else {
      checks.push({
        name: authName,
        state: 'pass',
        detail:
          secondsLeft === undefined
            ? vscode.l10n.t('{0} — roles: {1}', account, roles)
            : vscode.l10n.t('{0} — token valid for {1}, roles: {2}', account, humanDuration(secondsLeft), roles),
      });
    }

    if (!reachable) {
      checks.push(skipped(quotaName, vscode.l10n.t('the gateway did not answer')));
      return checks;
    }

    try {
      const quota = await this.deps.gateway.quota();
      // `tightest` is the server's own answer to "which limit bites first".
      // Nothing here estimates what a task will cost: no such number is on the
      // wire, and a client-side guess would be worse than no guess at all.
      const tightest = quota.tightest;
      let detail: string;
      if (!tightest) {
        detail = vscode.l10n.t('no limit is close to biting');
      } else if (quota.role) {
        detail = vscode.l10n.t(
          '{0}: {1} of {2} used ({3}%) — role {4}',
          tightest.name,
          tightest.used,
          tightest.cap,
          Math.round(tightest.pct),
          quota.role,
        );
      } else {
        detail = vscode.l10n.t(
          '{0}: {1} of {2} used ({3}%)',
          tightest.name,
          tightest.used,
          tightest.cap,
          Math.round(tightest.pct),
        );
      }
      checks.push({
        name: quotaName,
        state: tightest && tightest.pct >= 95 ? 'warn' : 'pass',
        detail,
      });
    } catch (err) {
      if (err instanceof HttpError && err.isQuota) {
        checks.push({
          name: quotaName,
          state: 'fail',
          // The server writes this sentence; the client must not paraphrase it
          // and must never render the JSON body.
          detail: err.retryAfter
            ? vscode.l10n.t('{0} Retry in {1}.', err.detail, humanDuration(err.retryAfter))
            : err.detail,
        });
      } else {
        const message = err instanceof Error ? err.message : String(err);
        checks.push({ name: quotaName, state: 'warn', detail: firstLine(redact(message)) });
      }
    }
    return checks;
  }

  // ── environment ───────────────────────────────────────────────────────────

  /**
   * A proxy without a loopback exclusion sends 127.0.0.1 through the corporate
   * proxy, which refuses it. The symptom is a runtime that starts and cannot be
   * reached, which reads as a crash — and the developer spends the afternoon
   * debugging the wrong process.
   */
  private checkProxy(): Check {
    const name = vscode.l10n.t('Proxy');
    const http = process.env.HTTP_PROXY ?? process.env.http_proxy ?? '';
    const https = process.env.HTTPS_PROXY ?? process.env.https_proxy ?? '';
    const noProxy = process.env.NO_PROXY ?? process.env.no_proxy ?? '';

    if (!http && !https) {
      return { name, state: 'pass', detail: vscode.l10n.t('no proxy variables set') };
    }
    if (noProxy.includes('127.0.0.1')) {
      return {
        name,
        state: 'pass',
        detail: vscode.l10n.t('{0}, and NO_PROXY excludes the loopback', redact(https || http)),
      };
    }

    const merged = [noProxy, '127.0.0.1', 'localhost'].filter(Boolean).join(',');
    const persist =
      process.platform === 'win32'
        ? `setx NO_PROXY "${merged}"`
        : `export NO_PROXY="${merged}"`;
    return {
      name,
      state: 'warn',
      detail: vscode.l10n.t(
        '{0} is set but NO_PROXY does not contain 127.0.0.1. Requests to the local runtime go to the proxy, which refuses them.',
        redact(https || http),
      ),
      fix: {
        label: vscode.l10n.t('Exclude the loopback'),
        run: async () => {
          // The runtime already patches its own child environment per spawn;
          // this fixes the extension host itself, which is what talks to
          // 127.0.0.1 — and hands over the line that makes it survive a reboot.
          process.env.NO_PROXY = merged;
          await vscode.env.clipboard.writeText(persist);
          void vscode.window.showInformationMessage(
            vscode.l10n.t(
              'The loopback is excluded for this window. To make it permanent, the command is on your clipboard.',
            ),
          );
        },
      },
    };
  }

  // ── subprocess helpers ────────────────────────────────────────────────────

  private goBinary(): string {
    return configString('dakcoder.goPath') || binaryName('go');
  }

  /**
   * The remedy for "no usable Go": point the extension at one.
   *
   * On a managed India Post machine Go is routinely installed somewhere that is
   * not on PATH — the developer has it and the extension cannot see it, which
   * is a different problem from not having it and has a different fix. The
   * internal distribution's URL is deployment-specific and no server field
   * carries it, so this offers the half we can actually do rather than opening
   * a link we would have to invent.
   */
  private pointAtGoBinary(): Remedy {
    return {
      label: vscode.l10n.t('Choose the go binary'),
      run: async () => {
        const picked = await vscode.window.showOpenDialog({
          title: vscode.l10n.t('Select the go binary'),
          openLabel: vscode.l10n.t('Use this go'),
          canSelectMany: false,
        });
        const file = picked?.[0]?.fsPath;
        if (!file) return;
        const probe = await this.probe(file, ['version']);
        if (!probe.ok) {
          throw new Error(vscode.l10n.t('{0} did not answer "go version"', file));
        }
        await vscode.workspace
          .getConfiguration('dakcoder')
          .update('goPath', file, vscode.ConfigurationTarget.Global);
        this.log.info(`dakcoder.goPath set to ${file}`);
      },
    };
  }

  private async goEnv(go: string): Promise<Record<string, string>> {
    // The full map, with no variable names as arguments: an unknown name is an
    // error on some toolchains and an empty string on others, and this file must
    // not care which Go the developer has.
    const probe = await this.probe(go, ['env', '-json']);
    if (!probe.ok) return {};
    try {
      const parsed = JSON.parse(probe.out) as Record<string, unknown>;
      const env: Record<string, string> = {};
      for (const [key, value] of Object.entries(parsed)) {
        if (typeof value === 'string') env[key] = value;
      }
      return env;
    } catch {
      this.log.warn('go env -json did not return JSON');
      return {};
    }
  }

  private async goEnvWrite(go: string, key: string, value: string): Promise<void> {
    await this.probeOrThrow(go, ['env', '-w', `${key}=${value}`]);
    this.log.info(`go env -w ${key}=${value}`);
  }

  private async goInstall(pkg: string, label: string): Promise<void> {
    const go = this.goBinary();
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: vscode.l10n.t('Installing {0}…', label),
        cancellable: false,
      },
      // `go install` reaches the network; behind the corporate proxy this is the
      // slowest remedy offered, which is why it gets a visible progress item.
      () => this.probeOrThrow(go, ['install', pkg], workspaceRoot()),
    );
  }

  /** Where a `go install`ed binary actually lands, before falling back to PATH. */
  private async locate(tool: string, env: Record<string, string>): Promise<string | undefined> {
    const name = binaryName(tool);
    const dirs = [env.GOBIN, env.GOPATH ? path.join(env.GOPATH, 'bin') : undefined].filter(
      (dir): dir is string => !!dir,
    );
    for (const dir of dirs) {
      const candidate = path.join(dir, name);
      if (await exists(candidate)) return candidate;
    }
    return undefined;
  }

  private async probeOrThrow(command: string, args: string[], cwd?: string): Promise<void> {
    const probe = await this.probe(command, args, cwd);
    if (!probe.ok) throw new Error(probe.error ?? `${command} failed`);
  }

  private async probe(command: string, args: string[], cwd?: string): Promise<ProbeResult> {
    this.log.debug(`$ ${command} ${args.join(' ')}`);
    try {
      const out = await withTimeout(run(command, args, this.log, cwd), PROBE_TIMEOUT_MS, command);
      // The raw evidence, redacted: a developer who pastes the log has the same
      // right to keep their proxy password as one who pastes the report.
      this.log.trace(redact(out.trimEnd()) || '(no output)');
      return { ok: true, out, missing: false };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const code = (err as NodeJS.ErrnoException).code;
      this.log.warn(`${command} ${args.join(' ')} → ${redact(firstLine(message))}`);
      return { ok: false, out: '', error: message, missing: code === 'ENOENT' };
    }
  }

  // ── the report ────────────────────────────────────────────────────────────

  private renderReport(report: DoctorReport): string {
    const extension = vscode.extensions.getExtension(EXTENSION_ID);
    const version = typeof extension?.packageJSON?.version === 'string' ? extension.packageJSON.version : 'dev';
    // Singular and plural are separate strings throughout: vscode.l10n has no
    // ICU plurals, and "1 checks failing" at the top of a report a developer is
    // about to paste into a support chat reads as a bug in the tool.
    const failedText =
      report.failed === 1
        ? vscode.l10n.t('1 check failing')
        : vscode.l10n.t('{0} checks failing', report.failed);
    const warnedText =
      report.warned === 1 ? vscode.l10n.t('1 warning') : vscode.l10n.t('{0} warnings', report.warned);
    const lines: string[] = [
      vscode.l10n.t('dakcoder Doctor report'),
      vscode.l10n.t('generated: {0}', report.at.toISOString()),
      vscode.l10n.t('extension: {0} · VS Code {1} · {2} {3}', version, vscode.version, process.platform, process.arch),
      vscode.l10n.t('result: {0}, {1}', failedText, warnedText),
      '',
    ];
    for (const section of report.sections) {
      lines.push(`## ${section.title}`);
      for (const check of section.checks) {
        lines.push(`- [${stateWord(check.state)}] ${check.name}: ${redact(check.detail)}`);
      }
      lines.push('');
    }
    lines.push(
      vscode.l10n.t(
        'Credentials inside proxy URLs are replaced with ***. This report contains no tokens.',
      ),
    );
    return lines.join('\n');
  }
}

// ── the tree ────────────────────────────────────────────────────────────────

type DoctorNode = Section | Check;

function isSection(node: DoctorNode): node is Section {
  return (node as Section).checks !== undefined;
}

class DoctorTree implements vscode.TreeDataProvider<DoctorNode> {
  private readonly changed = new vscode.EventEmitter<DoctorNode | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  private report?: DoctorReport;

  set(report: DoctorReport): void {
    this.report = report;
    this.changed.fire(undefined);
  }

  getChildren(node?: DoctorNode): DoctorNode[] {
    if (!node) return this.report?.sections ?? [];
    return isSection(node) ? node.checks : [];
  }

  getTreeItem(node: DoctorNode): vscode.TreeItem {
    if (isSection(node)) {
      const item = new vscode.TreeItem(node.title, vscode.TreeItemCollapsibleState.Expanded);
      const failed = node.checks.filter((c) => c.state === 'fail').length;
      const warned = node.checks.filter((c) => c.state === 'warn').length;
      item.description =
        failed > 0
          ? failed === 1
            ? vscode.l10n.t('1 failing')
            : vscode.l10n.t('{0} failing', failed)
          : warned > 0
            ? warned === 1
              ? vscode.l10n.t('1 warning')
              : vscode.l10n.t('{0} warnings', warned)
            : vscode.l10n.t('ok');
      return item;
    }

    const item = new vscode.TreeItem(node.name, vscode.TreeItemCollapsibleState.None);
    // The state word rides in the description as well as the icon. Colour and
    // glyph alone would leave a screen-reader user, and anyone in a high
    // contrast theme, reading the detail with no idea whether it is good news.
    item.description = `${stateWord(node.state)} · ${node.detail}`;
    item.tooltip = new vscode.MarkdownString(`**${node.name}** — ${stateWord(node.state)}\n\n${node.detail}`);
    item.iconPath = stateIcon(node.state);
    item.accessibilityInformation = {
      label: vscode.l10n.t('{0}, {1}. {2}', node.name, stateWord(node.state), node.detail),
    };
    // The inline remedy button hangs off this, and only rows that have one get
    // it — an "Apply fix" that does nothing is worse than no button.
    if (node.fix) item.contextValue = 'dakcoder.check.fixable';
    return item;
  }
}

// ── registration ────────────────────────────────────────────────────────────

/**
 * Wire Doctor into the extension. Returns the instance so activation can call
 * `run({reveal: true})` for the `dakcoder.doctor` command — which the activation
 * file owns — and `preflight()` before the first task of the day.
 */
export function register(context: vscode.ExtensionContext, deps: DoctorDeps): Doctor {
  const doctor = new Doctor(deps, context.globalState);
  context.subscriptions.push(
    doctor,
    vscode.commands.registerCommand('dakcoder.doctor.refresh', () => doctor.run()),
    vscode.commands.registerCommand('dakcoder.doctor.copyReport', () => doctor.copyReport()),
    vscode.commands.registerCommand('dakcoder.doctor.showLog', () => doctor.log.show(true)),
    vscode.commands.registerCommand('dakcoder.doctor.applyFix', (node?: DoctorNode) =>
      doctor.applyFix(node && !isSection(node) ? node : undefined),
    ),
  );
  return doctor;
}

// ── plain helpers ───────────────────────────────────────────────────────────

interface ProbeResult {
  ok: boolean;
  out: string;
  error?: string;
  /** The binary itself was not found, as opposed to running and failing. */
  missing: boolean;
}

/**
 * Which identity provider the gateway reports, or `undefined` if it says
 * nothing.
 *
 * Read from two places because the field has lived in both: nested under
 * `capabilities` on the deployment at `ai.cept.gov.in/dakcoder`, and top-level
 * on older builds. `/v1/health` is additive-only (C2), so a gateway that
 * reports neither is a gateway that has not been taught to — which is silence,
 * not "no identity provider", and the caller draws no row for it.
 */
function identityOf(health: Record<string, unknown>): string | undefined {
  const top = health.identity;
  if (typeof top === 'string' && top) return top;
  const caps = health.capabilities;
  if (caps && typeof caps === 'object') {
    const nested = (caps as Record<string, unknown>).identity;
    if (typeof nested === 'string' && nested) return nested;
  }
  return undefined;
}

function skipped(name: string, detail: string): Check {
  return { name, state: 'skip', detail };
}

function goMissing(): string {
  return vscode.l10n.t('the Go toolchain has to be working first');
}

function stateWord(state: CheckState): string {
  switch (state) {
    case 'pass':
      return vscode.l10n.t('pass');
    case 'fail':
      return vscode.l10n.t('fail');
    case 'warn':
      return vscode.l10n.t('warn');
    default:
      // Additive by habit: an unrecognised state is a row that still renders.
      return vscode.l10n.t('skipped');
  }
}

/** Every colour is a theme id, so it holds in high contrast and in light themes. */
function stateIcon(state: CheckState): vscode.ThemeIcon {
  switch (state) {
    case 'pass':
      return new vscode.ThemeIcon('pass', new vscode.ThemeColor('testing.iconPassed'));
    case 'fail':
      return new vscode.ThemeIcon('error', new vscode.ThemeColor('problemsErrorIcon.foreground'));
    case 'warn':
      return new vscode.ThemeIcon('warning', new vscode.ThemeColor('problemsWarningIcon.foreground'));
    default:
      return new vscode.ThemeIcon('circle-slash', new vscode.ThemeColor('disabledForeground'));
  }
}

function configString(key: string): string {
  const dot = key.lastIndexOf('.');
  return vscode.workspace.getConfiguration(key.slice(0, dot)).get<string>(key.slice(dot + 1))?.trim() ?? '';
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

function binaryName(tool: string): string {
  return process.platform === 'win32' ? `${tool}.exe` : tool;
}

async function exists(file: string): Promise<boolean> {
  try {
    await fsp.stat(file);
    return true;
  } catch {
    return false;
  }
}

/**
 * Fire a command only if something registered it.
 *
 * Doctor's remedies reach into surfaces other modules own, and a missing
 * command must degrade to a message rather than throwing an unhandled rejection
 * inside a one-click fix.
 */
async function runCommandIfPresent(command: string): Promise<void> {
  const all = await vscode.commands.getCommands(true);
  if (!all.includes(command)) {
    void vscode.window.showWarningMessage(
      vscode.l10n.t('That action is not available in this build ({0}).', command),
    );
    return;
  }
  await vscode.commands.executeCommand(command);
}

/**
 * The timeout rejects; the child keeps running.
 *
 * Killing it would need a handle `run()` does not return, and a stuck `git`
 * waiting on a credential prompt exits on its own once the prompt is answered
 * or the terminal closes. Reporting the timeout is what the developer needs;
 * reaping the process is not worth widening the runtime's API for.
 */
function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error(vscode.l10n.t('{0} did not answer within {1} seconds', what, String(ms / 1000)))),
      ms,
    );
    promise.then(resolve, reject).finally(() => clearTimeout(timer));
  });
}

/**
 * Stop git asking for a password on a terminal nobody is watching.
 *
 * `run()` gives no hook for a child environment, so the variables are set on
 * this process for the length of the probe and restored afterwards. Anything
 * else spawned in that window inherits "do not prompt", which is the behaviour
 * we want everywhere anyway.
 */
function suppressGitPrompts(): () => void {
  const previous = {
    GIT_TERMINAL_PROMPT: process.env.GIT_TERMINAL_PROMPT,
    GCM_INTERACTIVE: process.env.GCM_INTERACTIVE,
  };
  process.env.GIT_TERMINAL_PROMPT = '0';
  process.env.GCM_INTERACTIVE = 'never';
  return () => {
    restore('GIT_TERMINAL_PROMPT', previous.GIT_TERMINAL_PROMPT);
    restore('GCM_INTERACTIVE', previous.GCM_INTERACTIVE);
  };
}

function restore(key: string, value: string | undefined): void {
  if (value === undefined) delete process.env[key];
  else process.env[key] = value;
}

/** `go version go1.25.0 windows/amd64` → `[1, 25, 0]`, and `go1.25` → `[1, 25, 0]`. */
export function parseGoVersion(output: string): [number, number, number] | undefined {
  const match = /go(\d+)\.(\d+)(?:\.(\d+))?/.exec(output);
  if (!match) return undefined;
  return [Number(match[1]), Number(match[2]), Number(match[3] ?? 0)];
}

function compareVersions(a: readonly number[], b: readonly number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const diff = (a[i] ?? 0) - (b[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

/**
 * Whether a GOPRIVATE-style pattern list covers the GitLab host.
 *
 * Deliberately generous — `gitlab.cept.gov.in`, `*.cept.gov.in` and
 * `gitlab.cept.gov.in/*` all count. A stricter matcher would tell developers
 * their working configuration is broken, and a false alarm in a preflight is
 * how a preflight gets ignored.
 */
export function coversHost(patterns: string): boolean {
  return patterns
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean)
    .some((pattern) => {
      const host = pattern.split('/')[0];
      if (host === GITLAB_HOST) return true;
      return host.startsWith('*.') && GITLAB_HOST.endsWith(host.slice(1));
    });
}

/** Append rather than replace: the developer's other private hosts stay private. */
function mergePattern(existing: string, pattern: string): string {
  const parts = existing
    .split(',')
    .map((p) => p.trim())
    .filter(Boolean);
  if (!parts.includes(pattern)) parts.push(pattern);
  return parts.join(',');
}

function proxySummary(): string {
  const parts = ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY']
    .map((key) => {
      const value = process.env[key] ?? process.env[key.toLowerCase()];
      return value ? `${key}=${redact(value)}` : undefined;
    })
    .filter((part): part is string => !!part);
  return parts.length ? parts.join(' ') : vscode.l10n.t('none');
}

/**
 * Strip credentials out of any URL before it is logged or copied.
 *
 * A corporate proxy is routinely configured as `http://user:password@proxy`,
 * and the whole point of this report is that it gets pasted into a chat window.
 */
export function redact(text: string): string {
  return text.replace(/(\b[a-z][a-z0-9+.-]*:\/\/)[^/\s:@]+(?::[^/\s@]*)?@/gi, '$1***@');
}

function firstLine(text: string): string {
  const line = text.split(/\r?\n/).find((l) => l.trim());
  return (line ?? text).trim().slice(0, 300);
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Local date, because "the first task of a day" means the developer's day. */
function localDate(date: Date): string {
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${date.getFullYear()}-${month}-${day}`;
}

/**
 * A duration a person can read. Singular and plural are separate strings:
 * `vscode.l10n` has no ICU plurals, so "1 minutes" is what a single format
 * string produces, and it reads as a bug in a report people paste into chat.
 */
export function humanDuration(seconds: number): string {
  if (seconds < 60) {
    const n = Math.max(0, Math.round(seconds));
    return n === 1 ? vscode.l10n.t('1 second') : vscode.l10n.t('{0} seconds', n);
  }
  if (seconds < 3600) {
    const n = Math.round(seconds / 60);
    return n === 1 ? vscode.l10n.t('1 minute') : vscode.l10n.t('{0} minutes', n);
  }
  const n = Math.round(seconds / 3600);
  return n === 1 ? vscode.l10n.t('1 hour') : vscode.l10n.t('{0} hours', n);
}
