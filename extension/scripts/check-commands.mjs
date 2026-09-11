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

const EOL = String.fromCharCode(10);

/** The ids VS Code mints for every contributed view. Ours to call, never ours
 *  to register. */
const VIEW_SUFFIXES = ['focus', 'resetViewLocation', 'removeView', 'toggleVisibility'];
const reserved = new Set();
for (const views of Object.values(manifest.contributes?.views ?? {})) {
  for (const view of views) {
    for (const suffix of VIEW_SUFFIXES) reserved.add(`${view.id}.${suffix}`);
  }
}

const shadowed = [...registered].filter((c) => reserved.has(c)).sort();

if (shadowed.length > 0) {
  console.error(`
${shadowed.length} command(s) registered over an id VS Code owns:
`);
  for (const c of shadowed) console.error(`  ${c}`);
  console.error(
    [
      '',
      'VS Code mints these for every contributed view. Registering one shadows',
      'the built-in, and a handler that then calls the same id is a command that',
      'calls itself: it recurses until the extension host fails with',
      '"An object could not be cloned". Call them; do not register them.',
      '',
    ].join(EOL),
  );
  process.exit(1);
}

/**
 * And the other direction: a command with a handler and no palette entry.
 *
 * This was checked in one direction only, and the gap was real (BUG EXT-18): six
 * commands — including `dakcoder.stopTask`, which the status bar's own tooltip
 * tells the developer to run — had handlers and no way to reach them. Nothing
 * fails; the feature is simply not there, and the person looking for it
 * concludes the extension cannot do it.
 *
 * A command deliberately kept out of the palette is contributed with a
 * `commandPalette` entry of `"when": "false"`, which is how the context-menu
 * commands here already do it. So "registered and not contributed at all" has no
 * legitimate form.
 */
const uncontributed = [...registered]
  .filter((c) => c.startsWith('dakcoder.') && !declared.has(c) && !reserved.has(c))
  .sort();

if (uncontributed.length > 0) {
  console.error(`
${uncontributed.length} command(s) registered with no entry in package.json:
`);
  for (const c of uncontributed) console.error(`  ${c}`);
  console.error(
    [
      '',
      'These have handlers and no way to reach them: not in the palette, not in a',
      'menu, not discoverable at all. Add a `contributes.commands` entry — and if',
      'it should not appear in the palette, add a `commandPalette` menu entry with',
      '`"when": "false"` the way the context-menu commands do.',
      '',
    ].join(EOL),
  );
  process.exit(1);
}

/**
 * And the slash table, which is a third way to name a command and was the one
 * nobody checked.
 *
 * `slash()` in extension.ts maps `/name` to a command id and calls
 * `executeCommand`, whose rejection nothing awaits — so a typo there is a slash
 * command that does nothing, silently, which is exactly how `/migrate`,
 * `/debug`, `/scaffold` and `/test` shipped broken. Two of those were among the
 * four suggestions the empty panel offers as a first action.
 *
 * The table is read out of the source rather than imported, because this script
 * runs before the bundle exists and importing `extension.ts` would pull in
 * `vscode`.
 */
const extensionSrc = readFileSync(join(SRC, 'extension.ts'), 'utf8');
const table = /const routed: Record<string, string> = \{([\s\S]*?)\n {4}\};/.exec(extensionSrc);
if (!table) {
  console.error(
    [
      '',
      "the slash routing table could not be found in extension.ts.",
      'It is matched by shape, so renaming `routed` or reformatting its closing',
      'brace silently disables this check — which is the check that catches a',
      'slash command wired to a command id that does not exist.',
      '',
    ].join(EOL),
  );
  process.exit(1);
}

const routedIds = [...table[1].matchAll(/:\s*['"]([\w.]+)['"]/g)].map((m) => m[1]);
const danglingSlash = routedIds.filter((c) => !registered.has(c)).sort();

if (danglingSlash.length > 0) {
  console.error(`${EOL}${danglingSlash.length} slash command(s) route to an id nothing registers:${EOL}`);
  for (const c of danglingSlash) console.error(`  ${c}`);
  console.error(
    [
      '',
      '`executeCommand` rejects, nothing awaits the rejection, and the slash',
      'command does nothing at all — with no error anywhere the developer looks.',
      '',
    ].join(EOL),
  );
  process.exit(1);
}

console.log(
  `all ${declared.size} declared commands are registered, all ${registered.size} ` +
    `registered commands are contributed, all ${routedIds.length} slash routes resolve, ` +
    `none over a view's own id`,
);
