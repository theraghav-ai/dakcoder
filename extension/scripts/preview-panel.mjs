/*
 * Build a standalone HTML preview of the panel, for looking at.
 *
 * It runs the real renderer over a sample transcript, serialises the DOM it
 * builds, and inlines the real stylesheet with the vendored fonts rewritten to
 * absolute paths so a browser opened on file:// resolves them. What it shows is
 * the panel's own markup and the panel's own CSS -- nothing here restyles
 * anything, so a layout that reads badly here reads badly in the sidebar.
 */
import vm from 'node:vm';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const ROOT = resolve('media/chat');
const FONTS = resolve('media/fonts').replace(/\\/g, '/');

class N {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = []; this.parentNode = null; this.attributes = {}; this.dataset = {};
    this.style = {}; this.hidden = false; this._text = '';
    const cs = new Set(); this._classes = cs;
    this.classList = {
      add: (...n) => n.forEach((x) => cs.add(x)), remove: (...n) => n.forEach((x) => cs.delete(x)),
      toggle: (n, on) => (on ? cs.add(n) : cs.delete(n)), contains: (n) => cs.has(n),
    };
  }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n.__root === true; }
  set className(v) { this._classes.clear(); String(v || '').split(/\s+/).filter(Boolean).forEach((n) => this._classes.add(n)); }
  get className() { return [...this._classes].join(' '); }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join('') : this._text; }
  set textContent(v) { this.children.forEach((c) => (c.parentNode = null)); this.children = []; this._text = v == null ? '' : String(v); }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { if (c.parentNode) c.parentNode.removeChild(c); this._text = ''; c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i !== -1) this.children.splice(i, 1); c.parentNode = null; return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(n) { const p = this.parentNode; if (!p) return; p.children[p.children.indexOf(this)] = n; n.parentNode = p; this.parentNode = null; }
  before() {} after() {}
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }
  removeAttribute(k) { delete this.attributes[k]; }
  querySelector() { return null; }
  addEventListener() {} focus() {} contains() { return false; }
}

const IDS = ['announce', 'composer', 'console', 'input', 'input-label', 'keys', 'meter',
  'mode-pill', 'offline', 'popup', 'queued', 'send', 'skip', 'stop', 'transcript',
  'wind-down', 'working'];

const byId = new Map();
const root = new N('body'); root.__root = true;
for (const id of IDS) { const n = new N(id === 'input' ? 'textarea' : 'div'); n.attributes.id = id; byId.set(id, n); root.appendChild(n); }

const frames = [];
let onMessage = null;
const sandbox = {
  console, setTimeout, clearTimeout,
  requestAnimationFrame: (fn) => frames.push(fn),
  acquireVsCodeApi: () => ({ postMessage() {}, getState: () => ({}), setState() {} }),
  navigator: { clipboard: { writeText: async () => {} } },
  document: {
    getElementById: (id) => byId.get(id) || null,
    createElement: (t) => new N(t),
    createElementNS: (_n, t) => new N(t),
    createTextNode: (t) => { const n = new N('#text'); n.textContent = t; return n; },
    addEventListener() {}, get activeElement() { return null; },
  },
};
sandbox.window = sandbox; sandbox.globalThis = sandbox;
sandbox.window.addEventListener = (t, h) => { if (t === 'message') onMessage = h; };

vm.runInNewContext(readFileSync(`${ROOT}/highlight.js`, 'utf8'), sandbox, { filename: 'highlight.js' });
vm.runInNewContext(readFileSync(`${ROOT}/chat.js`, 'utf8'), sandbox, { filename: 'chat.js' });

/* The real strings, lifted out of src/chat.ts so the preview reads as English. */
const ts = readFileSync('src/chat.ts', 'utf8');
const S = {};
for (const m of ts.matchAll(/^\s{4}'?([A-Za-z_][\w.]*)'?:\s*vscode\.l10n\.t\((['"])((?:\\.|(?!\2).)*)\2/gm)) {
  S[m[1]] = m[3].replace(/\\'/g, "'").replace(/\\"/g, '"').replace(/\\n/g, '\n');
}

const send = (...ms) => { for (const d of ms) onMessage({ data: d }); while (frames.length) frames.shift()(); };
send({ type: 'init', strings: S, maxRows: 500, commands: [], mentions: [],
  display: { expandOutput: 'failures', previewLines: 20, syntaxHighlighting: true, fontSize: 0 } });

let id = 0;
const ev = (type, data) => ({ type: 'event', session: 'demo', seq: ++id, event: { id, type, data } });

/* The paragraph from the screenshot, verbatim: it is the case that failed. */
send(
  ev('user', { text: 'Convert the legacy routes to the n-api-template contract.' }),
  ev('turn_start', { turn: 20, mode: 'agent', attempt: 1 }),
  ev('assistant', {
    text:
      'The branch `template-conversion` already exists and points to main HEAD (61e9ea9) — I verified ' +
      'this with `git rev-parse` earlier. The step\'s file path `.git/refs/heads/template-conversion` ' +
      'is a git internal ref, not a workspace file I can write with `write_file`. The branch was cut ' +
      'via `git_ops op=branch` which is the correct mechanism. The step is **functionally complete**.\n\n' +
      '| step | status | note |\n| --- | :---: | --- |\n' +
      '| branch | done | cut from `main` |\n| routes | done | 97 routes captured |\n\n' +
      '1. `template-conversion` branch exists\n2. `.dakcoder/routes-before.json` written\n' +
      '   - all 97 routes from `routes.go`\n   - grouped by prefix\n\n' +
      '> Both steps are done.\n\n' +
      'Here is the shape it writes:\n\n' +
      '```go:internal/routes/routes.go\npackage routes\n\n' +
      '// Register wires every handler onto the template router.\n' +
      'func Register(r *Router, h *Handler) error {\n' +
      '\tr.GET("/api/v1/pension/:id", h.GetPension)\n\treturn nil\n}\n```',
  }),
  ev('tool_call', { id: 't1', name: 'read_file', arguments: { path: 'internal/routes/routes.go' } }),
  ev('tool_result', { id: 't1', name: 'read_file', ok: true, ms: 12, content: 'package routes\n\ntype Router struct{}\n' }),
  ev('tool_call', { id: 't2', name: 'go_build', arguments: {} }),
  ev('tool_result', {
    id: 't2', name: 'go_build', ok: false, ms: 2400,
    content: ['# dop/pension/handler',
      'handler/pension.go:9:24: undefined: Pension',
      'handler/pension.go:10:9: h.repo undefined (type *Handler has no field or method repo)',
      'handler/pension.go:12:1: missing return',
      'FAIL\tdop/pension/handler [build failed]'].join('\n'),
    fix: 'define Pension in model/pension.go',
  }),
  ev('tool_result', {
    id: 't3', name: 'resource_scaffold', ok: true, ms: 310, arguments: { resource: 'Pension' },
    mutations: [
      { kind: 'create', path: 'handler/pension.go', added: 64, removed: 0 },
      { kind: 'modify', path: 'app/module.go', added: 3, removed: 1 },
      { kind: 'modify', path: 'configs/app.yaml', added: 2, removed: 0, protected: true },
    ],
  }),
  ev('tool_pending', { id: 'ap1', tool: 'write_file', reason: 'writes a new file outside the scaffolded set', paths: ['model/pension.go'] }),
);
send({
  type: 'approval-preview', id: 'ap1', path: 'model/pension.go',
  diff: ['--- a/model/pension.go', '+++ b/model/pension.go', '@@ -1,3 +1,7 @@', ' package model', ' ',
    '-type Pension struct{}', '+type Pension struct {', '+\tID   int64  `db:"id"`', '+\tName string `db:"name"`', '+}'].join('\n'),
  added: 4, removed: 1, cut: 0,
});
send(ev('finish', { outcome: 'unverified', summary: 'the gate never came clean on go vet', turns: 20, mutations: ['handler/pension.go', 'model/pension.go'] }));

const VOID = new Set(['BR', 'HR', 'INPUT', 'IMG']);
const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function html(n) {
  if (n.tagName === '#TEXT') return esc(n.textContent);
  const tag = n.tagName.toLowerCase();
  const attrs = Object.entries(n.attributes).map(([k, v]) => ` ${k}="${esc(v)}"`).join('');
  const cls = n.className ? ` class="${esc(n.className)}"` : '';
  const style = Object.entries(n.style).filter(([, v]) => v).map(([k, v]) => `${k.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase())}:${v}`).join(';');
  const st = style ? ` style="${esc(style)}"` : '';
  const hid = n.hidden ? ' hidden' : '';
  if (VOID.has(n.tagName)) return `<${tag}${cls}${attrs}${st}${hid}>`;
  const kids = n.children.length ? n.children.map(html).join('') : esc(n._text);
  return `<${tag}${cls}${attrs}${st}${hid}>${kids}</${tag}>`;
}

const css = readFileSync(`${ROOT}/chat.css`, 'utf8').replace(/url\('\.\.\/fonts\//g, `url('file:///${FONTS}/`);
const shell = readFileSync(`${ROOT}/index.html`, 'utf8');
const body = shell
  .replace(/<!DOCTYPE[\s\S]*?<body>/i, '')
  .replace(/<\/body>[\s\S]*$/i, '')
  .replace(/<script[\s\S]*?<\/script>/g, '')
  .replace(/<main id="transcript"[\s\S]*?<\/main>/, html(byId.get('transcript')));

const page = `<!doctype html><html><head><meta charset="utf-8">
<title>dakcoder panel — typography preview</title>
<style>${css}
/* Preview chrome only. Nothing below affects the panel itself. */
html,body{height:auto}
body{max-width:420px;margin:0 auto;border-left:1px solid #26262a;border-right:1px solid #26262a}
.transcript{overflow:visible}
.stage{display:block}
#preview-note{font:12px system-ui;color:#8a8a87;max-width:420px;margin:10px auto;padding:0 16px}
</style></head>
<body class="__THEME__">
<div id="preview-note">Panel width is pinned to 420px. Toggle the theme by changing the body class to <code>vscode-light</code>.</div>
${body}
</body></html>`;

writeFileSync('preview-dark.html', page.replace('__THEME__', 'vscode-dark'));
writeFileSync('preview-light.html', page.replace('__THEME__', 'vscode-light'));
console.log('wrote preview-dark.html and preview-light.html');
