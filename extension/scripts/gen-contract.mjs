/**
 * Generate `src/contract.gen.ts` from `../api/contract.json`.
 *
 *   node scripts/gen-contract.mjs [--check]
 *
 * The runtime is the only side that can be authoritative about the wire
 * contract, so it publishes `api/contract.json` (`make contract`) and this
 * script turns it into types. The extension used to keep a hand-written copy,
 * and that copy drifted: the runtime emitted `metrics` events for five
 * releases while the `EventType` union did not list them.
 *
 * `--check` fails when either file is missing or the generated file is stale.
 * A missing file fails rather than skips. The Python catalogue check used to
 * skip on a missing file, and it went on passing without running for as long
 * as the file was absent.
 */

import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const SOURCE = join(ROOT, '..', 'api', 'contract.json');
const TARGET = join(ROOT, 'src', 'contract.gen.ts');
const REGENERATE = 'run `make contract` at the repository root and commit the result';

const source = safeRead(SOURCE);
if (source === null) fail(`api/contract.json is missing: ${REGENERATE}`);

const contract = JSON.parse(source);
const text = render(contract);

if (process.argv.includes('--check')) {
  const current = safeRead(TARGET);
  if (current === null) fail(`src/contract.gen.ts is missing: ${REGENERATE}`);
  // Compared without regard to line endings: git may check the file out with
  // CRLF on Windows, and that is not drift.
  if (current.replace(/\r\n/g, '\n') !== text) fail(`src/contract.gen.ts is stale: ${REGENERATE}`);
  console.log(`contract is current (api ${contract.api_version}, ${contract.hash})`);
} else {
  writeFileSync(TARGET, text, 'utf8');
  console.log(`wrote src/contract.gen.ts (api ${contract.api_version}, ${contract.hash})`);
}

function render({ api_version, hash, events }) {
  const union = events.map((type) => `  | '${type}'`).join('\n');
  return `// Generated from api/contract.json by scripts/gen-contract.mjs. Do not edit.
// Run \`make contract\` at the repository root and commit the result.

/**
 * The runtime API this build speaks. A mismatch with \`/v1/health\` is refused
 * at connect time.
 */
export const API_VERSION = '${api_version}';

/**
 * A hash of the contract this build was generated from. \`/v1/health\` reports
 * the runtime's. When only this differs, the runtime speaks the same version
 * with additions this build does not know about. That is legal under C2, so it
 * is logged, not refused.
 */
export const CONTRACT_HASH = '${hash}';

/**
 * Every event type the runtime can emit (C2). A lower bound: a newer runtime
 * may send types not listed here, and they must be ignored.
 */
export type EventType =
${union};
`;
}

function safeRead(file) {
  try {
    return readFileSync(file, 'utf8');
  } catch {
    return null;
  }
}

function fail(message) {
  console.error(message);
  process.exit(1);
}
