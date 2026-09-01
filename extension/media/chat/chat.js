/*
 * The chat renderer. Vanilla DOM, no framework, no dependency.
 *
 * Three rules shape everything below.
 *
 * **The transcript is the event stream.** One event, one row, appended and never
 * re-flowed. Rows resolve in place — a tool row goes from running to ok without
 * moving — so the panel reads the same after four hundred events as after four.
 *
 * **Nothing is built with innerHTML.** Model output, tool output and file paths
 * are all attacker-adjacent text; every string reaches the DOM through
 * `textContent`. The markdown renderer below builds nodes for the same reason,
 * and is deliberately small rather than complete.
 *
 * **Unknown is skipped, never shown as an error.** C2 is additive-only: the
 * reducer's default arm returns null and the row simply does not exist.
 */

(function () {
  'use strict';

  const vs = acquireVsCodeApi();

  const transcript = document.getElementById('transcript');
  const announcer = document.getElementById('announce');
  const meterEl = document.getElementById('meter');
  const input = document.getElementById('input');
  const inputLabel = document.getElementById('input-label');
  const popup = document.getElementById('popup');
  const sendBtn = document.getElementById('send');
  const stopBtn = document.getElementById('stop');
  const windBtn = document.getElementById('wind-down');
  const skipBtn = document.getElementById('skip');
  const offlineEl = document.getElementById('offline');
  const queuedEl = document.getElementById('queued');
  const workingEl = document.getElementById('working');
  const composerEl = document.getElementById('composer');
  const consoleEl = document.getElementById('console');
  const modePill = document.getElementById('mode-pill');
  const keysEl = document.getElementById('keys');

  /** Strings arrive from the host; `vscode.l10n` does not exist in here. */
  let S = {};
  let SLASH = [];
  let MENTIONS = [];
  let MAX_ROWS = 500;

  const restored = vs.getState() || {};
  /** Ordered row descriptors. The render is a pure function of these. */
  let rows = Array.isArray(restored.rows) ? restored.rows : [];
  /**
   * How far through the host's ring this panel has read.
   *
   * The host's own counter, not a wire event id. Event ids restart at 1 for
   * every session, so a cursor made of them cannot order a ring that spans two
   * — and a replay filtered on one silently dropped a new conversation's
   * opening rows because an older one had already reached higher ids.
   */
  let lastSeq = typeof restored.lastSeq === 'number' ? restored.lastSeq : 0;
  /**
   * Which session the events arriving now belong to. Row keys are namespaced by
   * it: ids are unique only *within* a session, so without this the second
   * conversation's `assistant:1` lands on the first one's row and replaces an
   * answer that is already on screen.
   */
  let session = typeof restored.session === 'string' ? restored.session : '';

  const byKey = new Map();
  const nodes = new Map();
  const pendingApprovals = new Set();

  let run = { phase: 'idle' };
  let offlineReason = null;
  /** From `turn_start.attempt`. The gate event itself carries no attempt number,
   *  so the grid's columns come from the turn that produced them. */
  let attempt = 1;
  /** The row deltas are folding into. Not persisted: `assistant_delta` is not
   *  persisted server-side either, and a restored webview waits for the real
   *  `assistant` event rather than inventing a partial one. */
  let openAssistant = null;
  /**
   * Numbers the rows this panel invents — the optimistic echo of a message the
   * developer just typed, and host notices. Restored rather than reset, because
   * a rebuilt panel starts again from 1 and its first echo then collides with a
   * `user:local:1` already in the restored rows: `put` finds the key taken,
   * keeps the old row and repaints the *new* text over it. That is the same
   * overwrite the session namespace fixes for server events.
   */
  let localSeq = typeof restored.localSeq === 'number' ? restored.localSeq : 0;
  /**
   * Which run of this conversation is going.
   *
   * A session now holds every message of a conversation, so it holds several
   * runs. Most rows are keyed by an event id and are distinct anyway; the gate
   * grid is not - it is keyed by kind, because its whole job is to put attempt 1
   * and attempt 2 of *one* run side by side. Without this the second message's
   * gate would be drawn into the first message's grid and overwrite it.
   */
  let runIndex = typeof restored.runIndex === 'number' ? restored.runIndex : 0;
  /**
   * The last row this turn that something following it may turn out to repeat.
   *
   * Two pairs on the wire say the same thing twice, and both are properties of
   * C2 rather than faults: the planner's prose arrives as `assistant` and then
   * again as the `plan` it is parsed into, and a failing run usually repeats its
   * error sentence as the finish summary. `RunState` declares both rules and
   * folds them — but nothing renders from `RunState`, and the panel draws
   * straight from the wire, so the panel printed the plan twice and every
   * failure twice. Not persisted: it is only meaningful between two events that
   * arrive together.
   */
  let foldable = null;
  let domId = 0;

  // ── small helpers ─────────────────────────────────────────────────────────

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function fmt(template, a, b, c) {
    const args = [a, b, c];
    return String(template || '').replace(/\{(\d)\}/g, function (match, i) {
      const value = args[Number(i)];
      return value === undefined || value === null ? match : String(value);
    });
  }

  /**
   * Count-bearing strings pick a variant by `n === 1`. The host ships both;
   * there is no ICU plural in `vscode.l10n`, and one template would render
   * "1 files", which on a revert dialog reads as a defect rather than as prose.
   */
  function plural(base, n) {
    return fmt(S[n === 1 ? base + '_one' : base + '_other'], n);
  }

  function compact(n) {
    if (typeof n !== 'number' || !isFinite(n)) return '?';
    if (Math.abs(n) < 1000) return String(n);
    return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  }

  function post(message) {
    vs.postMessage(message);
  }

  function nextId(prefix) {
    domId += 1;
    return prefix + '-' + domId;
  }

  // ── announcements ─────────────────────────────────────────────────────────

  let sayPending = null;
  let sayTimer = null;
  let sayLast = 0;

  /**
   * One composed sentence per event, into the single sibling status region.
   *
   * Throttled to roughly one every 1.5 s, because a fast run emits faster than
   * anyone can listen and a screen reader that is still reading turn 3 while the
   * run is on turn 9 is worse than silence. Approvals, errors and the finish row
   * are urgent and jump the queue — those are the ones a listener must not miss.
   */
  function say(text, urgent) {
    if (!text) return;
    if (urgent) {
      flushSay(text);
      return;
    }
    sayPending = text;
    if (sayTimer) return;
    const wait = Math.max(0, 1500 - (Date.now() - sayLast));
    sayTimer = setTimeout(function () {
      sayTimer = null;
      if (sayPending) flushSay(sayPending);
    }, wait);
  }

  function flushSay(text) {
    sayPending = null;
    sayLast = Date.now();
    // Cleared first so an identical consecutive sentence is still announced.
    announcer.textContent = '';
    announcer.textContent = text;
  }

  // ── persistence ───────────────────────────────────────────────────────────

  let saveTimer = null;

  function save() {
    if (saveTimer) return;
    saveTimer = setTimeout(function () {
      saveTimer = null;
      vs.setState({
        rows: rows.slice(-MAX_ROWS),
        lastSeq: lastSeq,
        session: session,
        localSeq: localSeq,
        runIndex: runIndex,
      });
    }, 400);
  }

  // ── the row table ─────────────────────────────────────────────────────────

  /**
   * A row key, namespaced by the session the event came from.
   *
   * Wire event ids are monotonic *within a session* and restart at 1 for the
   * next one. Keying on the id alone meant a second conversation's first
   * `assistant` event carried the same key as the first conversation's, so its
   * text was painted over a row that already held an answer — the answer to the
   * previous question simply became the answer to this one.
   *
   * Approvals are exempt: their ids are minted by the runtime and unique across
   * sessions, and the host synthesises `approval_resolved` with no session of
   * its own to name.
   */
  function keyFor(kind, id) {
    return session + '/' + kind + ':' + id;
  }

  function put(row) {
    if (!byKey.has(row.key)) {
      rows.push(row);
      byKey.set(row.key, row);
    }
    // Before paint, not after: the empty state is a sibling in the same flow,
    // and removing it afterwards would land the first row below it.
    syncEmpty();
    paint(row);
    trim();
    save();
    return row;
  }

  function paint(row) {
    const node = render(row);
    if (!node) return;
    node.dataset.key = row.key;
    const old = nodes.get(row.key);
    if (old && old.isConnected) old.replaceWith(node);
    else transcript.appendChild(node);
    nodes.set(row.key, node);
  }

  function trim() {
    while (rows.length > MAX_ROWS) {
      const dropped = rows.shift();
      byKey.delete(dropped.key);
      const node = nodes.get(dropped.key);
      if (node && node.isConnected) node.remove();
      nodes.delete(dropped.key);
    }
  }

  /**
   * Remove the optimistic echo of a message the runtime has now confirmed.
   *
   * The composer draws what was typed straight away, because a round trip the
   * developer can feel makes the panel seem broken. The runtime then records the
   * same message and it arrives as a `user` event. Only one of the two may stay,
   * and it has to be the server's: the echo is keyed by a counter local to this
   * window, so it would be the copy that a replay could not match and a second
   * panel would never have.
   */
  function dropEcho(text) {
    for (let i = rows.length - 1; i >= 0; i -= 1) {
      const row = rows[i];
      // Stop at the first row the server put there. A `user` event always
      // arrives before the run it opens, so its echo can only be inside the run
      // of locally-invented rows at the very end — and bounding the search there
      // is what stops a follow-up that repeats an earlier sentence word for word
      // from deleting the earlier one.
      if (row.key.indexOf('/') !== -1) return false;
      if (row.kind === 'user' && row.pending && row.text === text) {
        removeRow(row.key);
        return true;
      }
    }
    return false;
  }

  function removeRow(key) {
    const at = rows.findIndex(function (row) {
      return row.key === key;
    });
    if (at !== -1) rows.splice(at, 1);
    byKey.delete(key);
    const node = nodes.get(key);
    if (node && node.isConnected) node.remove();
    nodes.delete(key);
  }

  /** Trim-equal and not empty. Two blank rows are not the same sentence. */
  function sameText(a, b) {
    const left = String(a || '').trim();
    return left.length > 0 && left === String(b || '').trim();
  }

  function clearAll() {
    rows = [];
    lastSeq = 0;
    session = '';
    localSeq = 0;
    runIndex = 0;
    byKey.clear();
    nodes.clear();
    pendingApprovals.clear();
    openAssistant = null;
    transcript.textContent = '';
    syncEmpty();
    updateSkip();
    save();
  }

  // ── icons ─────────────────────────────────────────────────────────────────

  const SVG_NS = 'http://www.w3.org/2000/svg';

  /**
   * Inline SVG, built through the DOM rather than through `innerHTML`.
   *
   * The panel's CSP has no `unsafe-inline`, but that is not the reason: an
   * `innerHTML` sink anywhere in a renderer that also draws tool output is one
   * refactor away from being an injection point, and there is no version of
   * that trade worth making for four decorative glyphs.
   *
   * Everything drawn here is `aria-hidden`. The state it depicts is always also
   * a word in the same row, which is what the announcements read.
   */
  function icon(shapes, size, stroke) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', String(size || 12));
    svg.setAttribute('height', String(size || 12));
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', String(stroke || 1.8));
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    shapes.forEach(function (shape) {
      if (typeof shape === 'string') {
        const path = document.createElementNS(SVG_NS, 'path');
        path.setAttribute('d', shape);
        svg.appendChild(path);
        return;
      }
      const circle = document.createElementNS(SVG_NS, 'circle');
      circle.setAttribute('cx', String(shape[0]));
      circle.setAttribute('cy', String(shape[1]));
      circle.setAttribute('r', String(shape[2]));
      svg.appendChild(circle);
    });
    return svg;
  }

  const GLYPHS = {
    cube: ['M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z', 'M3 7.5 12 12l9-4.5M12 12v9'],
    check: ['m5 13 4 4 10-10'],
    cross: ['M6 6l12 12M18 6 6 18'],
    pencil: ['M4 20h4l10-10-4-4L4 16z'],
    released: [[12, 12, 8], 'M7 17 17 7'],
    clock: [[12, 12, 8.5], 'M12 7.5V12l3 2'],
    warn: ['M12 3 2 20h20z', 'M12 9v5M12 17v.5'],
    offline: [
      'M4 4l16 16M9 17h6M6.5 13.5a8 8 0 0 1 4-2.2M17.5 13.5a8 8 0 0 0-2.4-1.7' +
        'M3.5 10a13 13 0 0 1 4-2.6M20.5 10a13 13 0 0 0-9-3.4',
    ],
  };

  // ── the idle empty state ──────────────────────────────────────────────────

  /**
   * What an empty panel says.
   *
   * It is bottom-aligned so the sentence telling you to type sits directly
   * above the box you type into, and the four suggestions are real buttons that
   * fill the composer rather than send — the developer still has to say what
   * they want scaffolded, and sending `/scaffold` alone would start a run with
   * no subject.
   *
   * Kept out of `rows` entirely. It is not an event, it must not be persisted
   * into `vs.setState`, and it must not survive the first thing that happens.
   */
  const SUGGESTIONS = [
    ['/scaffold', 'cmdScaffold'],
    ['/audit', 'cmdAudit'],
    ['/migrate', 'cmdMigrate'],
    ['/debug', 'cmdDebug'],
  ];

  let emptyNode = null;

  function syncEmpty() {
    const wanted = rows.length === 0;
    if (wanted === Boolean(emptyNode && emptyNode.isConnected)) return;
    if (!wanted) {
      if (emptyNode && emptyNode.isConnected) emptyNode.remove();
      emptyNode = null;
      return;
    }
    emptyNode = renderEmpty();
    transcript.appendChild(emptyNode);
  }

  function renderEmpty() {
    const wrap = el('div', 'empty');

    const lede = el('div', 'lede');
    const mark = el('div', 'mark');
    mark.appendChild(icon(GLYPHS.cube, 26, 1.4));
    lede.appendChild(mark);
    lede.appendChild(el('h2', null, S.emptyTitle || ''));
    lede.appendChild(el('p', null, S.emptySubtitle || ''));
    wrap.appendChild(lede);

    const list = el('ul', 'suggestions');
    list.appendChild(el('li', 'eyebrow', S.suggestions || ''));
    SUGGESTIONS.forEach(function (pair) {
      const item = el('li');
      const button = el('button', 'suggestion');
      button.type = 'button';
      button.appendChild(el('span', 'cmd', pair[0]));
      button.appendChild(el('span', 'what', S[pair[1]] || ''));
      button.addEventListener('click', function () {
        input.value = pair[0] + ' ';
        autosize();
        input.focus();
        refresh();
      });
      item.appendChild(button);
      list.appendChild(item);
    });
    wrap.appendChild(list);
    return wrap;
  }

  // ── disclosure ────────────────────────────────────────────────────────────

  const LONG_CHARS = 250;
  const LONG_LINES = 3;

  function isLong(text) {
    return text.length > LONG_CHARS || text.split('\n').length > LONG_LINES;
  }

  /**
   * The second and third levels of disclosure.
   *
   * Short output sits inline in the hairline body. Anything past ~250 characters
   * or three lines is clamped and handed to a real editor instead, because a
   * 4,000-line `go test` log rendered into the panel destroys the density that
   * makes a long run readable — and an editor gives search, folding and
   * highlighting the panel would have to reimplement badly.
   */
  function bodyFor(text, language) {
    const body = el('div', 'body');
    if (!isLong(text)) {
      body.appendChild(el('pre', null, text));
      return body;
    }
    const clamp = el('div', 'clamp', text);
    body.appendChild(clamp);

    const actions = el('div', 'overflow-actions');
    const open = el('button', 'small', S.openInEditor);
    open.type = 'button';
    open.addEventListener('click', function () {
      post({ type: 'open-in-editor', content: text, language: language || 'plaintext' });
    });
    actions.appendChild(open);
    actions.appendChild(copyButton(text));
    body.appendChild(actions);
    return body;
  }

  function copyButton(text) {
    const button = el('button', 'small', S.copy);
    button.type = 'button';
    button.addEventListener('click', function () {
      // Through the host: `navigator.clipboard` is unreliable in a webview that
      // does not have focus, which is exactly when a copy button gets clicked.
      post({ type: 'copy', text: text });
      say(S.copied, true);
    });
    return button;
  }

  /** A row whose label is a disclosure button for its own body. */
  function shell(row, glyph, label, meta, body) {
    const node = el('div', 'row ' + (row.state || ''));
    node.appendChild(el('span', 'glyph', glyph));

    if (body) {
      const bodyId = nextId('body');
      body.id = bodyId;
      body.hidden = true;
      const toggle = el('button', 'toggle', label);
      toggle.type = 'button';
      toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-controls', bodyId);
      toggle.addEventListener('click', function () {
        const open = body.hidden;
        body.hidden = !open;
        toggle.setAttribute('aria-expanded', String(open));
        toggle.title = open ? S.hide : S.show;
      });
      toggle.title = S.show;
      node.appendChild(toggle);
    } else {
      node.appendChild(el('span', 'label', label));
    }

    node.appendChild(el('span', 'meta', meta || ''));
    if (body) node.appendChild(body);
    return node;
  }

  /**
   * A list of files a tool wrote, as one reviewable changeset.
   *
   * `resource_scaffold` writes seven files in a single call: seven separate
   * rows is the wrong ceremony for one logical action, and a bare list of paths
   * loses the verb — whether a file was created or overwritten is the first
   * thing anyone wants to know.
   *
   * A protected path says *why* it is protected rather than only that it is,
   * because "generated" and "holds credentials" call for different responses.
   */
  function changeset(mutations) {
    const wrap = el('div', 'changeset');
    const head = el('div', 'changeset-head');
    head.appendChild(el('span', null, S.changeset || ''));
    head.appendChild(el('span', 'count', plural('files', mutations.length)));
    wrap.appendChild(head);
    mutations.forEach(function (m) {
      const row = el('div', 'changeset-row');
      const kind = el('span', 'kind', S['kind.' + m.kind] || m.kind);
      kind.setAttribute('data-kind', m.kind);
      row.appendChild(kind);
      row.appendChild(el('span', 'file', m.path));
      if (m.protected) row.appendChild(el('span', 'why', protectedReason(m.path)));
      wrap.appendChild(row);
    });
    return wrap;
  }

  /** Why this path is protected, in the words that suggest what to do instead. */
  function protectedReason(path) {
    if (/_validator\.go$/.test(path)) return S.protectedGenerated;
    if (/(^|\/)configs\//.test(path)) return S.protectedCredentials;
    if (/(^|\/)db\//.test(path)) return S.protectedSchema;
    return S.protectedStructural;
  }

  function pathList(paths, protectedPaths) {
    const wrap = el('div', 'paths');
    const guarded = protectedPaths || [];
    paths.forEach(function (path) {
      const button = el('button', 'path');
      button.type = 'button';
      button.appendChild(document.createTextNode(path));
      if (guarded.indexOf(path) !== -1) {
        button.appendChild(document.createTextNode(' '));
        button.appendChild(el('span', 'badge', '[' + S.protectedPath + ']'));
      }
      button.addEventListener('click', function () {
        post({ type: 'open-path', path: path });
      });
      wrap.appendChild(button);
    });
    return wrap;
  }

  // ── markdown ──────────────────────────────────────────────────────────────

  const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(_[^_]+_)|(https?:\/\/[^\s<>)]+)/g;

  function inline(text, parent) {
    let cursor = 0;
    let match;
    INLINE.lastIndex = 0;
    while ((match = INLINE.exec(text)) !== null) {
      if (match.index > cursor) {
        parent.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      }
      if (match[1]) parent.appendChild(el('code', null, match[1].slice(1, -1)));
      else if (match[2]) parent.appendChild(el('strong', null, match[2].slice(2, -2)));
      else if (match[3]) parent.appendChild(el('em', null, match[3].slice(1, -1)));
      else {
        const link = el('a', null, match[4]);
        link.href = match[4];
        parent.appendChild(link);
      }
      cursor = match.index + match[0].length;
    }
    if (cursor < text.length) parent.appendChild(document.createTextNode(text.slice(cursor)));
  }

  function markdown(text) {
    const root = el('div', 'assistant');
    const lines = String(text || '').split(/\r?\n/);
    let i = 0;

    while (i < lines.length) {
      const fence = /^```(\S*)\s*$/.exec(lines[i]);
      if (fence) {
        const language = fence[1] || 'plaintext';
        const collected = [];
        i += 1;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) {
          collected.push(lines[i]);
          i += 1;
        }
        i += 1;
        root.appendChild(codeBlock(collected.join('\n'), language));
        continue;
      }

      const heading = /^(#{1,4})\s+(.*)$/.exec(lines[i]);
      if (heading) {
        const node = el('h' + Math.min(3, heading[1].length));
        inline(heading[2], node);
        root.appendChild(node);
        i += 1;
        continue;
      }

      if (/^\s*[-*]\s+/.test(lines[i])) {
        const list = el('ul');
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
          const item = el('li');
          inline(lines[i].replace(/^\s*[-*]\s+/, ''), item);
          list.appendChild(item);
          i += 1;
        }
        root.appendChild(list);
        continue;
      }

      if (!lines[i].trim()) {
        i += 1;
        continue;
      }

      const paragraph = [];
      while (i < lines.length && lines[i].trim() && !/^```/.test(lines[i])) {
        paragraph.push(lines[i]);
        i += 1;
      }
      const node = el('p');
      inline(paragraph.join('\n'), node);
      root.appendChild(node);
    }
    return root;
  }

  function codeBlock(code, language) {
    const block = el('div', 'code');
    const head = el('div', 'code-head');
    head.appendChild(el('span', 'name', language));
    head.appendChild(el('span', 'spacer'));
    head.appendChild(copyButton(code));
    if (isLong(code)) {
      const open = el('button', 'small', S.openInEditor);
      open.type = 'button';
      open.addEventListener('click', function () {
        post({ type: 'open-in-editor', content: code, language: language });
      });
      head.appendChild(open);
    }
    block.appendChild(head);
    block.appendChild(el('pre', null, code));
    return block;
  }

  // ── renderers ─────────────────────────────────────────────────────────────

  function render(row) {
    switch (row.kind) {
      case 'user':
        return renderUser(row);
      case 'assistant':
        return markdown(row.text);
      case 'turn':
        return renderTurn(row);
      case 'tool':
        return renderTool(row);
      case 'plan':
        return renderPlan(row);
      case 'gate':
        return renderGate(row);
      case 'compaction':
        return renderCompaction(row);
      case 'approval':
        return renderApproval(row);
      case 'finish':
        return renderFinish(row);
      case 'steer':
        return shell({ state: '' }, '↩', S.steerApplied, '');
      case 'quota':
        return renderQuota(row);
      case 'error':
        return shell({ state: 'fail' }, '✗', S.error, '', bodyFor(row.message, 'plaintext'));
      case 'notice':
        return shell({ state: row.level === 'error' ? 'fail' : '' }, 'ℹ', row.text, '');
      default:
        // A row kind this build does not know about is a row it does not draw.
        return null;
    }
  }

  function renderUser(row) {
    const node = el('div', 'row user');
    node.appendChild(el('span', 'who', S.you));
    node.appendChild(document.createTextNode(row.text));
    if (row.steering) {
      node.appendChild(document.createElement('br'));
      node.appendChild(el('span', 'chip', S.queuedChip));
    }
    return node;
  }

  function renderTurn(row) {
    const label = fmt(S.turn, row.turn, row.mode || '');
    const meta = row.attempt && row.attempt > 1 ? fmt(S.attempt, row.attempt) : '';
    return shell({ state: '' }, '›', label, meta);
  }

  function renderTool(row) {
    const glyph = row.state === 'ok' ? '✓' : row.state === 'fail' ? '✗' : '·';
    const word =
      row.state === 'ok' ? S.toolOk : row.state === 'fail' ? S.toolFailed : S.toolRunning;
    const label = row.name + (row.summary ? ' ' + row.summary : '');

    const parts = [word];
    if (row.lines) parts.push(plural('lines', row.lines));
    if (typeof row.ms === 'number') parts.push(fmt(S.ms, row.ms));
    const meta = parts.join(' · ');

    let body = null;
    if (row.content || row.fix || (row.mutations && row.mutations.length)) {
      body = el('div', 'body');
      if (row.content) {
        const inner = bodyFor(row.content, row.language || 'plaintext');
        while (inner.firstChild) body.appendChild(inner.firstChild);
      }
      if (row.truncated) body.appendChild(el('p', 'footnote', S.truncated));
      if (row.fix) body.appendChild(el('p', 'footnote', fmt(S.fixHint, row.fix)));
      if (row.mutations && row.mutations.length) {
        body.appendChild(changeset(row.mutations));
      }
    }
    return shell({ state: row.state === 'running' ? 'running' : row.state }, glyph, label, meta, body);
  }

  function renderPlan(row) {
    const parsed = parsePlan(row.text);
    const wrap = el('div', 'plan');
    wrap.appendChild(el('h2', null, plural('plan', row.steps || parsed.steps.length)));
    if (parsed.goal) {
      const goal = el('p');
      goal.appendChild(el('span', 'accepts', S.planGoal + ': '));
      goal.appendChild(document.createTextNode(parsed.goal));
      wrap.appendChild(goal);
    }

    const list = el('ol');
    parsed.steps.forEach(function (step) {
      const item = el('li');
      // A dash, always. See the footnote — no field carries per-step status and
      // deriving one from gate results would be a fabrication.
      const status = el('span', 'status', S.planStatusUnknown);
      status.setAttribute('aria-label', S.planStatusUnknown);
      item.appendChild(status);
      const text = el('span');
      text.appendChild(document.createTextNode(step.text));
      if (step.accepts) {
        text.appendChild(document.createElement('br'));
        text.appendChild(el('span', 'accepts', fmt(S.planAccepts, step.accepts)));
      }
      item.appendChild(text);
      list.appendChild(item);
    });
    wrap.appendChild(list);

    if (parsed.scope.length) {
      wrap.appendChild(el('p', 'footnote', S.planScope));
      wrap.appendChild(pathList(parsed.scope, []));
    }
    wrap.appendChild(el('p', 'footnote', S.planFootnote));
    return wrap;
  }

  /** The server's own step regex, kept identical so the count agrees. */
  const STEP = /^\s*\d+[.)]\s/;

  function parsePlan(text) {
    const lines = String(text || '').split(/\r?\n/);
    const steps = [];
    const scope = [];
    let goal = '';
    let current = null;

    lines.forEach(function (line) {
      const paths = line.match(/\b([\w./-]+\.go|go\.mod|[\w./-]+\.sql|[\w./-]+\.ya?ml)\b/g);
      if (paths) {
        paths.forEach(function (p) {
          if (scope.indexOf(p) === -1) scope.push(p);
        });
      }
      if (STEP.test(line)) {
        current = { text: line.replace(STEP, '').trim(), accepts: '' };
        steps.push(current);
        return;
      }
      const accepts = /^\s*Accepts?:\s*(.+)$/i.exec(line);
      if (accepts && current) {
        current.accepts = accepts[1].trim();
        return;
      }
      if (!goal && line.trim()) goal = line.trim();
    });
    return { goal: goal, steps: steps, scope: scope };
  }

  /**
   * The gate as a stage x attempt convergence grid.
   *
   * A list of gate runs answers "did it pass?"; the grid answers "is it getting
   * closer?", which is the question during a retry. Attempt numbers come from
   * `turn_start`, not from the gate event — the event carries no attempt field.
   */
  function renderGate(row) {
    const wrap = el('div', 'grid-wrap');
    const table = el('table', 'grid');
    table.appendChild(el('caption', null, row.gkind === 'inner' ? S.gateInner : S.gateFull));

    const names = [];
    row.attempts.forEach(function (a) {
      (a.stages || []).forEach(function (stage) {
        if (names.indexOf(stage.name) === -1) names.push(stage.name);
      });
      (a.not_run || []).forEach(function (name) {
        if (names.indexOf(name) === -1) names.push(name);
      });
    });

    const head = el('tr');
    const corner = el('th', null, S.gateStage);
    corner.scope = 'col';
    head.appendChild(corner);
    row.attempts.forEach(function (a) {
      const th = el('th', null, fmt(S.gateAttemptCol, a.attempt));
      th.scope = 'col';
      head.appendChild(th);
    });
    const thead = el('thead');
    thead.appendChild(head);
    table.appendChild(thead);

    // The stage that is holding the gate up. Named so the row can carry the
    // amber, matching the console cell that says the same thing — one fact, one
    // colour, in both places it appears.
    const lastAttempt = row.attempts[row.attempts.length - 1];
    const blocking = lastAttempt && !lastAttempt.ok ? lastAttempt.blocked_by : '';

    const tbody = el('tbody');
    names.forEach(function (name) {
      const tr = el('tr', name && name === blocking ? 'blocking' : '');
      const th = el('th', null, name);
      th.scope = 'row';
      tr.appendChild(th);
      row.attempts.forEach(function (a) {
        tr.appendChild(cellFor(a, name));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);

    const last = lastAttempt;
    if (last) {
      wrap.appendChild(
        el('p', 'footnote', last.ok ? fmt(S.gateConverged, last.attempt) : S.gateOpen),
      );

      /*
       * Why a cell is a dash, spelled out under the grid.
       *
       * The cell itself is one character wide by necessity, so without this the
       * reason exists only in a tooltip — which is unreachable by keyboard and
       * invisible in the screenshot someone pastes into a support chat. Skipped
       * and not-run keep separate lines because they call for different things:
       * one is a missing tool, the other is a stage the gate never got to.
       */
      const reasons = [];
      (last.stages || []).forEach(function (stage) {
        if (stage.skipped) reasons.push(stage.name + ' ' + fmt(S.gateSkipped, stage.skipped));
      });
      (last.not_run || []).forEach(function (name) {
        reasons.push(name + ' ' + S.gateNotRun);
      });
      reasons.forEach(function (line) {
        wrap.appendChild(el('p', 'footnote', line));
      });

      /*
       * The blocked gate gets the design's one raised surface, the same as an
       * approval card — because it is the same kind of moment. The run has
       * stopped and it is waiting for a person, and a footnote saying so reads
       * like commentary on a grid rather than like something addressed to the
       * reader.
       *
       * No buttons. `diagnostics.offerGateRerun` already offers "Run {stage}
       * locally" as an action, and putting a second copy here would need a new
       * webview-to-host message for no behaviour the developer does not
       * already have.
       */
      if (!last.ok && last.blocked_by) {
        const card = el('section', 'notice-card gated');
        const head = el('div', 'head');
        const mark = el('span', 'mark');
        mark.appendChild(icon(GLYPHS.warn, 13, 1.9));
        head.appendChild(mark);
        head.appendChild(el('span', null, fmt(S.gateBlocked, last.blocked_by)));
        card.appendChild(head);
        card.appendChild(el('p', null, fmt(S.gateBlockedWhy, last.blocked_by)));
        wrap.appendChild(card);
      }
      // Failure output belongs behind a disclosure, one per failing stage, so a
      // grid stays a grid rather than becoming a wall of compiler errors.
      (last.stages || []).forEach(function (stage) {
        if (stage.ok || !stage.content) return;
        // The word as well as the seconds. In the grid above, a failure is a
        // glyph in its own cell; down here the row is a stage name and a
        // duration, and without the word the only failure signal left would be
        // the colour.
        wrap.appendChild(
          shell(
            { state: 'fail' },
            '✗',
            stage.name,
            S.toolFailed + ' · ' + fmt(S.gateSeconds, stage.seconds),
            bodyFor(stage.content, 'go'),
          ),
        );
      });
    }
    return wrap;
  }

  /**
   * One cell: a glyph, and the sentence behind it.
   *
   * The visible mark is punctuation — the attempt columns are 52px, and a cell
   * reading "— skipped: govulncheck is not installed" would either wrap the
   * grid into unreadability or be clipped to nothing. The whole sentence lives
   * in `aria-label` and `title`, so a screen reader and a hover both get it,
   * and the reasons repeat as footnotes under the grid where they can be read
   * without pointing at anything.
   *
   * Four marks, not three. `·` is "this stage never ran in this attempt" and
   * `—` is "this stage was deliberately skipped, and there is a reason below".
   * Collapsing them would hide the distinction that decides whether the
   * developer has anything to fix.
   */
  function cellFor(a, name) {
    let stage = null;
    (a.stages || []).forEach(function (s) {
      if (s.name === name) stage = s;
    });
    if (!stage) {
      const missing = (a.not_run || []).indexOf(name) !== -1;
      return gateCell(missing ? 'skip' : 'absent', missing ? '—' : '·', missing ? S.gateNotRun : S.gateAbsent);
    }
    if (stage.skipped) return gateCell('skip', '—', fmt(S.gateSkipped, stage.skipped));
    const word = stage.ok ? S.gatePassed : S.gateFailed;
    return gateCell(
      stage.ok ? 'pass' : 'failcell',
      stage.ok ? '✓' : '✗',
      word + ' · ' + fmt(S.gateSeconds, stage.seconds),
    );
  }

  function gateCell(cls, glyph, label) {
    const td = el('td', cls, glyph);
    td.setAttribute('aria-label', label);
    td.title = label;
    return td;
  }

  /**
   * The quota card.
   *
   * Only what the gateway sent. `pct` is the server's own figure rather than
   * `used / cap` recomputed here — the two can legitimately differ, because the
   * tightest limit may be a weighted one, and a client that recomputes shows a
   * number the server would not agree with.
   *
   * The bar is decorative and `aria-hidden`; the percentage beside it is the
   * accessible value, so nothing here depends on seeing a coloured strip.
   */
  function renderQuota(row) {
    const card = el('section', 'notice-card plain');

    const head = el('div', 'head');
    const mark = el('span', 'mark');
    mark.appendChild(icon(GLYPHS.clock, 12, 1.8));
    head.appendChild(mark);
    head.appendChild(el('span', null, row.text));
    if (typeof row.pct === 'number') {
      head.appendChild(el('span', 'spacer'));
      head.appendChild(el('span', 'pct', Math.round(row.pct) + '%'));
    }
    card.appendChild(head);

    if (typeof row.pct === 'number') {
      const bar = el('div', 'bar');
      bar.setAttribute('aria-hidden', 'true');
      const fill = el('i');
      fill.style.width = Math.max(0, Math.min(100, Math.round(row.pct))) + '%';
      bar.appendChild(fill);
      card.appendChild(bar);
    }

    const caption = [];
    if (typeof row.pct === 'number' && row.name) {
      caption.push(fmt(S.quotaClosest, row.name, Math.round(row.pct)));
    }
    if (typeof row.resets === 'number') {
      caption.push(fmt(S.quotaResets, duration(row.resets)));
    }
    if (caption.length) card.appendChild(el('span', 'caption', caption.join(' ')));
    return card;
  }

  /** Coarse on purpose: a window that resets in "2h 41m" does not need seconds. */
  function duration(seconds) {
    if (seconds < 60) return plural('seconds', Math.max(0, Math.round(seconds)));
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return plural('minutes', minutes);
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? fmt(S.hoursMinutes, hours, rest) : plural('hours', hours);
  }

  function renderCompaction(row) {
    // Collapsed by default: a compaction is bookkeeping, and expanding it is a
    // deliberate act rather than something a developer scrolls past every turn.
    return shell(
      { state: '' },
      '⤡',
      fmt(S.compaction, compact(row.before), compact(row.after)),
      fmt(S.gateSeconds, row.seconds),
    );
  }

  function renderApproval(row) {
    const card = el('section', 'approval');
    card.id = 'approval-' + row.id;
    card.tabIndex = -1;

    // Tool above, subject below. The tool name is what the policy is about and
    // the path is what the decision is about, and putting them on one line made
    // the line that mattered wrap first at a 340px width.
    const subject = (row.paths && row.paths[0]) || row.tool;

    /*
     * A decided approval is a receipt, not a request.
     *
     * Built here rather than by appending an outcome to the live card, because
     * almost nothing on the live card still applies: "Approval needed" is no
     * longer true, and the reason it gives is an argument for a decision that
     * has already been made. What survives is what happened, to what, and — in
     * the three cases where the word alone is not the whole story — why.
     */
    if (row.decision) {
      const spec = DECIDED[row.decision] || DECIDED.accept;
      card.classList.add('decided');

      const outcome = el('p', 'outcome ' + spec[0]);
      const mark = el('span', 'mark');
      mark.appendChild(icon(GLYPHS[spec[1]], 13, spec[1] === 'check' ? 2.4 : 1.9));
      outcome.appendChild(mark);
      outcome.appendChild(el('span', null, S[spec[2]] || ''));
      outcome.appendChild(el('span', 'tool', row.tool));
      card.setAttribute('aria-labelledby', nextId('ap'));
      outcome.id = card.getAttribute('aria-labelledby');
      card.appendChild(outcome);

      card.appendChild(el('span', 'subject', subject));
      if (row.receipt) card.appendChild(el('span', 'receipt', row.receipt));
      return card;
    }

    const kicker = el('span', 'kicker', fmt(S.approvalTitle, row.tool));
    const title = el('h2', null, subject);
    card.setAttribute('aria-labelledby', nextId('ap'));
    title.id = card.getAttribute('aria-labelledby');
    // The heading is the path alone, so the accessible name restores the tool.
    title.setAttribute('aria-label', fmt(S.approvalTitle, row.tool) + ' — ' + subject);
    card.appendChild(kicker);
    card.appendChild(title);

    const dl = el('dl');
    dl.appendChild(el('dt', null, S.approvalReason));
    dl.appendChild(el('dd', null, row.reason));
    if (row.paths && row.paths.length) {
      dl.appendChild(el('dt', null, S.approvalPaths));
      const dd = el('dd');
      dd.appendChild(pathList(row.paths, row.protectedPaths || []));
      dl.appendChild(dd);
    }
    card.appendChild(dl);

    if (row.protectedPaths && row.protectedPaths.length) {
      card.appendChild(el('p', 'warn', '⚠ ' + S.approvalProtected));
    }
    if (row.unconditional) {
      card.appendChild(el('p', 'warn', '⚠ ' + S.approvalUnconditional));
    }

    const buttons = el('div', 'buttons');
    buttons.appendChild(decisionButton(row.id, 'accept', S.accept, 'primary'));

    const diff = el('button', 'secondary', S.showDiff);
    diff.type = 'button';
    diff.addEventListener('click', function () {
      post({ type: 'show-diff', id: row.id });
    });
    buttons.appendChild(diff);
    buttons.appendChild(decisionButton(row.id, 'edit', S.editArgs, 'secondary'));
    // Reject sits after the spacer, away from Accept. Two decisions of opposite
    // consequence should not be adjacent targets.
    buttons.appendChild(el('span', 'spacer'));
    buttons.appendChild(decisionButton(row.id, 'reject', S.reject, 'danger'));
    card.appendChild(buttons);

    if (typeof row.secondsLeft === 'number') {
      card.appendChild(el('span', 'countdown', fmt(S.approvalCountdown, row.secondsLeft)));
    }
    return card;
  }

  /** decision → [css class, glyph, string key]. `released` is the timeout. */
  const DECIDED = {
    accept: ['accepted', 'check', 'decidedAccept'],
    reject: ['rejected', 'cross', 'decidedReject'],
    edit: ['edited', 'pencil', 'decidedEdit'],
    released: ['released', 'released', 'decidedReleased'],
  };

  /**
   * The sentence under the outcome, for the three cases where the word alone
   * does not say what happened. "Accepted" needs no gloss; "released" does,
   * because the developer did not do it and it was recorded as a rejection.
   */
  function receiptFor(decision, tool, auto) {
    if (decision === 'released') return fmt(S.receiptReleased, tool);
    if (decision === 'edit') return fmt(S.receiptEdited, tool);
    if (auto) return S.receiptAuto || '';
    return '';
  }

  function decisionButton(id, decision, label, cls) {
    const button = el('button', cls, label);
    button.type = 'button';
    button.addEventListener('click', function () {
      post({ type: 'decide', id: id, decision: decision });
    });
    return button;
  }

  const FINISH = {
    running: ['·', 'finishRunning', ''],
    done: ['✓', 'finishDone', 'ok'],
    unverified: ['!', 'finishUnverified', 'fail'],
    no_progress: ['=', 'finishNoProgress', 'fail'],
    exhausted: ['⏱', 'finishExhausted', 'fail'],
    error: ['✗', 'finishError', 'fail'],
    aborted: ['■', 'finishAborted', ''],
  };

  function renderFinish(row) {
    const spec = FINISH[row.outcome] || ['·', 'finishRunning', ''];
    const parts = [];
    if (typeof row.turns === 'number') parts.push(plural('turns', row.turns));
    const mutations = row.mutations || [];
    if (mutations.length) parts.push(plural('files', mutations.length));

    let body = null;
    if (row.summary || mutations.length) {
      body = el('div', 'body');
      if (row.summary) body.appendChild(el('pre', null, row.summary));
      if (mutations.length) body.appendChild(pathList(mutations, []));
    }
    const node = shell({ state: spec[2] }, spec[0], S[spec[1]], parts.join(' · '), body);
    node.classList.add('finish');
    return node;
  }

  // ── the event reducer ─────────────────────────────────────────────────────

  function applyEvent(event) {
    const d = event.data || {};

    switch (event.type) {
      /*
       * The developer's own message, as the runtime recorded it. The composer
       * has usually drawn it already — optimistically, so typing feels
       * immediate — and that echo is dropped here rather than left to sit above
       * an identical row. Adopting the server's copy is what makes the message
       * survive a panel rebuild: the echo is this window's invention and the
       * event is in the transcript.
       */
      case 'user': {
        const text = String(d.text || '');
        dropEcho(text);
        put({ key: keyFor('user', event.id), kind: 'user', text: text, steering: false });
        return;
      }

      case 'turn_start': {
        attempt = typeof d.attempt === 'number' ? d.attempt : 1;
        openAssistant = null;
        foldable = null;
        put({
          key: keyFor('turn', event.id),
          kind: 'turn',
          turn: d.turn,
          mode: d.mode,
          attempt: attempt,
        });
        return;
      }

      case 'assistant_delta': {
        if (!openAssistant) {
          openAssistant = put({ key: keyFor('assistant', event.id), kind: 'assistant', text: '' });
        }
        openAssistant.text += String(d.text || '');
        paint(openAssistant);
        return;
      }

      case 'assistant': {
        // The authoritative text replaces whatever the deltas folded together.
        const row =
          openAssistant || put({ key: keyFor('assistant', event.id), kind: 'assistant', text: '' });
        row.text = String(d.text || '');
        paint(row);
        openAssistant = null;
        foldable = { key: row.key, kind: 'assistant', text: row.text };
        save();
        return;
      }

      case 'tool_call': {
        openAssistant = null;
        put({
          key: keyFor('tool', d.id),
          kind: 'tool',
          id: d.id,
          name: String(d.name || ''),
          summary: argSummary(d.arguments),
          state: 'running',
        });
        return;
      }

      case 'tool_result': {
        const row = byKey.get(keyFor('tool', d.id)) || put({
          key: keyFor('tool', d.id),
          kind: 'tool',
          id: d.id,
          name: String(d.name || ''),
          state: 'running',
        });
        const content = String(d.content || '');
        // A call answered from the loop's ledger never sent `tool_call`, so the
        // row above was created here with no arguments on it. Those results
        // carry their own, and without this a repeating run reads as a column
        // of bare tool names — which is the one case where the arguments are
        // what tell you what the loop is about.
        if (!row.summary && d.arguments) row.summary = argSummary(d.arguments);
        row.state = d.ok ? 'ok' : 'fail';
        row.content = content;
        row.lines = content ? content.split('\n').length : 0;
        if (typeof d.ms === 'number') row.ms = d.ms;
        row.mutations = Array.isArray(d.mutations) ? d.mutations : [];
        row.truncated = d.truncated === true;
        if (d.fix) row.fix = String(d.fix);
        paint(row);
        save();
        say(fmt(S.sayTool, row.name, d.ok ? S.toolOk : S.toolFailed), !d.ok);
        return;
      }

      case 'tool_pending': {
        const id = String(d.id || '');
        put({
          key: 'ap:' + id,
          kind: 'approval',
          id: id,
          tool: String(d.tool || ''),
          reason: String(d.reason || ''),
          paths: Array.isArray(d.paths) ? d.paths : [],
          protectedPaths: Array.isArray(d.protected) ? d.protected : [],
          unconditional: d.unconditional === true,
        });
        pendingApprovals.add(id);
        updateSkip();
        // Urgent, and the sentence names the skip link: the card is not focused,
        // because a run must never take focus out of the editor.
        say(fmt(S.sayApproval, d.tool), true);
        return;
      }

      /*
       * Synthesised host-side (extension.ts, from ApprovalCentre.onDidResolve),
       * because the wire carries no "resolved" event — whichever surface
       * answered the approval is the only thing that knows.
       *
       * `timeout` and `gone` are folded into one `released` outcome. They differ
       * in why the runtime took the approval back, but not in what happened to
       * the developer's change, and the receipt says what happened.
       */
      case 'approval_resolved': {
        const row = byKey.get('ap:' + String(d.id || ''));
        if (!row) return;
        const raw = String(d.decision || '');
        row.decision = raw === 'timeout' || raw === 'gone' ? 'released' : raw;
        row.receipt = receiptFor(row.decision, String(d.tool || row.tool), d.auto === true);
        paint(row);
        pendingApprovals.delete(row.id);
        updateSkip();
        save();
        return;
      }

      case 'plan': {
        // The prose already arrived as an `assistant`. The plan card is the
        // richer rendering of the same words, so it takes the row rather than
        // adding one underneath it.
        if (foldable && foldable.kind === 'assistant' && sameText(foldable.text, d.text)) {
          removeRow(foldable.key);
        }
        foldable = null;
        put({
          key: keyFor('plan', event.id),
          kind: 'plan',
          text: String(d.text || ''),
          steps: typeof d.steps === 'number' ? d.steps : 0,
        });
        say(S.sayPlan, false);
        return;
      }

      case 'gate': {
        if (d.kind === 'compaction') {
          put({
            key: keyFor('compact', event.id),
            kind: 'compaction',
            before: d.before,
            after: d.after,
            seconds: d.seconds,
          });
          return;
        }
        const key = keyFor('gate', runIndex + ':' + (d.kind || 'full'));
        let row = byKey.get(key);
        if (!row) {
          row = put({ key: key, kind: 'gate', gkind: d.kind || 'full', attempts: [] });
        }
        const column = {
          attempt: attempt,
          ok: d.ok === true,
          seconds: d.seconds,
          stages: Array.isArray(d.stages) ? d.stages : [],
          not_run: Array.isArray(d.not_run) ? d.not_run : [],
          blocked_by: d.blocked_by || '',
        };
        const existing = row.attempts.findIndex(function (a) {
          return a.attempt === column.attempt;
        });
        if (existing === -1) row.attempts.push(column);
        else row.attempts[existing] = column;
        paint(row);
        save();
        say(
          fmt(
            S.sayGate,
            d.kind === 'inner' ? S.gateInner : S.gateFull,
            column.attempt,
            column.ok ? S.gatePassed : S.gateFailed,
          ),
          !column.ok,
        );
        return;
      }

      case 'usage':
        renderMeter(d);
        return;

      case 'quota': {
        // Only figures the server already computed. The status bar owns quota;
        // a card here exists so the transcript records when it moved, and so a
        // run that is about to be refused says so before it is.
        const tightest = d.tightest;
        if (!tightest) return;
        const window = d.window || {};
        put({
          key: keyFor('quota', event.id),
          kind: 'quota',
          text: fmt(S.quota, tightest.name, tightest.used, tightest.cap),
          name: String(tightest.name || ''),
          pct: typeof tightest.pct === 'number' ? tightest.pct : null,
          resets: typeof window.resets_in === 'number' ? window.resets_in : null,
        });
        return;
      }

      case 'steer':
        put({ key: keyFor('steer', event.id), kind: 'steer' });
        return;

      case 'finish': {
        openAssistant = null;
        // A failing run usually states its cause twice: once as `error`, and
        // again as the summary it finishes with. The finish row carries the
        // outcome as well, so it is the one that stays.
        if (foldable && foldable.kind === 'error' && sameText(foldable.text, d.summary)) {
          removeRow(foldable.key);
        }
        foldable = null;
        // The run is over; anything the next message starts belongs to a grid of
        // its own.
        runIndex += 1;
        put({
          key: keyFor('finish', event.id),
          kind: 'finish',
          outcome: String(d.outcome || 'done'),
          summary: String(d.summary || ''),
          turns: d.turns,
          mutations: Array.isArray(d.mutations) ? d.mutations : [],
        });
        const spec = FINISH[String(d.outcome)] || FINISH.done;
        say(fmt(S.sayFinish, S[spec[1]]), true);
        return;
      }

      case 'error': {
        const message = String(d.message || d.error || '');
        const errorKey = keyFor('error', event.id);
        put({ key: errorKey, kind: 'error', message: message });
        foldable = { key: errorKey, kind: 'error', text: message };
        say(fmt(S.sayError, message.split('\n')[0]), true);
        return;
      }

      case 'heartbeat':
      case 'end':
        return;

      default:
        // C2: an event type this build has never heard of is a row we skip, not
        // an error we show. The `.vsix` and the wheel version independently.
        return;
    }
  }

  function argSummary(args) {
    if (!args || typeof args !== 'object') return '';
    const preferred = ['path', 'file', 'pattern', 'command', 'query', 'package'];
    for (let i = 0; i < preferred.length; i += 1) {
      const value = args[preferred[i]];
      if (typeof value === 'string' && value) return value.slice(0, 80);
    }
    try {
      const json = JSON.stringify(args);
      return json.length > 60 ? json.slice(0, 60) + '…' : json;
    } catch (err) {
      return '';
    }
  }

  // ── the meter ─────────────────────────────────────────────────────────────

  function renderMeter(usage) {
    meterEl.textContent = '';

    /*
     * The bar takes the width the sentence does not need, so the meter reads as
     * one object rather than as a strip above a caption. It is `aria-hidden`
     * and carries no value of its own: every figure it depicts is already a
     * word in the segments beside it, which is what a screen reader gets.
     *
     * `budget_used_pct` is the server's figure. Recomputing it from
     * prompt_tokens/budget would disagree with the status bar the moment the
     * gateway starts weighting anything.
     */
    if (typeof usage.budget_used_pct === 'number') {
      const bar = el('div', 'bar');
      bar.setAttribute('aria-hidden', 'true');
      const fill = el('i');
      fill.style.width = Math.max(0, Math.min(100, Math.round(usage.budget_used_pct))) + '%';
      bar.appendChild(fill);
      meterEl.appendChild(bar);
    }

    const segs = el('span', 'segs');
    meterEl.appendChild(segs);
    const add = function (text, cls) {
      segs.appendChild(el('span', cls ? 'seg ' + cls : 'seg', text));
    };

    add(fmt(S.meterContext, compact(usage.prompt_tokens), compact(usage.budget)));
    if (usage.reasoning_tokens) add(fmt(S.meterReasoning, compact(usage.reasoning_tokens)));

    // `cached_tokens` is null until the endpoint reports it. "cache 0%" would
    // read as a cache that is failing; "not reported" is the true statement.
    if (usage.cached_tokens === null || usage.cached_tokens === undefined) {
      add(S.meterCacheUnknown);
    } else if (usage.prompt_tokens > 0) {
      add(fmt(S.meterCache, Math.round((usage.cached_tokens / usage.prompt_tokens) * 100)));
    }

    if (usage.reasoning_leaked) {
      add(fmt(S.meterLeaked, compact(usage.reasoning_leaked)), 'anomaly');
    }
  }

  // ── the skip link ─────────────────────────────────────────────────────────

  /**
   * Rendered only while something is pending, so the first focusable element in
   * the panel is either a live route to the card or nothing at all. A permanent
   * skip link that usually goes nowhere trains people to ignore it.
   */
  function updateSkip() {
    const pending = pendingApprovals.size > 0;
    skipBtn.hidden = !pending;
    skipBtn.textContent = S.skipToApproval || '';
    // The header band and the composer both read "waiting on a decision" off
    // this set, and this is the one place it changes.
    applyConsole();
    applyComposerState();
  }

  skipBtn.addEventListener('click', function () {
    let target = null;
    pendingApprovals.forEach(function (id) {
      const node = nodes.get('ap:' + id);
      if (node && node.isConnected) target = node;
    });
    if (!target) return;
    target.scrollIntoView({ block: 'center' });
    const button = target.querySelector('button');
    if (button) button.focus();
    else target.focus();
  });

  // ── host messages ─────────────────────────────────────────────────────────

  const inbox = [];
  let frame = null;

  window.addEventListener('message', function (event) {
    const message = event.data;
    if (!message || typeof message.type !== 'string') return;
    if (message.type === 'batch' && Array.isArray(message.messages)) {
      for (let i = 0; i < message.messages.length; i += 1) inbox.push(message.messages[i]);
    } else {
      inbox.push(message);
    }
    // Every DOM write in one frame. The host caps its side at 25 messages a
    // second; this caps ours at one layout per frame however they arrive.
    if (frame === null) frame = requestAnimationFrame(drain);
  });

  function drain() {
    frame = null;
    const stick = atBottom();
    while (inbox.length) handle(inbox.shift());
    if (stick) transcript.scrollTop = transcript.scrollHeight;
  }

  function atBottom() {
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 40;
  }

  function handle(message) {
    switch (message.type) {
      case 'init':
        S = message.strings || {};
        SLASH = message.commands || [];
        MENTIONS = message.mentions || [];
        MAX_ROWS = message.maxRows || 500;
        applyStrings();
        repaintAll();
        return;

      case 'event': {
        if (typeof message.seq === 'number') {
          if (message.seq <= lastSeq) return; // already applied; a replay overlap
          lastSeq = message.seq;
        }
        // Set before the event is reduced, because every key the reducer mints
        // is namespaced by it.
        if (typeof message.session === 'string' && message.session) {
          session = message.session;
        }
        applyEvent(message.event || {});
        return;
      }

      case 'user':
        localSeq += 1;
        put({
          key: 'user:host:' + localSeq,
          kind: 'user',
          text: String(message.text || ''),
          steering: message.steering === true,
          // Optimistic until the runtime's own `user` event arrives and
          // replaces it. See `dropEcho`.
          pending: true,
        });
        return;

      case 'run':
        run = message.state || { phase: 'idle' };
        applyRunState();
        return;

      case 'offline':
        offlineReason = message.reason === undefined ? null : String(message.reason || '');
        applyOffline();
        return;

      case 'queued':
        applyQueued(message.count || 0);
        return;

      case 'notice':
        localSeq += 1;
        put({
          key: 'notice:' + localSeq,
          kind: 'notice',
          level: message.level,
          text: String(message.text || ''),
        });
        if (message.level === 'error') say(fmt(S.sayError, message.text), true);
        return;

      case 'mentions':
        applyMentions(message.token, message.items || []);
        return;

      case 'approval-resolved': {
        const row = byKey.get('ap:' + message.id);
        if (row) {
          row.decision = message.decision;
          paint(row);
        }
        pendingApprovals.delete(message.id);
        updateSkip();
        save();
        return;
      }

      /*
       * The panel shows one conversation at a time, and this says which.
       *
       * The clear is conditional and the condition matters. Opening a different
       * session must remove what is on screen — clicking through the Sessions
       * tree used to accumulate every conversation looked at into one
       * transcript. But the *first* message of a new conversation arrives with
       * nothing on screen except its own optimistic echo, and clearing there
       * would delete the sentence the developer had just typed.
       *
       * `session` is empty on a panel that has never shown one, and restored
       * alongside the rows on a panel that has, so it is the honest test for
       * "there is another conversation here".
       */
      case 'session': {
        const next = String(message.id || '');
        if (session && next !== session) clearAll();
        session = next;
        return;
      }

      case 'clear':
        clearAll();
        return;

      case 'focus':
        input.focus();
        return;

      default:
        // Additive here too: a host newer than this asset must degrade quietly.
        return;
    }
  }

  function repaintAll() {
    transcript.textContent = '';
    // The node is gone with the rest of the subtree; drop the handle too, or
    // syncEmpty sees a detached node and decides it has nothing to do.
    emptyNode = null;
    nodes.clear();
    byKey.clear();
    rows.forEach(function (row) {
      byKey.set(row.key, row);
      paint(row);
      if (row.kind === 'approval' && !row.decision) pendingApprovals.add(row.id);
    });
    syncEmpty();
    updateSkip();
    transcript.scrollTop = transcript.scrollHeight;
  }

  // ── composer state ────────────────────────────────────────────────────────

  function applyStrings() {
    transcript.setAttribute('aria-label', S.transcript || '');
    inputLabel.textContent = S.placeholder || '';
    popup.setAttribute('aria-label', S.suggestions || '');
    sendBtn.textContent = S.send || '';
    stopBtn.textContent = S.stop || '';
    stopBtn.title = S.stopHint || '';
    windBtn.textContent = S.windDown || '';
    windBtn.title = S.windDownHint || '';
    applyRunState();
    applyOffline();
    updateSkip();
    // The empty state is drawn at boot, before `init` has delivered a single
    // string — so it comes back blank unless it is rebuilt once they arrive.
    if (emptyNode && emptyNode.isConnected) {
      emptyNode.remove();
      emptyNode = null;
      syncEmpty();
    }
    if (!meterEl.firstChild) meterEl.textContent = S.meterIdle || '';
  }

  let tick = null;

  /**
   * The console row: mode · turn/attempt · blocking gate stage · context.
   *
   * Every cell is conditional on its data having arrived, so a read-only
   * planner run pays two cells and an idle panel pays none. That is what makes
   * one persistent row defensible at a 340px sidebar width where four were not.
   *
   * It answers "where is it, and is it going to pass?" without a scroll — which
   * during a forty-turn gate loop is the only question anyone has.
   */
  function applyConsole() {
    if (run.phase === 'idle') {
      consoleEl.hidden = true;
      consoleEl.textContent = '';
      // Cleared, not just hidden. The band comes back on the next run, and it
      // must not come back still wearing the state the last one ended in.
      consoleEl.classList.remove('running', 'blocked-state');
      return;
    }

    /*
     * Waiting on a decision replaces the mode cell rather than adding to it.
     *
     * "coding" is true but useless while the run is blocked on a person — the
     * question the band answers is "where is it, and is it going to pass?", and
     * the answer right now is "it is waiting for you". The turn and attempt
     * stay, because they are what the reviewer needs to place the request.
     */
    const waiting = pendingApprovals.size > 0;
    const cells = [];
    if (waiting) cells.push({ text: S.consoleNeedsApproval, cls: 'waiting' });
    else if (run.mode) cells.push({ text: S['mode.' + run.mode] || run.mode });
    if (run.turn) {
      // The attempt is shown only when there has been one, because "attempt 1"
      // on every turn is noise that hides the retry it exists to announce.
      const turn = fmt(S.consoleTurn, run.turn);
      cells.push({ text: run.attempt > 1 ? turn + ' · ' + fmt(S.consoleAttempt, run.attempt) : turn });
    }
    if (run.blockedBy) {
      cells.push({ text: fmt(S.consoleBlocked, run.blockedBy), cls: 'blocked' });
    }
    if (run.context) cells.push({ text: run.context });

    consoleEl.textContent = '';
    cells.forEach(function (cell, i) {
      if (i > 0) consoleEl.appendChild(el('span', 'sep', '·'));
      const node = el('span', 'cell ' + (cell.cls || ''));
      node.appendChild(el('b', '', cell.text));
      consoleEl.appendChild(node);
    });
    consoleEl.hidden = cells.length === 0;

    /*
     * The band's own state, in two classes.
     *
     * `running` earns the pulsing amber dot; `blocked-state` tints the whole
     * band, and is the only persistent chrome in the panel that ever changes
     * colour. Both are decoration on top of cells that already say the same
     * thing in words — the class is never the only carrier.
     */
    consoleEl.classList.toggle('running', run.phase !== 'idle' && !waiting);
    // "Waiting on you" is the panel's own knowledge, not the host's: `RunState`
    // has no approval phase, because from the runtime's side a blocked approval
    // is still a running turn.
    consoleEl.classList.toggle('blocked-state', waiting);
  }

  function applyRunState() {
    applyConsole();
    const running = run.phase !== 'idle';
    stopBtn.hidden = !running;
    // Winding down already means "stop after this turn"; offering it again
    // would be a button whose only effect is to say what is already true.
    windBtn.hidden = run.phase !== 'running' || pendingApprovals.size > 0;
    input.placeholder = running ? S.placeholderRunning || '' : S.placeholder || '';
    workingEl.hidden = !running;
    applyComposerState();
    if (tick) {
      clearInterval(tick);
      tick = null;
    }
    if (!running) {
      workingEl.textContent = '';
      return;
    }
    paintWorking();
    // One text write a second, and no animation: `prefers-reduced-motion` has
    // nothing to suppress if the indicator never moves in the first place.
    tick = setInterval(paintWorking, 1000);
  }

  /*
   * One writer for the composer's status line, called both by the ticker and
   * the instant an approval arrives or resolves. If only the ticker wrote it,
   * the line would be up to a second out of date at exactly the moment it
   * changes meaning — which is the moment someone is reading it.
   *
   * While an approval is up, the elapsed time is not the useful fact: the run
   * is not spending it on the model, it is spending it on the reviewer, and
   * counting it up reads as the agent being slow.
   */
  function paintWorking() {
    if (run.phase === 'idle') {
      workingEl.textContent = '';
      return;
    }
    if (pendingApprovals.size > 0) {
      workingEl.textContent = S.waitingDecision || '';
      return;
    }
    const base = run.tool ? fmt(S.workingTool, run.tool) : S.working;
    const seconds = run.startedAt ? Math.round((Date.now() - run.startedAt) / 1000) : null;
    workingEl.textContent = seconds === null ? base : base + ' · ' + fmt(S.elapsed, seconds);
  }

  /**
   * Which of the five composers this is.
   *
   * The design gives the composer a different border and a different footer per
   * state — idle, running, waiting on a decision, offline, and completing a
   * slash command. All five are the same DOM; only the classes and the footer
   * text change, because a composer that is rebuilt loses the caret and
   * whatever half-typed correction was in it.
   */
  function applyComposerState() {
    const off = offlineReason !== null;
    const waiting = pendingApprovals.size > 0;
    const running = run.phase !== 'idle';
    const completing = !popup.hidden;

    composerEl.classList.toggle('offline', off);
    composerEl.classList.toggle('running', !off && running && !waiting);
    composerEl.classList.toggle('waiting', !off && waiting);
    composerEl.classList.toggle('completing', !off && completing);

    // The mode chip is an idle-only affordance: while a run is in flight the
    // console band above already says the mode, and saying it twice in one
    // 340px column is the kind of duplication that pushed the old four-row
    // header out of the design in the first place.
    /*
     * The mode *name*, not the activity. `S['mode.coder']` is "coding", which
     * the console band is right to use while a turn is in flight and which is
     * simply false on an idle panel — nothing is coding. The chip says which
     * mode the next task will start in, so it names it the way
     * `dakcoder.defaultMode` does.
     */
    const showMode = !off && !running && Boolean(run.mode);
    modePill.hidden = !showMode;
    modePill.textContent = showMode ? run.mode : '';

    keysEl.hidden = !completing;
    keysEl.textContent = completing ? S.popupKeys || '' : '';
    paintWorking();
  }

  let wasOffline = false;

  /**
   * The offline card.
   *
   * Two sentences, and the second one is the point. "The stream dropped" and
   * "the run died" have the same symptom — a panel that stopped moving — and
   * only the second is a reason to do anything. Saying that the run continues
   * on the runtime is what stops someone re-running work that is still in
   * flight.
   */
  function applyOffline() {
    const off = offlineReason !== null;
    const what = offlineReason || S.offlineDefault || '';
    offlineEl.hidden = !off;
    offlineEl.textContent = '';

    if (off) {
      const head = el('div', 'head');
      const mark = el('span', 'mark');
      mark.appendChild(icon(GLYPHS.offline, 13, 1.8));
      head.appendChild(mark);
      head.appendChild(el('span', null, S.offlineTitle || ''));
      head.appendChild(el('span', 'spacer'));
      const spin = el('span', 'spinner');
      spin.setAttribute('aria-hidden', 'true');
      head.appendChild(spin);
      offlineEl.appendChild(head);
      offlineEl.appendChild(el('p', 'what', what));
      offlineEl.appendChild(el('p', 'aside', S.offlineAside || ''));
    }

    // Disabled, not left accepting input that is going to fail: the agent cannot
    // reach the model without the gateway, by design, and a queued message that
    // silently dies is worse than a composer that says why it is closed.
    input.disabled = off;
    sendBtn.disabled = off;
    applyComposerState();
    if (off && !wasOffline) say(fmt(S.sayOffline, what), true);
    if (!off && wasOffline) say(S.sayOnline, true);
    wasOffline = off;
  }

  function applyQueued(count) {
    queuedEl.hidden = count <= 0;
    queuedEl.textContent = count > 0 ? plural('queued', count) : '';
    if (count > 0) say(S.sayQueued, false);
  }

  // ── sending, and steering ─────────────────────────────────────────────────

  function send() {
    const text = input.value.trim();
    if (!text || input.disabled) return;

    const slash = /^\/(\w+)\s*([\s\S]*)$/.exec(text);
    const known =
      slash &&
      SLASH.some(function (spec) {
        return spec.name === slash[1];
      });

    localSeq += 1;
    /*
     * Steering is the point of this branch. A message typed during a run queues
     * as a correction the run reads before its next turn; without it the only
     * way to disagree with a run in flight is Stop, which throws away every turn
     * of context it had built. The chip says "queued" immediately and the host's
     * `queued` message confirms the depth.
     */
    const steering = run.phase === 'running';
    put({
      key: 'user:local:' + localSeq,
      kind: 'user',
      text: text,
      steering: steering,
      // Optimistic until the runtime's own `user` event arrives and replaces it.
      // Set whatever the composer believes about the run, because it may be
      // wrong: a run that ended between the last event and this keystroke means
      // the message the panel is calling a correction is the one the runtime
      // will record as the next thing said. A steering message really does get
      // no `user` event — the run records those as `steer` — so its echo simply
      // stays, which is why this is a flag and not a promise.
      pending: true,
    });

    if (known) post({ type: 'slash', command: slash[1], argument: slash[2].trim() });
    else post({ type: 'submit', text: text, steering: steering });

    input.value = '';
    autosize();
    closePopup();
  }

  sendBtn.addEventListener('click', send);
  stopBtn.addEventListener('click', function () {
    post({ type: 'stop' });
  });
  windBtn.addEventListener('click', function () {
    post({ type: 'wind-down' });
  });

  function autosize() {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  }

  // ── slash and mention popup ───────────────────────────────────────────────

  let options = [];
  let active = -1;
  let completionToken = 0;
  let completionTimer = null;
  let currentToken = null;
  /** The static mention entries the in-flight completion request will be merged
   *  into, held so a slow gopls answer cannot drop them. */
  let pendingStatics = [];

  function tokenAtCaret() {
    const caret = input.selectionStart;
    const before = input.value.slice(0, caret);
    const match = /(^|\s)([/@][^\s]*)$/.exec(before);
    if (!match) return null;
    const token = match[2];
    const start = caret - token.length;
    // A slash command is only a command at the start of the message; `/` inside
    // a sentence (or a path) is just a slash.
    if (token.charAt(0) === '/' && start !== 0) return null;
    return { text: token, start: start, end: caret };
  }

  function refresh() {
    const token = tokenAtCaret();
    currentToken = token;
    if (!token) {
      closePopup();
      return;
    }

    if (token.text.charAt(0) === '/') {
      const query = token.text.slice(1).toLowerCase();
      setOptions(
        SLASH.filter(function (spec) {
          return spec.name.indexOf(query) === 0;
        }).map(function (spec) {
          return { insert: '/' + spec.name + ' ', label: '/' + spec.name, hint: spec.hint };
        }),
      );
      return;
    }

    const rest = token.text.slice(1);
    const statics = MENTIONS.filter(function (spec) {
      const bare = spec.trigger.slice(1);
      return bare.indexOf(rest) === 0 || rest.indexOf(bare) === 0;
    }).map(function (spec) {
      return {
        insert: spec.trigger + (spec.kind === 'build' || spec.kind === 'diag' ? ' ' : ''),
        label: spec.trigger,
        hint: spec.reference ? spec.hint + ' — ' + S.hintReference : spec.hint,
      };
    });
    setOptions(statics);

    let kind = 'file';
    let query = rest;
    if (rest.charAt(0) === '#') {
      kind = 'symbol';
      query = rest.slice(1);
    } else if (rest.indexOf('pkg:') === 0) {
      kind = 'package';
      query = rest.slice(4);
    }
    requestCompletions(kind, query, statics);
  }

  function requestCompletions(kind, query, statics) {
    if (completionTimer) clearTimeout(completionTimer);
    completionTimer = setTimeout(function () {
      completionTimer = null;
      completionToken += 1;
      pendingStatics = statics;
      post({ type: 'complete', kind: kind, query: query, token: completionToken });
    }, 120);
  }

  function applyMentions(token, items) {
    // A stale reply for a token the developer has already typed past would
    // replace a correct list with an obsolete one.
    if (token !== completionToken || !currentToken) return;
    setOptions(
      pendingStatics.concat(
        items.map(function (item) {
          return {
            insert: item.insert + ' ',
            label: item.label,
            hint: item.detail || '',
          };
        }),
      ),
    );
  }

  function setOptions(list) {
    options = list;
    popup.textContent = '';
    if (!list.length) {
      closePopup();
      return;
    }
    list.forEach(function (option, i) {
      const li = el('li');
      li.id = 'opt-' + i;
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', 'false');
      li.appendChild(el('span', 'name', option.label));
      li.appendChild(el('span', 'hint', option.hint || ''));
      li.addEventListener('mousedown', function (event) {
        event.preventDefault();
        accept(i);
      });
      popup.appendChild(li);
    });
    popup.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    applyComposerState();
    setActive(0);
  }

  function setActive(i) {
    active = i;
    const items = popup.children;
    for (let n = 0; n < items.length; n += 1) {
      items[n].setAttribute('aria-selected', String(n === i));
    }
    if (items[i]) {
      input.setAttribute('aria-activedescendant', items[i].id);
      items[i].scrollIntoView({ block: 'nearest' });
    }
  }

  function closePopup() {
    popup.hidden = true;
    popup.textContent = '';
    options = [];
    active = -1;
    input.setAttribute('aria-expanded', 'false');
    input.removeAttribute('aria-activedescendant');
    applyComposerState();
  }

  function accept(i) {
    const option = options[i];
    if (!option || !currentToken) return;
    const value = input.value;
    input.value = value.slice(0, currentToken.start) + option.insert + value.slice(currentToken.end);
    const caret = currentToken.start + option.insert.length;
    input.setSelectionRange(caret, caret);
    closePopup();
    autosize();
    input.focus();
  }

  input.addEventListener('input', function () {
    autosize();
    refresh();
  });

  input.addEventListener('keydown', function (event) {
    const open = !popup.hidden && options.length > 0;

    if (event.key === 'ArrowDown' && open) {
      event.preventDefault();
      setActive((active + 1) % options.length);
      return;
    }
    if (event.key === 'ArrowUp' && open) {
      event.preventDefault();
      setActive((active - 1 + options.length) % options.length);
      return;
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && open) {
      event.preventDefault();
      accept(active);
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  });

  /**
   * Escape is non-destructive, everywhere in the panel.
   *
   * It closes the popup if one is open, otherwise it puts focus back in the
   * composer. It never stops a run: a key that both dismisses a menu and kills
   * twenty turns of work is a key nobody can press with confidence.
   */
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!popup.hidden) {
      event.preventDefault();
      closePopup();
      input.focus();
      return;
    }
    if (document.activeElement !== input) {
      event.preventDefault();
      input.focus();
    }
  });

  input.addEventListener('blur', function () {
    // A click on an option fires mousedown first, which is why accept() lives
    // there rather than on click.
    setTimeout(closePopup, 0);
  });

  // ── boot ──────────────────────────────────────────────────────────────────

  meterEl.textContent = '';
  repaintAll();
  // The host answers with `init` and replays only events past this id, the same
  // resumption the SSE client does against the runtime.
  post({ type: 'ready', lastSeq: lastSeq });
})();
