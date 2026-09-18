/**
 * Generate `src/contract.gen.ts` from `../api/contract.json` and `../api/openapi.json`.
 *
 *   node scripts/gen-contract.mjs [--check]
 *
 * The runtime is the only side that can be authoritative about the wire
 * contract, so it publishes both files (`make contract`) and this script turns
 * them into types. The extension used to keep hand-written copies, and they
 * drifted: the runtime emitted `metrics` events for five releases while the
 * `EventType` union did not list them, and the reply to extending an approval
 * was typed as a number when the runtime can send null.
 *
 * `--check` fails when any file is missing or the generated file is stale. A
 * missing file fails rather than skips. The Python catalogue check used to skip
 * on a missing file, and it went on passing without running for as long as the
 * file was absent.
 *
 * The schema-to-TypeScript conversion covers what the runtime's Pydantic models
 * produce and nothing else. A schema it does not recognise stops the script
 * rather than becoming `unknown`, which would compile and describe nothing.
 */

import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const CONTRACT = join(ROOT, '..', 'api', 'contract.json');
const OPENAPI = join(ROOT, '..', 'api', 'openapi.json');
const TARGET = join(ROOT, 'src', 'contract.gen.ts');
const REGENERATE = 'run `make contract` at the repository root and commit the result';

const contractText = safeRead(CONTRACT);
if (contractText === null) fail(`api/contract.json is missing: ${REGENERATE}`);
const openapiText = safeRead(OPENAPI);
if (openapiText === null) fail(`api/openapi.json is missing: ${REGENERATE}`);

const contract = JSON.parse(contractText);
const openapi = JSON.parse(openapiText);
const text = render(contract, openapi, digest(openapiText));

if (process.argv.includes('--check')) {
  const current = safeRead(TARGET);
  if (current === null) fail(`src/contract.gen.ts is missing: ${REGENERATE}`);
  // Compared without regard to line endings: git may check the file out with
  // CRLF on Windows, and that is not drift.
  if (normalise(current) !== text) fail(`src/contract.gen.ts is stale: ${REGENERATE}`);
  console.log(`contract is current (api ${contract.api_version}, ${contract.hash})`);
} else {
  writeFileSync(TARGET, text, 'utf8');
  console.log(`wrote src/contract.gen.ts (api ${contract.api_version}, ${contract.hash})`);
}

function render({ api_version, hash, events }, openapi, openapiDigest) {
  const union = events.map((type) => `  | '${type}'`).join('\n');
  const schemas = Object.entries(openapi.components.schemas)
    .map(([name, schema]) => declaration(name, schema))
    .join('\n');
  return `// Generated from api/contract.json and api/openapi.json by scripts/gen-contract.mjs.
// Do not edit. Run \`make contract\` at the repository root and commit the result.
// openapi.json digest: ${openapiDigest}

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

// ── REST shapes ─────────────────────────────────────────────────────────────
//
// Every request and response body, from api/openapi.json. Each is a lower
// bound (C2): a newer runtime may add fields, and they must be ignored.

${schemas}`;
}

function declaration(name, schema) {
  const doc = jsdoc(schema, '');
  if (schema.type !== 'object' || !schema.properties) {
    return `${doc}export type ${name} = ${tsType(schema, name)};\n`;
  }
  const required = new Set(schema.required ?? []);
  const fields = Object.entries(schema.properties).map(([field, prop]) => {
    const optional = required.has(field) ? '' : '?';
    return `${jsdoc(prop, '  ')}  ${key(field)}${optional}: ${tsType(prop, `${name}.${field}`)};`;
  });
  return `${doc}export interface ${name} {\n${fields.join('\n')}\n}\n`;
}

function tsType(schema, where) {
  if (schema.$ref) return schema.$ref.split('/').pop();
  if (schema.anyOf) return schema.anyOf.map((s) => tsType(s, where)).join(' | ');
  if (schema.enum) return schema.enum.map(literal).join(' | ');
  if ('const' in schema) return literal(schema.const);
  switch (schema.type) {
    case 'string':
      return 'string';
    case 'integer':
    case 'number':
      return 'number';
    case 'boolean':
      return 'boolean';
    case 'null':
      return 'null';
    case 'array': {
      const item = tsType(schema.items ?? {}, `${where}[]`);
      return /[|&]/.test(item) ? `(${item})[]` : `${item}[]`;
    }
    case 'object': {
      const extra = schema.additionalProperties;
      if (schema.properties) break;
      if (extra === undefined || extra === true) return 'Record<string, unknown>';
      return `Record<string, ${tsType(extra, `${where}{}`)}>`;
    }
    case undefined:
      // Pydantic's `Any`: an empty schema.
      if (Object.keys(schema).filter((k) => k !== 'description').length === 0) return 'unknown';
  }
  fail(`gen-contract: cannot convert the schema at ${where}: ${JSON.stringify(schema)}`);
}

function jsdoc(schema, indent) {
  const lines = [];
  if (schema.description) lines.push(...schema.description.split('\n'));
  if (schema.deprecated) lines.push('@deprecated');
  if (!lines.length) return '';
  if (lines.length === 1) return `${indent}/** ${lines[0]} */\n`;
  return `${indent}/**\n${lines.map((l) => `${indent} * ${l}`.trimEnd()).join('\n')}\n${indent} */\n`;
}

function key(name) {
  return /^[A-Za-z_$][\w$]*$/.test(name) ? name : `'${name}'`;
}

function literal(value) {
  return typeof value === 'string' ? `'${value.replace(/'/g, "\\'")}'` : JSON.stringify(value);
}

/** The same digest `test_contract.py` computes, so the Python side can check it. */
function digest(text) {
  return createHash('sha256').update(normalise(text), 'utf8').digest('hex').slice(0, 16);
}

function normalise(text) {
  return text.replace(/\r\n/g, '\n');
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
