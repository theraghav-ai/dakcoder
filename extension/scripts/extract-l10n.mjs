/**
 * Extract every `vscode.l10n.t(...)` literal into `l10n/bundle.l10n.json`.
 *
 *   node scripts/extract-l10n.mjs [--check]
 *
 * **Why extract at all when English works anyway.** `l10n.t` falls back to the
 * literal, so an empty bundle ships a working English extension — which is
 * exactly why this gets skipped and then never done. The bundle is the artefact
 * a translator receives; without it there is nothing to hand over, and
 * retrofitting l10n into thousands of string literals later is the expensive
 * version of this script.
 *
 * `--check` fails when the bundle is stale, so CI catches a string added
 * without re-extracting rather than a translator discovering it.
 *
 * Deliberately a regex rather than a TypeScript AST walk: the call is always
 * `l10n.t('literal', …)` by house rule, a parser here would be a build
 * dependency, and a template literal or a variable — which a regex cannot read
 * — is reported as an error rather than silently dropped.
 */

import { readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const SRC = join(ROOT, 'src');
const BUNDLE = join(ROOT, 'l10n', 'bundle.l10n.json');

/** `l10n.t('…')` or `l10n.t("…")`, capturing the literal only. */
const CALL = /\bl10n\.t\(\s*(['"])((?:\\.|(?!\1)[^\\])*)\1/g;
/** A call whose first argument is not a literal: unextractable, and a defect. */
const DYNAMIC = /\bl10n\.t\(\s*[`$a-zA-Z_{]/g;

const strings = new Set();
const dynamic = [];

for (const name of walk(SRC)) {
  const src = readFileSync(name, 'utf8');
  for (const match of src.matchAll(CALL)) {
    // Unescape so the bundle holds the string as it renders, not as it is typed.
    strings.add(match[2].replace(/\\(['"\\])/g, '$1').replace(/\\n/g, '\n'));
  }
  for (const match of src.matchAll(DYNAMIC)) {
    const line = src.slice(0, match.index).split('\n').length;
    dynamic.push(`${name.slice(ROOT.length + 1)}:${line}`);
  }
}

if (dynamic.length > 0) {
  console.error(`\n${dynamic.length} l10n.t() call(s) with a non-literal first argument:\n`);
  for (const where of dynamic) console.error(`  ${where}`);
  console.error(
    '\nA computed message cannot be extracted, so it can never be translated.\n' +
      'Use a literal with {0} placeholders and pass the values as arguments.\n',
  );
  process.exit(1);
}

// Sorted, so a diff of this file shows what changed rather than how the walker
// happened to order the files that day.
const bundle = Object.fromEntries([...strings].sort().map((s) => [s, s]));
const text = `${JSON.stringify(bundle, null, 2)}\n`;

if (process.argv.includes('--check')) {
  const current = safeRead(BUNDLE);
  if (current !== text) {
    console.error('l10n/bundle.l10n.json is stale — run: node scripts/extract-l10n.mjs');
    process.exit(1);
  }
  console.log(`l10n bundle is current (${strings.size} strings)`);
} else {
  writeFileSync(BUNDLE, text, 'utf8');
  console.log(`extracted ${strings.size} strings to l10n/bundle.l10n.json`);
}

function* walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    // The test tree's strings are not user-visible.
    if (entry.isDirectory() && entry.name !== 'test') yield* walk(join(dir, entry.name));
    else if (entry.isFile() && entry.name.endsWith('.ts')) yield join(dir, entry.name);
  }
}

function safeRead(file) {
  try {
    return readFileSync(file, 'utf8');
  } catch {
    return '';
  }
}
