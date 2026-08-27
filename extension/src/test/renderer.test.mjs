/**
 * Tests for the panel's renderer, against a stub DOM.
 *
 * `chat.js` runs inside a webview, which is why nothing else here can reach it:
 * it is an IIFE with no exports, loaded by a page. It is also where the fault
 * that made two questions look like one lived, so it is worth testing rather
 * than reasoning about. The stub below implements only what the file actually
 * touches - seventeen elements by id, and the handful of node methods the
 * renderer uses - so a renderer that reaches for a real browser API fails here
 * by absence rather than passing against a stub that quietly lies.
 */

import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const CHAT_JS = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'media', 'chat', 'chat.js');

const IDS = [
  'announce', 'composer', 'console', 'input', 'input-label', 'keys', 'meter',
  'mode-pill', 'offline', 'popup', 'queued', 'send', 'skip', 'stop',
  'transcript', 'wind-down', 'working',
];

class Node {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.selectionStart = 0;
    this._text = '';
    const classes = new Set();
    this._classes = classes;
    this.classList = {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    };
  }

  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node.__root === true;
  }

  set className(value) {
    this._classes.clear();
    String(value || '').split(/\s+/).filter(Boolean).forEach((n) => this._classes.add(n));
  }
  get className() {
    return [...this._classes].join(' ');
  }

  /** Recursive, like the real one: it is how a test reads what is on screen. */
  get textContent() {
    if (!this.children.length) return this._text;
    return this.children.map((c) => c.textContent).join('');
  }
  set textContent(value) {
    this.children.forEach((c) => (c.parentNode = null));
    this.children = [];
    this._text = value === undefined || value === null ? '' : String(value);
  }

  get firstChild() {
    return this.children[0] || null;
  }

  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    this._text = '';
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const at = this.children.indexOf(child);
    if (at !== -1) this.children.splice(at, 1);
    child.parentNode = null;
    return child;
  }
  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }
  replaceWith(next) {
    const parent = this.parentNode;
    if (!parent) return;
    parent.children[parent.children.indexOf(this)] = next;
    next.parentNode = parent;
    this.parentNode = null;
  }
  before(node) {
    const parent = this.parentNode;
    if (!parent) return;
    parent.children.splice(parent.children.indexOf(this), 0, node);
    node.parentNode = parent;
  }
  after(node) {
    const parent = this.parentNode;
    if (!parent) return;
    parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
    node.parentNode = parent;
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
  removeAttribute(name) {
    delete this.attributes[name];
  }
  querySelector() {
    return null;
  }
  addEventListener() {}
  focus() {}
  contains() {
    return false;
  }
}

/** Load `chat.js` into a fresh stub page and return the handles a test needs. */
function panel() {
  const byId = new Map();
  const root = new Node('body');
  root.__root = true;
  for (const id of IDS) {
    const node = new Node(id === 'input' ? 'textarea' : 'div');
    node.attributes.id = id;
    byId.set(id, node);
    root.appendChild(node);
  }

  const posted = [];
  const frames = [];
  let onMessage = null;
  let stored = {};

  const sandbox = {
    console,
    setTimeout,
    clearTimeout,
    requestAnimationFrame: (fn) => frames.push(fn),
    acquireVsCodeApi: () => ({
      postMessage: (m) => posted.push(m),
      getState: () => stored,
      setState: (s) => (stored = s),
    }),
    navigator: { clipboard: { writeText: async () => {} } },
    document: {
      getElementById: (id) => byId.get(id) || null,
      createElement: (tag) => new Node(tag),
      createElementNS: (_ns, tag) => new Node(tag),
      createTextNode: (text) => {
        const node = new Node('#text');
        node.textContent = text;
        return node;
      },
      addEventListener: () => {},
      get activeElement() {
        return null;
      },
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.window.addEventListener = (type, handler) => {
    if (type === 'message') onMessage = handler;
  };

  vm.runInNewContext(readFileSync(CHAT_JS, 'utf8'), sandbox, { filename: 'chat.js' });

  const send = (...messages) => {
    for (const data of messages) onMessage({ data });
    while (frames.length) frames.shift()();
  };

  send({ type: 'init', strings: strings(), maxRows: 500, commands: [], mentions: [] });

  return {
    send,
    posted,
    /** The rows on screen, in order. The renderer stamps each with its key. */
    rows: () =>
      byId
        .get('transcript')
        .children.filter((n) => n.dataset.key)
        .map((n) => ({ key: n.dataset.key, text: n.textContent })),
  };
}

/** The host ships every string; the renderer has no fallbacks by design. */
function strings() {
  return new Proxy({}, { get: (_t, name) => (typeof name === 'string' ? '{0}' : undefined) });
}

let seq = 0;
const wire = (session, id, type, data = {}) => ({
  type: 'event',
  session,
  seq: (seq += 1),
  event: { id, type, data },
});

const saying = (rows, text) => rows.filter((r) => r.text.indexOf(text) !== -1);

// -- the two-conversation fault ---------------------------------------------

describe('the transcript, across two conversations', () => {
  it('does not let a second session paint over the first one', () => {
    // The reported fault, reduced. Wire event ids are unique only within a
    // session and restart at 1 for the next one, so keying rows on the id alone
    // meant the second conversation's first answer replaced the first
    // conversation's: the question changed and the answer on screen with it.
    const p = panel();
    p.send(
      wire('s1', 1, 'user', { text: 'hi' }),
      wire('s1', 2, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('s1', 3, 'assistant', { text: 'first answer' }),
      wire('s2', 1, 'user', { text: 'how are you' }),
      wire('s2', 2, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('s2', 3, 'assistant', { text: 'second answer' }),
    );

    const rows = p.rows();
    const text = rows.map((r) => r.text);
    assert.equal(saying(rows, 'first answer').length, 1, `the first answer was erased: ${text}`);
    assert.equal(saying(rows, 'second answer').length, 1, `the second answer is missing: ${text}`);
    assert.ok(
      text.findIndex((t) => t.indexOf('first answer') !== -1) <
        text.findIndex((t) => t.indexOf('second answer') !== -1),
      'the answers are out of order',
    );
  });

  it('keeps every turn of one conversation as its own row', () => {
    const p = panel();
    p.send(
      wire('s3', 1, 'user', { text: 'hi' }),
      wire('s3', 2, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('s3', 3, 'assistant', { text: 'first answer' }),
      wire('s3', 4, 'finish', { outcome: 'done', summary: '', turns: 1, mutations: [] }),
      wire('s3', 5, 'user', { text: 'and the handler?' }),
      wire('s3', 6, 'turn_start', { turn: 2, mode: 'planner' }),
      wire('s3', 7, 'assistant', { text: 'second answer' }),
    );

    const keys = p.rows().map((r) => r.key);
    assert.equal(new Set(keys).size, keys.length, `rows collided: ${keys}`);
    assert.equal(keys.filter((k) => k.indexOf('/turn:') !== -1).length, 2, 'both turns must show');
    assert.equal(keys.filter((k) => k.indexOf('/assistant:') !== -1).length, 2);
  });

  it('gives each run of a conversation its own gate grid', () => {
    // The gate grid is keyed by kind rather than by event id, because its job is
    // to put attempt 1 and attempt 2 of one run side by side. A session now
    // holds several runs, so without a run counter in the key the second
    // message's gate is drawn into the first message's grid.
    const p = panel();
    p.send(
      wire('s8', 1, 'user', { text: 'build it' }),
      wire('s8', 2, 'gate', { kind: 'full', ok: false, stages: [{ name: 'go build', ok: false }] }),
      wire('s8', 3, 'finish', { outcome: 'unverified', summary: '', turns: 1, mutations: [] }),
      wire('s8', 4, 'user', { text: 'try again' }),
      wire('s8', 5, 'gate', { kind: 'full', ok: true, stages: [{ name: 'go build', ok: true }] }),
    );

    const grids = p.rows().filter((r) => r.key.indexOf('/gate:') !== -1);
    assert.equal(grids.length, 2, `the second run overwrote the first run's gate: ${grids.length}`);
  });
});

// -- the developer's own messages -------------------------------------------

describe('the transcript, and what the developer typed', () => {
  it('shows a typed message once, not twice', () => {
    // The composer draws it immediately, because a round trip the developer can
    // feel makes the panel seem broken. The runtime then records the same
    // message and it arrives as a `user` event. Both on screen is the panel
    // stuttering.
    const p = panel();
    p.send({ type: 'user', text: 'add a Pension resource', steering: false });
    assert.equal(p.rows().length, 1);

    p.send(wire('s4', 1, 'user', { text: 'add a Pension resource' }));
    const said = saying(p.rows(), 'add a Pension resource');
    assert.equal(said.length, 1, 'the echo and the recorded message are both on screen');
    assert.ok(
      said[0].key.indexOf('s4/') === 0,
      'the surviving row must be the one a replay can match',
    );
  });

  it('does not delete an earlier message that happens to repeat', () => {
    const p = panel();
    p.send(
      { type: 'user', text: 'run the tests', steering: false },
      wire('s5', 1, 'user', { text: 'run the tests' }),
      wire('s5', 2, 'assistant', { text: 'they pass' }),
      { type: 'user', text: 'run the tests', steering: false },
      wire('s5', 3, 'user', { text: 'run the tests' }),
    );
    assert.equal(saying(p.rows(), 'run the tests').length, 2, 'asked twice, shown twice');
  });
});

// -- a rebuilt panel ---------------------------------------------------------

describe('the transcript, after the panel is rebuilt', () => {
  it('resumes from the host cursor rather than from a wire event id', () => {
    const p = panel();
    p.send(wire('s6', 1, 'assistant', { text: 'answer' }));
    const ready = p.posted.filter((m) => m.type === 'ready');
    assert.equal(ready.length, 1);
    assert.ok('lastSeq' in ready[0], 'ids restart per session and cannot order a ring');
  });

  it('ignores a replayed message it has already applied', () => {
    const p = panel();
    const event = wire('s7', 1, 'assistant', { text: 'answer' });
    p.send(event, event);
    assert.equal(saying(p.rows(), 'answer').length, 1);
  });
});
