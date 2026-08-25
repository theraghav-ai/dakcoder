/**
 * Cross-compile `gotools` for every platform the `.vsix` supports, and write the
 * checksum manifest the extension verifies before launching one.
 *
 *   node scripts/build-gotools.mjs [--host-only]
 *
 * **Why a checksum at all.** The binary is spawned with the workspace as its
 * argument and writes files there. A tampered or truncated one — a partial
 * download, a half-extracted `.vsix` — is not a hypothetical: it is the ordinary
 * failure mode of shipping a binary, and it presents as the agent behaving
 * strangely rather than as anything anyone would suspect. Verifying before the
 * first launch turns that into one clear refusal.
 *
 * **Why not download at runtime.** The point of vendoring is that the network is
 * out of the critical path. A binary fetched on first use puts it straight back.
 */

import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, readdirSync, writeFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const GOTOOLS = join(ROOT, '..', 'gotools');
const OUT = join(ROOT, 'bin');

/**
 * The four VS Code ships for. Named the way `process.platform`-`process.arch`
 * reports them, so the extension composes the filename rather than mapping it —
 * a mapping table is one more thing to get wrong on the platform nobody tests.
 */
const TARGETS = [
  { file: 'gotools-win32-x64.exe', GOOS: 'windows', GOARCH: 'amd64' },
  { file: 'gotools-darwin-arm64', GOOS: 'darwin', GOARCH: 'arm64' },
  { file: 'gotools-darwin-x64', GOOS: 'darwin', GOARCH: 'amd64' },
  { file: 'gotools-linux-x64', GOOS: 'linux', GOARCH: 'amd64' },
];

const hostOnly = process.argv.includes('--host-only');
const checkOnly = process.argv.includes('--check');
const hostFile = `gotools-${process.platform}-${process.arch}${process.platform === 'win32' ? '.exe' : ''}`;

const version = process.env.DAKCODER_VERSION ?? readVersion();
mkdirSync(OUT, { recursive: true });

if (checkOnly) {
  // The manifest and the binaries must agree, or the extension refuses to launch
  // the sidecar at runtime — which is correct, and a terrible place to find out.
  verifyManifest();
  process.exit(0);
}

const built = [];
for (const target of TARGETS) {
  if (hostOnly && target.file !== hostFile) continue;
  const out = join(OUT, target.file);
  process.stdout.write(`building ${target.file} … `);
  execFileSync(
    'go',
    [
      'build',
      // Trimmed and stripped: the path prefix of whoever built it is not
      // information anyone needs, and it differs per machine, which would make
      // the checksum unreproducible.
      '-trimpath',
      '-ldflags',
      `-s -w -X main.Version=${version}`,
      '-o',
      out,
      './cmd/gotools',
    ],
    {
      cwd: GOTOOLS,
      env: { ...process.env, GOOS: target.GOOS, GOARCH: target.GOARCH, CGO_ENABLED: '0' },
      stdio: ['ignore', 'inherit', 'inherit'],
    },
  );
  console.log(`${(statSync(out).size / 1024 / 1024).toFixed(1)} MB`);
  built.push(target.file);
}

// `sha256sum` format, so it can be checked with the standard tool as well as by
// the extension — a manifest only one program can read is a manifest nobody
// checks by hand when they need to.
const lines = readdirSync(OUT)
  .filter((n) => n.startsWith('gotools-'))
  .sort()
  .map((name) => `${sha256(join(OUT, name))}  ${name}`);

writeFileSync(join(OUT, 'gotools.sha256'), `${lines.join('\n')}\n`, 'utf8');

console.log(`\nwrote bin/gotools.sha256 (${lines.length} binaries)`);
for (const line of lines) console.log(`  ${line}`);
if (hostOnly) {
  console.log(
    `\nhost-only build: the other platforms are cross-compiled by CI.\n` +
      `The extension refuses to launch a binary it has no checksum for, so a\n` +
      `partial set fails loudly on the missing platform rather than silently.`,
  );
}

function verifyManifest() {
  let manifest;
  try {
    manifest = readFileSync(join(OUT, 'gotools.sha256'), 'utf8');
  } catch {
    console.error('bin/gotools.sha256 is missing — run: npm run build:gotools');
    process.exit(1);
  }
  const problems = [];
  let checked = 0;
  for (const line of manifest.split('\n')) {
    const match = line.match(/^([0-9a-f]{64})\s+\*?(.+?)\s*$/i);
    if (!match) continue;
    const [, expected, name] = match;
    try {
      const actual = sha256(join(OUT, name));
      checked += 1;
      if (actual !== expected.toLowerCase()) problems.push(`${name}: checksum mismatch`);
    } catch {
      problems.push(`${name}: listed in the manifest but not present`);
    }
  }
  const present = readdirSync(OUT).filter((n) => n.startsWith('gotools-'));
  for (const name of present) {
    if (!manifest.includes(name)) problems.push(`${name}: present but not in the manifest`);
  }
  if (problems.length > 0) {
    console.error('\ngotools sidecar manifest is wrong:\n');
    for (const p of problems) console.error(`  ${p}`);
    console.error('\nThe extension refuses to launch a binary whose checksum does not match.\n');
    process.exit(1);
  }
  console.log(`gotools manifest is current (${checked} binaries verified)`);
}

function sha256(file) {
  return createHash('sha256').update(readFileSync(file)).digest('hex');
}

function readVersion() {
  try {
    return JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8')).version;
  } catch {
    return 'dev';
  }
}
