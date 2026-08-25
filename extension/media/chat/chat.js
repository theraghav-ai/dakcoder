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

  /** Strings arrive from the host; `vscode.l10n` does not exist in here. */
  let S = {};
  let SLASH = [];
  let MENTIONS = [];
  let MAX_ROWS = 500;

  const restored = vs.getState() || {};
  /** Ordered row descriptors. The render is a pure function of these. */
  let rows = Array.isArray(restored.rows) ? restored.rows : [];
  let lastEventId = typeof restored.lastEventId === 'number' ? restored.lastEventId : 0;

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
  let localSeq = 0;
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
      vs.setState({ rows: rows.slice(-MAX_ROWS), lastEventId: lastEventId });
    }, 400);
  }

  // ── the row table ─────────────────────────────────────────────────────────

  function put(row) {
    if (!byKey.has(row.key)) {
      rows.push(row);
      byKey.set(row.key, row);
    }
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

  function clearAll() {
    rows = [];
    lastEventId = 0;
    byKey.clear();
    nodes.clear();
    pendingApprovals.clear();
    openAssistant = null;
    transcript.textContent = '';
    updateSkip();
    save();
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
        return shell({ state: '' }, '◷', row.text, '');
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
        body.appendChild(
          pathList(
            row.mutations.map(function (m) {
              return m.path;
            }),
            row.mutations
              .filter(function (m) {
                return m.protected;
              })
              .map(function (m) {
                return m.path;
              }),
          ),
        );
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

    const tbody = el('tbody');
    names.forEach(function (name) {
      const tr = el('tr');
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

    const last = row.attempts[row.attempts.length - 1];
    if (last) {
      wrap.appendChild(
        el('p', 'footnote', last.ok ? fmt(S.gateConverged, last.attempt) : S.gateOpen),
      );
      if (!last.ok && last.blocked_by) {
        wrap.appendChild(el('p', 'footnote', fmt(S.gateBlocked, last.blocked_by)));
      }
      // Failure output belongs behind a disclosure, one per failing stage, so a
      // grid stays a grid rather than becoming a wall of compiler errors.
      (last.stages || []).forEach(function (stage) {
        if (stage.ok || !stage.content) return;
        wrap.appendChild(
          shell(
            { state: 'fail' },
            '✗',
            stage.name,
            fmt(S.gateSeconds, stage.seconds),
            bodyFor(stage.content, 'go'),
          ),
        );
      });
    }
    return wrap;
  }

  function cellFor(a, name) {
    let stage = null;
    (a.stages || []).forEach(function (s) {
      if (s.name === name) stage = s;
    });
    if (!stage) {
      const missing = (a.not_run || []).indexOf(name) !== -1;
      return el('td', missing ? 'skip' : 'absent', missing ? S.gateNotRun : S.gateAbsent);
    }
    if (stage.skipped) return el('td', 'skip', fmt(S.gateSkipped, stage.skipped));
    const word = stage.ok ? S.gatePassed : S.gateFailed;
    return el(
      'td',
      stage.ok ? 'pass' : 'failcell',
      word + ' ' + fmt(S.gateSeconds, stage.seconds),
    );
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

    const title = el('h2', null, fmt(S.approvalTitle, row.tool));
    card.setAttribute('aria-labelledby', nextId('ap'));
    title.id = card.getAttribute('aria-labelledby');
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

    if (row.decision) {
      const word =
        row.decision === 'accept'
          ? S.decidedAccept
          : row.decision === 'reject'
            ? S.decidedReject
            : S.decidedEdit;
      card.appendChild(el('p', 'decided', word));
      return card;
    }

    const buttons = el('div', 'buttons');
    buttons.appendChild(decisionButton(row.id, 'accept', S.accept, 'primary'));
    buttons.appendChild(decisionButton(row.id, 'reject', S.reject, 'secondary'));

    const diff = el('button', 'secondary', S.showDiff);
    diff.type = 'button';
    diff.addEventListener('click', function () {
      post({ type: 'show-diff', id: row.id });
    });
    buttons.appendChild(diff);
    buttons.appendChild(decisionButton(row.id, 'edit', S.editArgs, 'secondary'));
    card.appendChild(buttons);
    return card;
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
    if (typeof event.id === 'number' && event.id > lastEventId) lastEventId = event.id;
    const d = event.data || {};

    switch (event.type) {
      case 'turn_start': {
        attempt = typeof d.attempt === 'number' ? d.attempt : 1;
        openAssistant = null;
        put({
          key: 'turn:' + event.id,
          kind: 'turn',
          turn: d.turn,
          mode: d.mode,
          attempt: attempt,
        });
        return;
      }

      case 'assistant_delta': {
        if (!openAssistant) {
          openAssistant = put({ key: 'assistant:' + event.id, kind: 'assistant', text: '' });
        }
        openAssistant.text += String(d.text || '');
        paint(openAssistant);
        return;
      }

      case 'assistant': {
        // The authoritative text replaces whatever the deltas folded together.
        const row = openAssistant || put({ key: 'assistant:' + event.id, kind: 'assistant', text: '' });
        row.text = String(d.text || '');
        paint(row);
        openAssistant = null;
        save();
        return;
      }

      case 'tool_call': {
        openAssistant = null;
        put({
          key: 'tool:' + d.id,
          kind: 'tool',
          id: d.id,
          name: String(d.name || ''),
          summary: argSummary(d.arguments),
          state: 'running',
        });
        return;
      }

      case 'tool_result': {
        const row = byKey.get('tool:' + d.id) || put({
          key: 'tool:' + d.id,
          kind: 'tool',
          id: d.id,
          name: String(d.name || ''),
          state: 'running',
        });
        const content = String(d.content || '');
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

      case 'plan': {
        put({
          key: 'plan:' + event.id,
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
            key: 'compact:' + event.id,
            kind: 'compaction',
            before: d.before,
            after: d.after,
            seconds: d.seconds,
          });
          return;
        }
        const key = 'gate:' + (d.kind || 'full');
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
        // Only the one line the server already computed. The status bar owns
        // quota; a row here exists so the transcript records when it moved.
        const tightest = d.tightest;
        if (!tightest) return;
        put({
          key: 'quota:' + event.id,
          kind: 'quota',
          text: fmt(S.quota, tightest.name, tightest.used, tightest.cap),
        });
        return;
      }

      case 'steer':
        put({ key: 'steer:' + event.id, kind: 'steer' });
        return;

      case 'finish': {
        openAssistant = null;
        put({
          key: 'finish:' + event.id,
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
        put({ key: 'error:' + event.id, kind: 'error', message: message });
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
    const add = function (text, cls) {
      meterEl.appendChild(el('span', cls ? 'seg ' + cls : 'seg', text));
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

      case 'event':
        applyEvent(message.event || {});
        return;

      case 'user':
        localSeq += 1;
        put({
          key: 'user:host:' + localSeq,
          kind: 'user',
          text: String(message.text || ''),
          steering: message.steering === true,
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
    nodes.clear();
    byKey.clear();
    rows.forEach(function (row) {
      byKey.set(row.key, row);
      paint(row);
      if (row.kind === 'approval' && !row.decision) pendingApprovals.add(row.id);
    });
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
    if (!meterEl.firstChild) meterEl.textContent = S.meterIdle || '';
  }

  let tick = null;

  function applyRunState() {
    const running = run.phase !== 'idle';
    stopBtn.hidden = !running;
    windBtn.hidden = run.phase !== 'running';
    input.placeholder = running ? S.placeholderRunning || '' : S.placeholder || '';
    workingEl.hidden = !running;
    if (tick) {
      clearInterval(tick);
      tick = null;
    }
    if (!running) {
      workingEl.textContent = '';
      return;
    }
    const showWorking = function () {
      const base = run.tool ? fmt(S.workingTool, run.tool) : S.working;
      const seconds = run.startedAt ? Math.round((Date.now() - run.startedAt) / 1000) : null;
      workingEl.textContent = seconds === null ? base : base + ' · ' + fmt(S.elapsed, seconds);
    };
    showWorking();
    // One text write a second, and no animation: `prefers-reduced-motion` has
    // nothing to suppress if the indicator never moves in the first place.
    tick = setInterval(showWorking, 1000);
  }

  let wasOffline = false;

  function applyOffline() {
    const off = offlineReason !== null;
    offlineEl.hidden = !off;
    offlineEl.textContent = off ? offlineReason || S.offlineDefault : '';
    // Disabled, not left accepting input that is going to fail: the agent cannot
    // reach the model without the gateway, by design, and a queued message that
    // silently dies is worse than a composer that says why it is closed.
    input.disabled = off;
    sendBtn.disabled = off;
    if (off && !wasOffline) say(fmt(S.sayOffline, offlineEl.textContent), true);
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
    put({ key: 'user:local:' + localSeq, kind: 'user', text: text, steering: steering });

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
  post({ type: 'ready', lastEventId: lastEventId });
})();
