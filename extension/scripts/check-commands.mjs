/**
 * Every command in the palette must have a registration.
 *
 * A command declared in `contributes.commands` with no `registerCommand` throws
 * "command not found" when invoked — and the palette is exactly where someone
 * discovers it, so the first person to find the feature is the one who hits the
 * error. Nothing else catches this: it typechecks, it bundles, and it packages.
 *
 *   node scripts/check-commands.mjs
 */

import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const SRC = join(ROOT, 'src');

const manifest = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
const declared = new Set((manifest.contributes?.commands ?? []).map((c) => c.command));

/**
 * `registerCommand` plus the one-line helpers the modules wrap it in.
 *
 * Matching the helpers rather than banning them: a module that registers twenty
 * commands is more readable with a two-character alias, and a checker that
 * forces a house style is a checker people work around.
 */
const PATTERNS = [
  /registerCommand\(\s*['"]([\w.]+)['"]/g,
  /registerTextEditorCommand\(\s*['"]([\w.]+)['"]/g,
  /\bcmd\(\s*['"]([\w.]+)['"]/g,
  /\bcommand\(\s*['"]([\w.]+)['"]/g,
  /\bregister\(\s*['"]([\w.]+)['"]/g,
];

const registered = new Set();
for (const name of readdirSync(SRC)) {
  if (!name.endsWith('.ts')) continue;
  const src = readFileSync(join(SRC, name), 'utf8');
  for (const pattern of PATTERNS) {
    for (const match of src.matchAll(pattern)) registered.add(match[1]);
  }
}

const missing = [...declared].filter((c) => !registered.has(c)).sort();

if (missing.length > 0) {
  console.error(`\n${missing.length} command(s) declared in package.json with no registration:\n`);
  for (const c of missing) console.error(`  ${c}`);
  console.error('\nInvoking any of these from the palette throws "command not found".\n');
  process.exit(1);
}

console.log(`all ${declared.size} declared commands are registered`);
