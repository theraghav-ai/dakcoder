/**
 * The integration suite's entry point, run *inside* a real VS Code.
 *
 * Importing the test file is what registers its cases — the harness collects
 * them at module load and `runAll` executes them in this process, where the
 * `vscode` module actually exists.
 */

import { runAll } from './harness';

export async function run(): Promise<void> {
  // Side-effecting import: loading the module registers every test in it.
  await import('./activation.test');
  await runAll();
}
