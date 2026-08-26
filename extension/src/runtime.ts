/**
 * Finding a Python, building a venv, and spawning `dakcoderd`.
 *
 * **Wheels are installed offline.** The install runs `--no-index --find-links`
 * against a directory of vendored wheels inside the `.vsix`. Resolving
 * dependencies from the network on first run is minutes behind a corporate
 * proxy at best and the documented failure mode at worst, and a first-run
 * experience that *can* fail on the network *will* fail for someone on day one
 * of the pilot. First impressions of an internal tool do not get a second try.
 *
 * **The spawn environment holds no model credential.** The runtime authenticates
 * to the gateway as the developer and never holds a model key; the gateway is
 * the only process that may. A local key would be an unmetered bypass around
 * quota and audit, so the variables are deleted from the child environment here
 * rather than trusted to be absent — including one a developer exported for an
 * unrelated project, which is exactly the case a policy document does not catch.
 *
 * **Port 0, and the port is read from stdout.** Two VS Code windows open at once
 * is normal; a fixed port turns the second into a confusing failure. The runtime
 * binds, listens, then prints `{"port","pid","version"}` before serving, so the
 * parent has the port even if startup then fails.
 */

import { ChildProcess, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';

import { RuntimeClient } from './client';
import { resolveGotools } from './diagnostics';
import { API_VERSION, type Health } from './protocol';

/** Variables that must never reach the child, whatever the developer's shell holds. */
const FORBIDDEN_IN_CHILD = [
  'DAKCODER_MODEL_API_KEY',
  'DAKCODER_MODEL_BASE_URL',
  'OPENAI_API_KEY',
  'LITELLM_API_KEY',
  'ANTHROPIC_API_KEY',
  'AZURE_OPENAI_API_KEY',
] as const;

export interface Announcement {
  port: number;
  pid: number;
  version: string;
}

export class RuntimeError extends Error {
  constructor(
    message: string,
    readonly remedy?: string,
  ) {
    super(message);
    this.name = 'RuntimeError';
  }
}

export interface RuntimeOptions {
  workspace: string;
  gatewayUrl: string;
  jwt: () => string | undefined;
  storage: vscode.Uri;
  extensionPath: string;
  log: vscode.LogOutputChannel;
  pythonPath?: string;
  prewarm?: boolean;
}

export class Runtime implements vscode.Disposable {
  private child?: ChildProcess;
  private announced?: Announcement;
  private starting?: Promise<Announcement>;
  readonly client: RuntimeClient;
  /** Generated per spawn. Authenticates the extension to its own runtime. */
  private readonly loopbackToken = randomToken();

  constructor(private readonly opts: RuntimeOptions) {
    this.client = new RuntimeClient('', () => this.loopbackToken);
  }

  get running(): boolean {
    return !!this.child && this.child.exitCode === null;
  }

  get port(): number | undefined {
    return this.announced?.port;
  }

  /** Idempotent, and safe to call concurrently — two commands racing is normal. */
  async ensure(): Promise<Announcement> {
    if (this.announced && this.running) return this.announced;
    this.starting ??= this.start().finally(() => {
      this.starting = undefined;
    });
    return this.starting;
  }

  private async start(): Promise<Announcement> {
    const python = await this.venvPython();
    const env = this.childEnv(await this.gotools());

    const args = [
      '-m',
      'dakcoder_agent.serve',
      '--workspace',
      this.opts.workspace,
      '--port',
      '0',
    ];
    if (this.opts.prewarm === false) args.push('--no-prewarm');

    this.opts.log.info(`spawning dakcoderd: ${python} ${args.join(' ')}`);
    const child = spawn(python, args, { env, cwd: this.opts.workspace, windowsHide: true });
    this.child = child;

    const announced = await this.readAnnouncement(child);
    this.announced = announced;
    this.client.setBase(`http://127.0.0.1:${announced.port}`);

    const health = await this.waitForHealth();
    this.assertVersion(health);
    this.opts.log.info(
      `dakcoderd ${health.version} ready on ${announced.port} (api ${health.api_version})`,
    );
    return announced;
  }

  /**
   * Read the one JSON line the runtime prints before it serves.
   *
   * Waiting on `/v1/health` instead would be a race we lose on a slow machine:
   * we would not know which port to poll.
   */
  private readAnnouncement(child: ChildProcess): Promise<Announcement> {
    return new Promise((resolve, reject) => {
      let stdout = '';
      let stderr = '';
      const timer = setTimeout(() => {
        reject(
          new RuntimeError(
            'the runtime did not report a port within 60 seconds.',
            'Run "dakcoder: Doctor" — the usual cause is a Python that cannot import the wheel.',
          ),
        );
      }, 60_000);

      const finish = (err?: RuntimeError, value?: Announcement) => {
        clearTimeout(timer);
        if (err) reject(err);
        else resolve(value!);
      };

      child.stdout?.on('data', (chunk: Buffer) => {
        stdout += chunk.toString();
        const newline = stdout.indexOf('\n');
        if (newline === -1) return;
        const line = stdout.slice(0, newline).trim();
        stdout = stdout.slice(newline + 1);
        try {
          const parsed = JSON.parse(line) as Announcement;
          if (typeof parsed.port === 'number') finish(undefined, parsed);
        } catch {
          this.opts.log.warn(`unexpected line on runtime stdout: ${line.slice(0, 200)}`);
        }
      });

      child.stderr?.on('data', (chunk: Buffer) => {
        const text = chunk.toString();
        stderr += text;
        this.opts.log.warn(text.trimEnd());
      });

      child.on('error', (err) =>
        finish(new RuntimeError(`the runtime could not be started: ${err.message}`)),
      );
      child.on('exit', (code) => {
        if (this.announced) return;
        // stderr is the actionable part: the runtime refuses to start with a
        // named reason (a model key present, no gateway URL) and that sentence
        // is worth far more than "exited with 2".
        finish(
          new RuntimeError(
            `the runtime exited with ${code} before reporting a port.`,
            stderr.trim().split('\n').slice(-3).join(' ') || undefined,
          ),
        );
      });
    });
  }

  private async waitForHealth(): Promise<Health> {
    const deadline = Date.now() + 60_000;
    let last: unknown;
    while (Date.now() < deadline) {
      try {
        return await this.client.health();
      } catch (err) {
        last = err;
        await sleep(200);
      }
    }
    throw new RuntimeError(
      `the runtime never became healthy: ${String(last)}`,
      'Run "dakcoder: Doctor".',
    );
  }

  /**
   * Refuse a version mismatch loudly, at connect time.
   *
   * Silent version skew across a client/server boundary is the failure that
   * costs the most support time: everything half-works and nobody suspects the
   * seam.
   */
  private assertVersion(health: Health): void {
    if (health.api_version === API_VERSION) return;
    throw new RuntimeError(
      vscode.l10n.t(
        'This extension speaks runtime API {0}; the runtime reports {1}.',
        API_VERSION,
        health.api_version,
      ),
      vscode.l10n.t('Update the extension, or reinstall the bundled runtime.'),
    );
  }

  /**
   * The verified sidecar path, or nothing.
   *
   * Resolved here rather than left to `diagnostics.ts`'s lazy locator, because
   * that one fires on the first lint and `dakcoder.lintOnSave` defaults to
   * false — the first thing a pilot does is send a chat message, which spawns
   * this child. Only this call site has a happens-before against the daemon's
   * first `repo_map`.
   *
   * A checksum mismatch must not stop the runtime: a daemon without `gotools`
   * still answers, still streams, and reports one clear failure per
   * gotools-backed tool, whereas a runtime that refuses to spawn leaves the
   * developer with no agent and no explanation.
   */
  private async gotools(): Promise<string | undefined> {
    try {
      return await resolveGotools(vscode.Uri.file(this.opts.extensionPath), this.opts.log);
    } catch (err) {
      this.opts.log.error(`gotools was not passed to the runtime: ${String(err)}`);
      return undefined;
    }
  }

  private childEnv(gotools: string | undefined): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = {
      ...process.env,
      DAKCODER_MODE: 'local',
      DAKCODER_GATEWAY_URL: this.opts.gatewayUrl,
      DAKCODER_GATEWAY_TOKEN: this.loopbackToken,
      DAKCODER_VERSION: extensionVersion(this.opts.extensionPath),
      // The sidecar ships inside the `.vsix` under a platform-suffixed name
      // (`bin/gotools-win32-x64.exe`, §4.5) and the runtime is a venv under
      // globalStorage, so the child can reach it neither by PATH — which holds
      // no entry and would be looking for the wrong name anyway — nor by walking
      // up from its own file. §4.6 names this variable; without the hand-off
      // every gotools-backed tool fails on a correct install, starting with the
      // `repo_map` the Planner opens with, so the first thing a developer ever
      // sees is a broken agent. The path is the one `resolveGotools` already
      // verified, so the child honours `dakcoder.gotoolsPath` and never sees a
      // binary the manifest refused.
      ...(gotools ? { GOTOOLS_PATH: gotools } : {}),
      PYTHONUTF8: '1',
      PYTHONIOENCODING: 'utf-8',
    };
    const jwt = this.opts.jwt();
    if (jwt) env.DAKCODER_JWT = jwt;

    for (const name of FORBIDDEN_IN_CHILD) {
      if (env[name]) {
        this.opts.log.warn(
          `${name} is set in this shell and was removed from the runtime's environment. ` +
            'Model traffic goes through the gateway; a local key would bypass quota and audit.',
        );
      }
      delete env[name];
    }

    // A proxy set without a loopback exclusion sends 127.0.0.1 through the
    // corporate proxy, which then refuses it. The symptom is a runtime that
    // starts and cannot be reached, which reads as a crash.
    if ((env.HTTP_PROXY || env.HTTPS_PROXY) && !(env.NO_PROXY ?? '').includes('127.0.0.1')) {
      env.NO_PROXY = [env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(',');
      this.opts.log.info('added 127.0.0.1 to NO_PROXY for the runtime');
    }
    return env;
  }

  // ── the venv ──────────────────────────────────────────────────────────────

  private async venvPython(): Promise<string> {
    const root = path.join(this.opts.storage.fsPath, 'runtime');
    const python =
      process.platform === 'win32'
        ? path.join(root, 'Scripts', 'python.exe')
        : path.join(root, 'bin', 'python');

    const wheels = path.join(this.opts.extensionPath, 'runtime');
    const stamp = path.join(root, '.wheel-hash');
    const wanted = wheelHash(wheels);

    if (fs.existsSync(python) && readIfExists(stamp) === wanted) return python;

    const base = await this.findPython();
    this.opts.log.info(`creating the runtime venv with ${base}`);
    await run(base, ['-m', 'venv', root], this.opts.log);
    await run(python, ['-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check'], this.opts.log).catch(
      () => this.opts.log.warn('pip could not be upgraded; continuing with the bundled one'),
    );

    const agentWheel = firstWheel(wheels, 'dakcoder_agent');
    if (!agentWheel) {
      throw new RuntimeError(
        'the bundled runtime wheels are missing from this build.',
        'Reinstall the .vsix; the runtime/ directory should contain dakcoder_agent and its dependencies.',
      );
    }

    // `--no-index` is the whole point: nothing here touches the network.
    await run(
      python,
      [
        '-m',
        'pip',
        'install',
        '--no-index',
        '--find-links',
        wheels,
        '--disable-pip-version-check',
        '--force-reinstall',
        agentWheel,
      ],
      this.opts.log,
    );
    fs.writeFileSync(stamp, wanted, 'utf8');
    return python;
  }

  /**
   * Configured path, then the Python extension's selected interpreter, then the
   * usual names. Version-checked, because the failure of a too-old Python is a
   * syntax error deep in a traceback rather than anything a developer can read.
   */
  private async findPython(): Promise<string> {
    const candidates: string[] = [];
    if (this.opts.pythonPath) candidates.push(this.opts.pythonPath);

    const pythonExt = vscode.extensions.getExtension('ms-python.python');
    if (pythonExt) {
      try {
        const api = pythonExt.isActive ? pythonExt.exports : await pythonExt.activate();
        const selected: string | undefined =
          api?.settings?.getExecutionDetails?.()?.execCommand?.[0];
        if (selected) candidates.push(selected);
      } catch {
        /* the Python extension is optional */
      }
    }
    candidates.push(...(process.platform === 'win32' ? ['py', 'python', 'python3'] : ['python3', 'python']));

    for (const candidate of candidates) {
      const version = await pythonVersion(candidate);
      if (version && (version[0] > 3 || (version[0] === 3 && version[1] >= 11))) return candidate;
    }
    throw new RuntimeError(
      'no Python 3.11 or newer was found.',
      'Set "dakcoder.pythonPath", or install Python from the internal distribution.',
    );
  }

  dispose(): void {
    if (!this.child) return;
    this.opts.log.info('stopping the runtime');
    try {
      this.child.kill();
    } catch {
      /* already gone */
    }
    this.child = undefined;
    this.announced = undefined;
  }
}

// ── helpers ─────────────────────────────────────────────────────────────────

function randomToken(): string {
  return createHash('sha256')
    .update(`${Date.now()}:${Math.random()}:${process.pid}`)
    .digest('base64url');
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function readIfExists(file: string): string | undefined {
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch {
    return undefined;
  }
}

/**
 * Keyed on the wheels' *content*, not the version.
 *
 * A rebuilt wheel at the same version is a different wheel, and a version-keyed
 * cache serves the old one forever — which during development means testing a
 * build that no longer exists.
 */
function wheelHash(dir: string): string {
  const hash = createHash('sha256');
  let names: string[] = [];
  try {
    names = fs.readdirSync(dir).filter((n) => n.endsWith('.whl')).sort();
  } catch {
    return 'none';
  }
  for (const name of names) {
    const stat = fs.statSync(path.join(dir, name));
    hash.update(`${name}:${stat.size}:${stat.mtimeMs}`);
  }
  return hash.digest('hex').slice(0, 16);
}

function firstWheel(dir: string, prefix: string): string | undefined {
  try {
    const name = fs.readdirSync(dir).find((n) => n.startsWith(prefix) && n.endsWith('.whl'));
    return name ? path.join(dir, name) : undefined;
  } catch {
    return undefined;
  }
}

function extensionVersion(extensionPath: string): string {
  try {
    return JSON.parse(fs.readFileSync(path.join(extensionPath, 'package.json'), 'utf8')).version;
  } catch {
    return 'dev';
  }
}

export function run(
  command: string,
  args: string[],
  log?: vscode.LogOutputChannel,
  cwd?: string,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, windowsHide: true, shell: false });
    let out = '';
    let err = '';
    child.stdout?.on('data', (c) => (out += c.toString()));
    child.stderr?.on('data', (c) => (err += c.toString()));
    child.on('error', reject);
    child.on('exit', (code) => {
      if (code === 0) resolve(out);
      else {
        log?.warn(`${command} ${args.join(' ')} exited ${code}: ${err.slice(0, 400)}`);
        reject(new Error(err.trim() || `${command} exited with ${code}`));
      }
    });
  });
}

async function pythonVersion(command: string): Promise<[number, number] | undefined> {
  try {
    const out = await run(command, ['-c', 'import sys;print(sys.version_info[0],sys.version_info[1])']);
    const [major, minor] = out.trim().split(/\s+/).map(Number);
    return Number.isFinite(major) && Number.isFinite(minor) ? [major, minor] : undefined;
  } catch {
    return undefined;
  }
}
