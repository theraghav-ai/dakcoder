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

/*
 * The elements `chat.js` reaches for by id.
 *
 * `find`, `find-*` and `jump` are deliberately absent. Both features need a real
 * DOM - `createTreeWalker`, `splitText`, layout-aware scrolling - and the point
 * of this stub is that a renderer reaching for a browser API it has not declared
 * fails here by absence. The renderer guards both on the elements being present,
 * so their absence is exactly the condition under test: the transcript must
 * render identically in a host that has neither.
 */
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
  /* Read back, because the renderer does: the approval card mints its own
     `aria-labelledby` and then reads it to stamp the heading it points at.
     Without this the card threw, and nothing in this file rendered one. */
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
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
    /** The transcript element itself, for tests that inspect row internals. */
    transcript: byId.get('transcript'),
    /** The rows on screen, in order. The renderer stamps each with its key. */
    rows: () =>
      byId
        .get('transcript')
        .children.filter((n) => n.dataset.key)
        .map((n) => ({ key: n.dataset.key, text: said(n) })),
  };
}

/**
 * What a row *says*, with its hover toolbar left out.
 *
 * Messages carry a per-message action bar (copy, and on your own messages,
 * edit). Those are controls rather than content, and folding their labels into
 * the row's text would make every assertion about what the panel said also an
 * assertion about which buttons it happened to offer.
 */
function said(node) {
  if (node._classes && node._classes.has('msg-actions')) return '';
  if (!node.children.length) return node.textContent;
  return node.children.map(said).join('');
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

// -- the two places the wire says a thing twice ------------------------------

describe('the transcript, when the wire repeats itself', () => {
  it('does not print the plan once as prose and again as a card', () => {
    // The reported "double response". C2 emits the planner's text twice — as
    // `assistant`, then as the `plan` it is parsed into — and that is a property
    // of the wire, not a fault. `RunState` declares the rule and folds them; the
    // panel draws straight from the wire and did not, so every plan appeared
    // twice, the second time with a heading over it.
    const p = panel();
    p.send(
      wire('f1', 1, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('f1', 2, 'assistant', { text: '1. Edit handler/user.go' }),
      wire('f1', 2, 'plan', { text: '1. Edit handler/user.go', steps: 1 }),
    );

    const rows = p.rows();
    assert.equal(saying(rows, 'Edit handler/user.go').length, 1, `printed twice: ${rows.length}`);
    assert.ok(rows.some((r) => r.key.indexOf('/plan:') !== -1), 'the card is the richer one');
    assert.ok(!rows.some((r) => r.key.indexOf('/assistant:') !== -1));
  });

  it('keeps a plan that is not what was just said', () => {
    const p = panel();
    p.send(
      wire('f2', 1, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('f2', 2, 'assistant', { text: 'Let me look at the handler first.' }),
      wire('f2', 2, 'plan', { text: '1. Edit handler/user.go', steps: 1 }),
    );

    assert.equal(p.rows().length, 3, 'a plan that adds something must add a row');
  });

  it('does not state a failure once as an error and again as the summary', () => {
    // The other half of the same rule, and the more common one: a failing run
    // usually finishes with the sentence it already reported.
    const p = panel();
    p.send(
      wire('f3', 1, 'error', { message: 'the model endpoint is unavailable' }),
      wire('f3', 2, 'finish', {
        outcome: 'error',
        summary: 'the model endpoint is unavailable',
        turns: 1,
        mutations: [],
      }),
    );

    assert.equal(saying(p.rows(), 'endpoint is unavailable').length, 1);
  });

  it('keeps an error the finish does not repeat', () => {
    const p = panel();
    p.send(
      wire('f4', 1, 'error', { message: 'go vet: composite literal uses unkeyed fields' }),
      wire('f4', 2, 'finish', {
        outcome: 'unverified',
        summary: 'the gate never came clean',
        turns: 3,
        mutations: [],
      }),
    );

    assert.equal(p.rows().length, 2, 'a distinct cause and outcome are two facts');
  });

  it('does not fold across a turn boundary', () => {
    // `foldable` is only meaningful between two events that arrive together. A
    // plan in turn 2 must not reclaim the row an assistant wrote in turn 1.
    const p = panel();
    p.send(
      wire('f5', 1, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('f5', 2, 'assistant', { text: 'the same words' }),
      wire('f5', 3, 'turn_start', { turn: 2, mode: 'coder' }),
      wire('f5', 4, 'plan', { text: 'the same words', steps: 1 }),
    );

    assert.equal(saying(p.rows(), 'the same words').length, 2);
  });
});

// -- streaming ---------------------------------------------------------------

describe('the transcript, while an answer is being written', () => {
  it('grows one row rather than adding a row per fragment', () => {
    // Deltas share the id of the stored event they precede, so keying on the id
    // alone would still be one row. What actually holds the row together is
    // `openAssistant`, and this is the test that says so.
    const p = panel();
    p.send(
      wire('e1', 3, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('e1', 4, 'assistant_delta', { text: 'The pension handler ' }),
      wire('e1', 4, 'assistant_delta', { text: 'lives in handler/pension.go.' }),
    );

    const rows = p.rows();
    assert.equal(rows.length, 2, `a row per fragment: ${rows.map((r) => r.key)}`);
    assert.equal(rows[1].text, 'The pension handler lives in handler/pension.go.');
  });

  it('lets the authoritative message replace what streamed, not follow it', () => {
    // The `assistant` event is the transcript; the deltas are a view of it being
    // written. Appending would print the answer twice.
    const p = panel();
    p.send(
      wire('e2', 3, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('e2', 4, 'assistant_delta', { text: 'half an ' }),
      wire('e2', 4, 'assistant_delta', { text: 'answer' }),
      wire('e2', 4, 'assistant', { text: 'the whole answer' }),
    );

    const said = p.rows().filter((r) => r.key.indexOf('/assistant:') !== -1);
    assert.equal(said.length, 1, 'the streamed row and the real one are both on screen');
    assert.equal(said[0].text, 'the whole answer');
  });

  it('starts a new row for the next turn rather than growing the last one', () => {
    const p = panel();
    p.send(
      wire('e3', 1, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('e3', 2, 'assistant_delta', { text: 'first' }),
      wire('e3', 2, 'assistant', { text: 'first' }),
      wire('e3', 3, 'turn_start', { turn: 2, mode: 'coder' }),
      wire('e3', 4, 'assistant_delta', { text: 'second' }),
    );

    const said = p.rows().filter((r) => r.key.indexOf('/assistant:') !== -1);
    assert.deepEqual(
      said.map((r) => r.text),
      ['first', 'second'],
      'the second turn was written into the first turn"s row',
    );
  });

  it('does not treat a partial answer as a transcript row to restore', () => {
    // Deltas are never persisted server-side, so a rebuilt panel must not have
    // saved half an answer it will never be sent again.
    const p = panel();
    p.send(
      wire('e4', 1, 'turn_start', { turn: 1, mode: 'planner' }),
      wire('e4', 2, 'assistant_delta', { text: 'half an answer' }),
    );
    const ready = p.posted.filter((m) => m.type === 'ready');
    assert.equal(ready.length, 1, 'the panel announces itself once');
  });
});

// -- opening a different conversation ---------------------------------------

describe('the transcript, when a different session is opened', () => {
  it('shows the conversation that was opened, and only that one', () => {
    // Clicking through the Sessions tree used to accumulate every conversation
    // looked at into one transcript, because nothing told the panel the session
    // had changed: `RunState.reset()` cleared the host's copy and the rows on
    // screen stayed. `showSession` posts the clear before the transcript of the
    // newly-opened session is replayed through it.
    const p = panel();
    p.send(
      wire('a1', 1, 'user', { text: 'the first conversation' }),
      wire('a1', 2, 'assistant', { text: 'answer to the first' }),
    );
    assert.equal(p.rows().length, 2);

    // What `ChatViewProvider.showSession` posts on the switch, ahead of the
    // replayed transcript.
    p.send(
      { type: 'session', id: 'a2' },
      wire('a2', 1, 'user', { text: 'the second conversation' }),
      wire('a2', 2, 'assistant', { text: 'answer to the second' }),
    );

    const rows = p.rows();
    assert.equal(saying(rows, 'the first conversation').length, 0, 'the old conversation stayed');
    assert.equal(saying(rows, 'answer to the first').length, 0, 'the old answers stayed');
    assert.equal(saying(rows, 'the second conversation').length, 1);
    assert.equal(saying(rows, 'answer to the second').length, 1);
  });

  it('accepts the reopened conversation despite its ids starting over', () => {
    // The clear resets the panel's cursor. If it did not, every event of the
    // session just opened would look like one already applied and the panel
    // would come back empty.
    const p = panel();
    p.send(wire('b1', 1, 'assistant', { text: 'first' }), { type: 'session', id: 'b2' });
    p.send(wire('b2', 1, 'assistant', { text: 'reopened' }));
    assert.equal(saying(p.rows(), 'reopened').length, 1, 'the reopened session rendered nothing');
  });

  it('keeps the echo of the message that starts a conversation', () => {
    // The switch is announced *before* the session has produced anything, and
    // on the first message of a new conversation the only thing on screen is the
    // composer's own echo of it. An unconditional clear here deleted the
    // sentence the developer had just typed, a beat after they typed it.
    const p = panel();
    p.send({ type: 'user', text: 'add a Pension resource', steering: false });
    p.send({ type: 'session', id: 'c1' });
    assert.equal(
      saying(p.rows(), 'add a Pension resource').length,
      1,
      'the echo was cleared by the switch that its own message caused',
    );
  });

  it('says nothing about a session it is already showing', () => {
    const p = panel();
    p.send(
      wire('d1', 1, 'user', { text: 'keep me' }),
      wire('d1', 2, 'assistant', { text: 'and me' }),
      { type: 'session', id: 'd1' },
    );
    assert.equal(p.rows().length, 2, 're-announcing the current session cleared it');
  });
});

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

// -- what the panel shows without being asked --------------------------------

/** Every node in a row, so a test can ask which of them exist. */
function all(node, out = []) {
  out.push(node);
  node.children.forEach((c) => all(c, out));
  return out;
}

const nodeOf = (p, part) =>
  p.transcript.children.find((n) => n.dataset.key && n.dataset.key.indexOf(part) !== -1);

describe('the transcript, and how much of it you can read', () => {
  it('holds the whole of a tool result, not a clipped preview of it', () => {
    // The reported complaint, reduced to its mechanism. The old renderer cut
    // anything past three lines to a 3.3em box under `overflow: hidden` and put
    // the rest behind "Open in editor", so a five-line vet failure - the single
    // most common thing this panel prints - could not be read in the panel at
    // all. Every character has to be in the DOM; how much of it is on screen at
    // once is a scroll height, which is a different question.
    const five = ['one', 'two', 'three', 'four', 'five'].join('\n');
    const p = panel();
    p.send(
      wire('d1', 1, 'tool_call', { id: 't1', name: 'go_vet', arguments: { path: 'handler' } }),
      wire('d1', 2, 'tool_result', { id: 't1', name: 'go_vet', ok: false, content: five }),
    );

    const row = p.rows().find((r) => r.key.indexOf('/tool:') !== -1);
    assert.ok(row, 'the tool row is missing');
    for (const line of ['one', 'two', 'three', 'four', 'five']) {
      assert.ok(row.text.indexOf(line) !== -1, `"${line}" was clipped out of the row`);
    }
  });

  it('opens a failure and leaves a success shut', () => {
    // The panel's whole attention policy. Collapsing both meant the one row
    // that explains why a run stopped looked exactly like the thirty that
    // worked - and cost the same two clicks to read.
    const p = panel();
    p.send(
      wire('d2', 1, 'tool_call', { id: 'a', name: 'read_file', arguments: { path: 'x.go' } }),
      wire('d2', 2, 'tool_result', { id: 'a', name: 'read_file', ok: true, content: 'x\ny\nz\nw' }),
      wire('d2', 3, 'tool_call', { id: 'b', name: 'go_build', arguments: {} }),
      wire('d2', 4, 'tool_result', { id: 'b', name: 'go_build', ok: false, content: 'x\ny\nz\nw' }),
    );

    const bodies = (key) =>
      all(nodeOf(p, key)).filter((n) => n._classes && n._classes.has('body'));
    assert.equal(bodies('tool:a')[0].hidden, true, 'a successful read opened itself');
    assert.equal(bodies('tool:b')[0].hidden, false, 'the failure stayed collapsed');
  });

  it('offers a way to read long output in place, not only in an editor', () => {
    const long = Array.from({ length: 60 }, (_, i) => `line ${i}`).join('\n');
    const p = panel();
    p.send(
      wire('d3', 1, 'tool_call', { id: 't', name: 'go_test', arguments: {} }),
      wire('d3', 2, 'tool_result', { id: 't', name: 'go_test', ok: false, content: long }),
    );

    const nodes = all(nodeOf(p, 'tool:t'));
    const scroll = nodes.find((n) => n._classes && n._classes.has('scroll'));
    assert.ok(scroll, 'long output has no scrollable viewport');
    assert.ok(scroll.style.maxHeight, 'the viewport has no height, so it cannot scroll');
    assert.ok(
      nodes.some((n) => n._classes && n._classes.has('dump-bar')),
      'no controls for expanding, wrapping or copying it',
    );
    // And the text is all there regardless of what the viewport shows.
    assert.ok(nodes.some((n) => n.textContent.indexOf('line 59') !== -1), 'the tail was dropped');
  });

  it('elides only what is too large to lay out, and says how much', () => {
    // 40,000 lines in one `<pre>` is tens of megabytes of layout in a webview
    // with a 60 MB budget. The head and tail stay; the middle is named.
    const huge = Array.from({ length: 9000 }, (_, i) => `l${i}`).join('\n');
    const p = panel();
    p.send(
      wire('d4', 1, 'tool_call', { id: 'h', name: 'go_test', arguments: {} }),
      wire('d4', 2, 'tool_result', { id: 'h', name: 'go_test', ok: false, content: huge }),
    );

    const nodes = all(nodeOf(p, 'tool:h'));
    assert.ok(
      nodes.some((n) => n._classes && n._classes.has('elision')),
      'a transcript-destroying payload was laid out whole',
    );
    assert.ok(nodes.some((n) => n.textContent.indexOf('l0\n') !== -1), 'the head is missing');
    assert.ok(nodes.some((n) => n.textContent.indexOf('l8999') !== -1), 'the tail is missing');
  });

  it('renders a table, a numbered list and a link as themselves', () => {
    // All three used to fall through to a paragraph: a table printed as pipes,
    // a numbered list ran together on one line, and a link printed its own
    // brackets and parentheses.
    const p = panel();
    p.send(
      wire('d5', 1, 'assistant', {
        text: [
          '| rule | status |',
          '| --- | --- |',
          '| R-101 | fail |',
          '',
          '1. First step',
          '2. Second step',
          '',
          'See [the contract](https://example.invalid/spec).',
        ].join('\n'),
      }),
    );

    const nodes = all(nodeOf(p, '/assistant:'));
    const tags = nodes.map((n) => n.tagName);
    assert.ok(tags.indexOf('TABLE') !== -1, 'the table rendered as pipes');
    assert.ok(tags.indexOf('OL') !== -1, 'the numbered list rendered as a paragraph');
    const anchor = nodes.find((n) => n.tagName === 'A');
    assert.ok(anchor, 'the link rendered as markdown punctuation');
    assert.equal(anchor.textContent, 'the contract');
    assert.equal(anchor.href, 'https://example.invalid/spec');
  });

  it('never turns a non-http link into an anchor', () => {
    // The renderer builds every node through textContent, so the only way a URL
    // can become executable is by reaching an `href`. It must not.
    const p = panel();
    p.send(
      wire('d6', 1, 'assistant', { text: 'Try [this](javascript:alert(1)) or [that](file:///etc).' }),
    );
    const anchors = all(nodeOf(p, '/assistant:')).filter((n) => n.tagName === 'A');
    assert.equal(anchors.length, 0, `a non-http scheme became a link: ${anchors.map((a) => a.href)}`);
  });

  it('gives a code fence its filename, and the actions that use it', () => {
    const p = panel();
    p.send(
      wire('d7', 1, 'assistant', {
        text: '```go:handler/user.go\nfunc Handle() {}\n```',
      }),
    );
    const nodes = all(nodeOf(p, '/assistant:'));
    assert.ok(
      nodes.some((n) => n._classes && n._classes.has('file') && n.textContent === 'handler/user.go'),
      'the fence`s filename was thrown away',
    );
    assert.ok(nodes.some((n) => n.textContent === 'func Handle() {}'), 'the code is missing');
  });
});

// -- the display settings ----------------------------------------------------

/** A panel whose `init` carries `dakcoder.chat.*` other than the defaults. */
function panelWith(display) {
  const p = panel();
  p.send({ type: 'init', strings: strings(), maxRows: 500, commands: [], mentions: [], display });
  return p;
}

const bodies = (p, key) =>
  all(nodeOf(p, key)).filter((n) => n._classes && n._classes.has('body'));

const twoResults = (p, session) =>
  p.send(
    wire(session, 1, 'tool_call', { id: 'ok', name: 'read_file', arguments: { path: 'x.go' } }),
    wire(session, 2, 'tool_result', { id: 'ok', name: 'read_file', ok: true, content: 'a\nb\nc\nd' }),
    wire(session, 3, 'tool_call', { id: 'no', name: 'go_build', arguments: {} }),
    wire(session, 4, 'tool_result', { id: 'no', name: 'go_build', ok: false, content: 'a\nb\nc\nd' }),
  );

describe('the transcript, under dakcoder.chat settings', () => {
  it('opens everything when asked to', () => {
    const p = panelWith({ expandOutput: 'always', previewLines: 20, syntaxHighlighting: true });
    twoResults(p, 'g1');
    assert.equal(bodies(p, 'tool:ok')[0].hidden, false, 'a success stayed shut under "always"');
    assert.equal(bodies(p, 'tool:no')[0].hidden, false);
  });

  it('opens nothing when asked to, including failures', () => {
    const p = panelWith({ expandOutput: 'never', previewLines: 20, syntaxHighlighting: true });
    twoResults(p, 'g2');
    assert.equal(bodies(p, 'tool:no')[0].hidden, true, 'a failure opened under "never"');
    // And the text is still there to be read on one click - "never" is about
    // attention, never about availability.
    assert.ok(bodies(p, 'tool:no')[0].textContent.indexOf('d') !== -1);
  });

  it('takes the viewport height from the setting', () => {
    const long = Array.from({ length: 80 }, (_, i) => `r${i}`).join('\n');
    const tall = panelWith({ expandOutput: 'failures', previewLines: 50, syntaxHighlighting: true });
    const short = panelWith({ expandOutput: 'failures', previewLines: 6, syntaxHighlighting: true });
    for (const [p, session] of [[tall, 'g3'], [short, 'g4']]) {
      p.send(
        wire(session, 1, 'tool_call', { id: 'v', name: 'go_test', arguments: {} }),
        wire(session, 2, 'tool_result', { id: 'v', name: 'go_test', ok: true, content: long }),
      );
    }
    const heightOf = (p) =>
      all(nodeOf(p, 'tool:v')).find((n) => n._classes && n._classes.has('scroll')).style.maxHeight;
    assert.ok(heightOf(tall).indexOf('50') !== -1, `tall viewport: ${heightOf(tall)}`);
    assert.ok(heightOf(short).indexOf('6') !== -1, `short viewport: ${heightOf(short)}`);
  });

  it('clamps a viewport height a hand-edited settings.json could set', () => {
    const p = panelWith({ expandOutput: 'failures', previewLines: 99999, syntaxHighlighting: true });
    p.send(
      wire('g5', 1, 'tool_call', { id: 'v', name: 'go_test', arguments: {} }),
      wire('g5', 2, 'tool_result', {
        id: 'v',
        name: 'go_test',
        ok: true,
        content: Array.from({ length: 400 }, (_, i) => `r${i}`).join('\n'),
      }),
    );
    const height = all(nodeOf(p, 'tool:v')).find((n) => n._classes && n._classes.has('scroll'))
      .style.maxHeight;
    assert.ok(height.indexOf('99999') === -1, `an unclamped height reached layout: ${height}`);
  });
});

// -- inline markdown ---------------------------------------------------------

const md = (p, session, text) => {
  p.send(wire(session, 1, 'assistant', { text }));
  return all(nodeOf(p, '/assistant:'));
};

const tagged = (nodes, tag) => nodes.filter((n) => n.tagName === tag).map((n) => n.textContent);

describe('the transcript, and inline markdown', () => {
  it('bolds the whole span, not its first character', () => {
    // The alternatives are joined into one regex, so a `\1` written inside the
    // strong rule refers to the *first* group of the joined expression - the
    // code span's backtick fence - and not to its own opening `**`. Written
    // that way, `**fail**` rendered as a bold "f" followed by "ail" and a stray
    // emphasised asterisk.
    const nodes = md(panel(), 'm1', 'R-207 **fail** here');
    assert.deepEqual(tagged(nodes, 'STRONG'), ['fail']);
    assert.equal(tagged(nodes, 'EM').length, 0, 'the closing delimiter leaked out as emphasis');
  });

  it('parses emphasis inside strong rather than looping on it', () => {
    // `inline` recurses, and a `/g` regex keeps `lastIndex` on the object. One
    // shared instance meant the nested call rewound the cursor its own caller
    // was still walking, so the outer loop re-matched what it had just consumed
    // and allocated until the webview died.
    const nodes = md(panel(), 'm2', 'the **gate *never* came clean** today');
    assert.deepEqual(tagged(nodes, 'STRONG'), ['gate never came clean']);
    assert.deepEqual(tagged(nodes, 'EM'), ['never']);
  });

  it('leaves snake_case identifiers alone', () => {
    const nodes = md(panel(), 'm3', 'call fx_provide_handler before fx_invoke_run.');
    assert.equal(tagged(nodes, 'EM').length, 0, 'an identifier was italicised');
  });

  it('does not let an unclosed delimiter swallow the rest of the message', () => {
    const p = panel();
    const nodes = md(p, 'm4', 'a ** b _ c ~~ d');
    assert.equal(tagged(nodes, 'STRONG').length, 0);
    assert.equal(tagged(nodes, 'S').length, 0);
    // The text survives whole: an unmatched delimiter is punctuation, and a
    // renderer that eats the sentence around it is worse than one that ignores
    // the markup.
    assert.equal(nodeOf(p, '/assistant:').textContent.indexOf('a ** b _ c ~~ d'), 0);
  });

  it('renders a checklist as checkboxes rather than as literal brackets', () => {
    const nodes = md(panel(), 'm5', '- [x] scaffolded\n- [ ] registered');
    const boxes = nodes.filter((n) => n.tagName === 'INPUT');
    assert.equal(boxes.length, 2);
    assert.equal(boxes[0].attributes.checked, 'true');
    assert.equal(boxes[1].attributes.checked, undefined);
  });
});

// -- the approval card -------------------------------------------------------

describe('the approval card', () => {
  it('shows the change it is asking about', () => {
    // The card used to ask for a decision without showing the change: a tool, a
    // reason, a path, and a button that opened an editor tab somewhere else.
    const p = panel();
    p.send(
      wire('p1', 1, 'tool_pending', {
        id: 'ap1',
        tool: 'write_file',
        reason: 'writes outside the scaffolded set',
        paths: ['model/pension.go'],
      }),
      {
        type: 'approval-preview',
        id: 'ap1',
        path: 'model/pension.go',
        diff: '--- a/model/pension.go\n+++ b/model/pension.go\n@@ -1,2 +1,3 @@\n package model\n-type P struct{}\n+type P struct {\n+}',
        added: 2,
        removed: 1,
        cut: 0,
      },
    );

    const card = p.transcript.children.find((n) => n.dataset.key === 'ap:ap1');
    assert.ok(card, 'no approval card');
    const text = card.textContent;
    assert.ok(text.indexOf('+type P struct {') !== -1, 'the diff is not in the card');
    assert.ok(text.indexOf('-type P struct{}') !== -1, 'the removed line is not in the card');
    assert.ok(text.indexOf('+2') !== -1 && text.indexOf('1') !== -1, 'no diff stat');
  });

  it('says why there is no diff rather than showing an empty box', () => {
    const p = panel();
    p.send(
      wire('p2', 1, 'tool_pending', { id: 'ap2', tool: 'resource_scaffold', reason: 'r', paths: ['x.go'] }),
      {
        type: 'approval-preview',
        id: 'ap2',
        path: 'x.go',
        diff: '',
        added: 0,
        removed: 0,
        cut: 0,
        why: 'resource_scaffold produces its files from templates when it runs.',
      },
    );
    const card = p.transcript.children.find((n) => n.dataset.key === 'ap:ap2');
    assert.ok(card.textContent.indexOf('from templates when it runs') !== -1);
  });

  it('does not attach a diff to an approval that has already been answered', () => {
    // A decided card is a receipt. A diff of a change that already landed reads
    // as an invitation to reconsider a decision nobody can now change.
    const p = panel();
    p.send(
      wire('p3', 1, 'tool_pending', { id: 'ap3', tool: 'write_file', reason: 'r', paths: ['x.go'] }),
      { type: 'approval-resolved', id: 'ap3', decision: 'accept' },
      { type: 'approval-preview', id: 'ap3', path: 'x.go', diff: '+late', added: 1, removed: 0, cut: 0 },
    );
    const card = p.transcript.children.find((n) => n.dataset.key === 'ap:ap3');
    assert.equal(card.textContent.indexOf('+late'), -1, 'a decided card grew a diff');
  });
});
