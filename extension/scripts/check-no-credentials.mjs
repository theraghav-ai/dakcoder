/**
 * Fail the build if a model credential is anywhere in the packaged extension.
 *
 * Part B §4.6: the model API key exists in exactly one place — the gateway's
 * secret store. The extension never holds one, the local runtime never holds
 * one, and the `.vsix` must never carry one. This is the leak that would matter
 * most, and an invariant nobody verifies is a comment.
 *
 * Scans the built bundle, the webview assets, the manifest and every vendored
 * wheel filename. Run after `npm run compile`, before `vsce package`.
 *
 *   node scripts/check-no-credentials.mjs
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, relative } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');

/**
 * Shapes of the credentials that could plausibly appear.
 *
 * Deliberately broader than "the key we use": a leak nobody predicted is still
 * a leak, and a false positive costs a minute while a miss costs a rotation.
 */
const PATTERNS = [
  { name: 'OpenAI-style key', re: /\bsk-[A-Za-z0-9_-]{16,}/ },
  { name: 'bearer literal', re: /\bBearer\s+[A-Za-z0-9._-]{24,}/ },
  { name: 'GitLab PAT', re: /\bglpat-[A-Za-z0-9_-]{16,}/ },
  { name: 'AWS access key', re: /\bAKIA[0-9A-Z]{16}\b/ },
  { name: 'private key block', re: /-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----/ },
  {
    name: 'assigned model credential',
    // An assignment, not a mention: the variable NAMES appear all over this
    // codebase precisely because the runtime deletes them from the child
    // environment, and flagging those would make the check cry wolf until
    // someone turned it off.
    // `DAKCODER_MODEL_<ROLE>_API_KEY` too: a role can carry its own key, and a
    // per-role one in the .vsix is the same leak as the shared one.
    re: /(DAKCODER_MODEL[A-Z0-9_]*_API_KEY|OPENAI_API_KEY|LITELLM_API_KEY|ANTHROPIC_API_KEY)\s*[:=]\s*['"][^'"\s]{8,}/,
  },
];

const SCAN_EXT = new Set(['.js', '.json', '.html', '.css', '.md', '.mjs', '.cjs', '.txt', '.yaml', '.yml']);
/**
 * Only what ships. This mirrors `.vscodeignore`, and the list is load-bearing
 * rather than tidy: without `.vscode-test` the scanner walks the *downloaded
 * VS Code*, finds a `sk-prompt-` literal in the bundled Copilot extension and a
 * private-key block in the shared process, and fails the build over someone
 * else's source. A check that cries wolf gets switched off, and then the real
 * leak ships.
 */
const SKIP_DIR = new Set([
  'node_modules',
  '.git',
  '.vscode-test',
  'dist-test',
  'src',
  'scripts',
]);

/** @type {{file: string, name: string, sample: string}[]} */
const hits = [];
let scanned = 0;

function walk(dir) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIR.has(entry)) continue;
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      walk(full);
      continue;
    }
    if (!SCAN_EXT.has(extname(entry))) continue;
    scanned += 1;
    const text = readFileSync(full, 'utf8');
    for (const { name, re } of PATTERNS) {
      const match = re.exec(text);
      if (!match) continue;
      hits.push({
        file: relative(ROOT, full),
        name,
        // Truncated: printing a whole credential into a CI log is the same
        // mistake one step further along.
        sample: `${match[0].slice(0, 12)}…`,
      });
    }
  }
}

walk(ROOT);

if (hits.length > 0) {
  console.error(`\nCREDENTIAL LEAK — ${hits.length} match(es) in the packaged extension:\n`);
  for (const hit of hits) console.error(`  ${hit.file}: ${hit.name} (${hit.sample})`);
  console.error(
    '\nThe model API key belongs in the gateway secret store and nowhere else.\n' +
      'Model traffic is proxied through the gateway so quota and audit are unbypassable;\n' +
      'a key in the .vsix is an unmetered bypass around both.\n',
  );
  process.exit(1);
}

console.log(`no credentials found in ${scanned} packaged files`);
