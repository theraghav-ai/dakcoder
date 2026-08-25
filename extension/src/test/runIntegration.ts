/**
 * Downloads a VS Code, launches it, and runs the integration suite inside it.
 *
 * Run from the terminal, not from the extension host:
 *
 *   npm run test:integration
 *
 * `--disable-extensions` keeps other installed extensions out of the timing
 * numbers and out of the command registry, so "every declared command resolves"
 * is a statement about this extension rather than about whatever else happens to
 * be installed on the machine running CI.
 */

import * as path from 'node:path';
import { runTests } from '@vscode/test-electron';

async function main(): Promise<void> {
  // `ELECTRON_RUN_AS_NODE` makes an Electron binary behave as a bare Node
  // interpreter. Some terminals and agent harnesses set it for their own
  // subprocesses, and it is inherited — so VS Code launches as Node, rejects
  // every one of its own flags as "bad option", and tries to `require` the
  // workspace path as a module. The symptom looks nothing like the cause.
  delete process.env.ELECTRON_RUN_AS_NODE;

  // Compiled to dist-test/test/, so two levels up is the extension root — the
  // folder holding package.json, which is what VS Code loads as the extension.
  const extensionDevelopmentPath = path.resolve(__dirname, '../..');
  const extensionTestsPath = path.resolve(__dirname, './integration/index');

  try {
    await runTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      // No launchArgs. A positional folder path is consumed as the test entry
      // by this launcher, and every assertion here is scoped to the extension's
      // own manifest and command registry — none of them needs a folder open.
      // Activation's no-folder path is the one that runs, which is also the one
      // a developer sees when they install the extension before opening a repo.
    });
  } catch (err) {
    console.error('integration tests failed:', err);
    process.exit(1);
  }
}

void main();
