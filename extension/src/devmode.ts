/**
 * Running the extension and the agent from source, under F5.
 *
 * A packaged install runs the agent from wheels copied into a venv under
 * globalStorage, which is right for a developer's machine and wrong for this
 * repository's: every Python change needed `scripts/release.py` before it could
 * be seen. In the Extension Development Host the runtime is spawned from the
 * checkout's own `.venv` instead -- where `apps/agent` and `apps/shared` are
 * installed editable -- so a Python edit is live on `dakcoder: Restart Runtime`
 * and a TypeScript edit on `Developer: Reload Window`.
 *
 * Development mode only. `ExtensionMode.Development` is set by VS Code for
 * `--extensionDevelopmentPath` and never for an installed `.vsix`, so nothing
 * here can change what a developer's install executes.
 */

import * as fs from 'node:fs';
import * as path from 'node:path';

import * as vscode from 'vscode';

/** The checkout's interpreter, or undefined to use the packaged runtime. */
export function devPython(
  context: vscode.ExtensionContext,
  log: vscode.LogOutputChannel,
): string | undefined {
  if (context.extensionMode !== vscode.ExtensionMode.Development) return undefined;

  // `DAKCODER_DEV_PYTHON` for a venv somewhere else; the launch configuration
  // does not set it, so the default is the repository's own `.venv`.
  const repo = path.resolve(context.extensionUri.fsPath, '..');
  const candidate =
    process.env.DAKCODER_DEV_PYTHON?.trim() ||
    (process.platform === 'win32'
      ? path.join(repo, '.venv', 'Scripts', 'python.exe')
      : path.join(repo, '.venv', 'bin', 'python'));

  if (!fs.existsSync(candidate)) {
    log.warn(
      `development mode, but ${candidate} does not exist; using the packaged runtime. ` +
        'Create it with: python -m venv .venv, then .venv/Scripts/python -m pip install ' +
        '-e apps/shared -e apps/agent',
    );
    return undefined;
  }
  return candidate;
}
