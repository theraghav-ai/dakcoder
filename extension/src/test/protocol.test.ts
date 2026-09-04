/**
 * Unit tests that need no VS Code host.
 *
 * The SSE parser is the one worth the most here. A parser that assumes one chunk
 * is one frame works perfectly against a loopback on the same machine and
 * corrupts the first long tool result over a slow link — so every test below
 * feeds it deliberately hostile chunking. If these only ever saw whole frames
 * they would pass forever and prove nothing.
 */

import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import { parseSse } from '../client';
import { isResumable, normaliseQuota, parsePlan, type WireEvent } from '../protocol';

/** A ReadableStream that hands out exactly the chunks given, in order. */
function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  let i = 0;
  return new ReadableStream({
    pull(controller) {
      if (i >= chunks.length) {
        controller.close();
        return;
      }
      controller.enqueue(encoder.encode(chunks[i++]));
    },
  });
}

async function collect(...chunks: string[]): Promise<WireEvent[]> {
  const out: WireEvent[] = [];
  for await (const event of parseSse(streamOf(...chunks))) out.push(event);
  return out;
}

/** Cut a string into n-byte pieces, to simulate an adversarial network. */
function shred(text: string, size: number): string[] {
  const parts: string[] = [];
  for (let i = 0; i < text.length; i += size) parts.push(text.slice(i, i + size));
  return parts;
}

describe('the SSE parser', () => {
  it('reads a whole frame', async () => {
    const events = await collect('id: 1\nevent: assistant\ndata: {"text":"hello"}\n\n');
    assert.equal(events.length, 1);
    assert.equal(events[0].id, 1);
    assert.equal(events[0].type, 'assistant');
    assert.equal(events[0].data.text, 'hello');
  });

  it('reassembles a frame split across chunks', async () => {
    const events = await collect(
      'id: 7\nevent: too',
      'l_result\ndata: {"id":"c1",',
      '"name":"read_file","ok":true}',
      '\n\n',
    );
    assert.equal(events.length, 1);
    assert.equal(events[0].id, 7);
    assert.equal(events[0].type, 'tool_result');
    assert.equal(events[0].data.name, 'read_file');
  });

  it('survives being shredded one byte at a time', async () => {
    // The pathological case. If the buffer logic is wrong anywhere, this is
    // where it shows.
    const wire =
      'id: 1\nevent: turn_start\ndata: {"turn":1,"mode":"coder"}\n\n' +
      'id: 2\nevent: assistant\ndata: {"text":"line one\\nline two"}\n\n' +
      'id: 3\nevent: end\ndata: {}\n\n';
    const events = await collect(...shred(wire, 1));
    assert.deepEqual(
      events.map((e) => [e.id, e.type]),
      [
        [1, 'turn_start'],
        [2, 'assistant'],
        [3, 'end'],
      ],
    );
    assert.equal(events[1].data.text, 'line one\nline two');
  });

  it('reads several frames out of one chunk', async () => {
    const events = await collect(
      'id: 1\nevent: a\ndata: {}\n\nid: 2\nevent: b\ndata: {}\n\nid: 3\nevent: c\ndata: {}\n\n',
    );
    assert.deepEqual(events.map((e) => e.type), ['a', 'b', 'c']);
  });

  it('ignores keep-alive comments without emitting an event', async () => {
    // EventSource swallows these silently, which is why liveness needs a real
    // parser rather than the browser API.
    const events = await collect(': keep-alive\n\n', 'id: 4\nevent: heartbeat\ndata: {}\n\n');
    assert.equal(events.length, 1);
    assert.equal(events[0].type, 'heartbeat');
  });

  it('skips a malformed frame instead of ending the run', async () => {
    const events = await collect(
      'id: 1\nevent: assistant\ndata: {not json\n\n',
      'id: 2\nevent: assistant\ndata: {"text":"still here"}\n\n',
    );
    assert.equal(events.length, 2);
    assert.equal(events[0].data.message, 'a malformed event was skipped');
    assert.equal(events[1].data.text, 'still here', 'one bad frame must not kill the stream');
  });

  it('carries the id forward so a reconnect never replays what it has seen', async () => {
    const events = await collect('id: 42\nevent: a\ndata: {}\n\n', 'event: b\ndata: {}\n\n');
    assert.equal(events[0].id, 42);
    assert.equal(events[1].id, 42, 'a frame without an id inherits the last one');
  });

  it('handles CRLF line endings', async () => {
    const events = await collect('id: 5\r\nevent: gate\r\ndata: {"ok":true}\r\n\r\n');
    assert.equal(events.length, 1);
    assert.equal(events[0].type, 'gate');
    assert.equal(events[0].data.ok, true);
  });

  it('handles CRLF shredded so the CR and LF land in different chunks', async () => {
    // The reason a lone trailing CR is held back rather than normalised per
    // chunk: split here, a naive normaliser leaves a stray CR inside the frame.
    const CRLF = '\r\n';
    const wire = `id: 6${CRLF}event: usage${CRLF}data: {"budget":32768}${CRLF}${CRLF}`;
    const events = await collect(...shred(wire, 1));
    assert.equal(events.length, 1);
    assert.equal(events[0].type, 'usage');
    assert.equal(events[0].data.budget, 32768);
  });

  it('joins multi-line data fields', async () => {
    const events = await collect('event: x\ndata: {"a":1,\ndata: "b":2}\n\n');
    assert.deepEqual(events[0].data, { a: 1, b: 2 });
  });

  it('stops cleanly when the consumer aborts', async () => {
    const controller = new AbortController();
    const seen: WireEvent[] = [];
    const stream = streamOf('id: 1\nevent: a\ndata: {}\n\n', 'id: 2\nevent: b\ndata: {}\n\n');
    for await (const event of parseSse(stream, controller.signal)) {
      seen.push(event);
      controller.abort();
    }
    assert.equal(seen.length, 1, 'aborting mid-iteration must not throw');
  });
});

describe('parsePlan', () => {
  const PLAN = [
    'Add a pension resource with list filters.',
    '',
    '1. Scaffold the resource from the spec.',
    '   Accepts: seven files written and go build passes',
    '2. Wire PensionHandler into bootstrap/bootstrapper.go.',
    '   Accepts: fx registration compiles',
    '3. Add the list filters to repo/postgres/pension.go.',
    '   Accepts: rules_lint reports no layer-sql-boundary violation',
  ].join('\n');

  it('finds the goal, the steps and their acceptance criteria', () => {
    const plan = parsePlan(PLAN);
    assert.equal(plan.goal, 'Add a pension resource with list filters.');
    assert.equal(plan.steps.length, 3);
    assert.equal(plan.steps[1].text, 'Wire PensionHandler into bootstrap/bootstrapper.go.');
    assert.equal(plan.steps[2].accepts, 'rules_lint reports no layer-sql-boundary violation');
  });

  it('leaves a step unknown when the runtime did not mark it', () => {
    // Still the honest rendering for a plan from a runtime that predates
    // per-step status. Tying "the gate passed" to "the step advanced" would be
    // a fabrication, and a fabricated status is worse than a visible dash.
    for (const step of parsePlan(PLAN).steps) {
      assert.equal(step.status, 'unknown');
    }
  });

  const MARKED = [
    'Add a pension resource with list filters.',
    '',
    '1. [ done   ] handler/pension.go - scaffold the resource.',
    '2. [ FAILED ] bootstrap/bootstrapper.go - wire the handler.',
    '      go_build rejected this file',
    '3. [ skipped] repo/postgres/pension.go - add the list filters.',
    '      the repository already filters by status',
    '4. [ todo   ] handler/request.go - add the filter DTO.',
    '      Accepts: go build passes',
  ].join('\n');

  it('reads the status the runtime marked on each step', () => {
    // The runtime maintains this from the workspace and the gate, so the panel
    // and the model cannot disagree about what has been done. Before it existed
    // the panel showed a dash for every step and a footnote explaining why.
    const steps = parsePlan(MARKED).steps;
    assert.deepEqual(
      steps.map((s) => s.status),
      ['passed', 'failed', 'skipped', 'pending'],
    );
  });

  it('strips the status mark out of the step text', () => {
    // The list has a status column of its own. Leaving `[ done   ]` in the body
    // would state the same fact twice, in the wrong place.
    const steps = parsePlan(MARKED).steps;
    assert.equal(steps[0].text, 'handler/pension.go - scaffold the resource.');
    assert.ok(!steps[1].text.includes('['), steps[1].text);
  });

  it('still finds the paths and the acceptance criteria around the marks', () => {
    const plan = parsePlan(MARKED);
    assert.ok(plan.scope.includes('bootstrap/bootstrapper.go'));
    assert.equal(plan.steps[3].accepts, 'go build passes');
  });

  it('collects the file paths it can see, for the scope line', () => {
    const scope = parsePlan(PLAN).scope;
    assert.ok(scope.includes('bootstrap/bootstrapper.go'));
    assert.ok(scope.includes('repo/postgres/pension.go'));
  });

  it('does not invent steps out of prose', () => {
    assert.equal(parsePlan('I will look at the handler and then decide.').steps.length, 0);
  });

  it('accepts both "1." and "1)" numbering', () => {
    assert.equal(parsePlan('1) first\n2) second').steps.length, 2);
  });
});

describe('isResumable', () => {
  it('offers resume for the five outcomes the runtime accepts', () => {
    for (const status of ['unverified', 'no_progress', 'exhausted', 'error', 'aborted']) {
      assert.equal(isResumable(status), true, status);
    }
  });

  it('refuses resume for a finished run', () => {
    // `done` takes a follow-up instead. Resuming a successful run would re-enter
    // the gate loop on a change that already passed.
    assert.equal(isResumable('done'), false);
    assert.equal(isResumable('running'), false);
  });

  it('treats an unknown status as not resumable', () => {
    // C2 is additive: a status this build has never heard of must not produce a
    // button that posts to a route the runtime will refuse.
    assert.equal(isResumable('quarantined'), false);
  });
});


describe('normaliseQuota', () => {
  /** What the gateway's `Snapshot.as_dict()` sent before it grew the nested view. */
  const flat = {
    sub: 'gitlab:7',
    lane: 'interactive',
    window_open: true,
    window_expires_at: '2026-08-26T14:00:00+00:00',
    used: { window_tokens: 1200, window_runs: 2, hour_tokens: 1200, week_tokens: 1200, week_sessions: 1 },
    limits: { window_tokens: 10_000, window_runs: 3, hour_tokens: 5_000, week_tokens: 30_000, week_sessions: 2 },
    tightest: { limit: 'window_runs', used_pct: 66.7 },
  };
  const now = Date.parse('2026-08-26T13:30:00+00:00');

  it('builds the nested view from the flat counters', () => {
    const q = normaliseQuota(flat, now);
    assert.deepEqual(q.window, { used: 1200, cap: 10_000, expires_in: 1800, runs: { used: 2, cap: 3 } });
    assert.deepEqual(q.week, { used: 1200, cap: 30_000, sessions: { used: 1, cap: 2 } });
    assert.deepEqual(q.hour, { used: 1200, cap: 5_000 });
    // `name` and `pct` are what the status bar and tooltip read; both must be set.
    assert.deepEqual(q.tightest, { name: 'window_runs', used: 2, cap: 3, pct: 66.7 });
  });

  it('reports no window when none is open, and no tightest limit when nothing is used', () => {
    const q = normaliseQuota(
      { ...flat, window_open: false, window_expires_at: null, used: {}, tightest: { limit: '', used_pct: 0 } },
      now,
    );
    assert.equal(q.window, undefined);
    assert.equal(q.tightest, undefined);
    assert.deepEqual(q.week, { used: 0, cap: 30_000, sessions: { used: 0, cap: 2 } });
  });

  it('prefers the nested view when the gateway sends both', () => {
    const q = normaliseQuota(
      {
        ...flat,
        window: {
          used: 1300,
          cap: 10_000,
          expires_in: 42,
          opened_at: '2026-08-26T09:00:00+00:00',
          runs: { used: 2, cap: 3 },
        },
        tightest: { limit: 'window_runs', used_pct: 66.7, name: 'window_runs', used: 2, cap: 3, pct: 66.7 },
        role: 'developer',
      },
      now,
    );
    assert.deepEqual(q.window, {
      used: 1300,
      cap: 10_000,
      expires_in: 42,
      opened_at: '2026-08-26T09:00:00+00:00',
      runs: { used: 2, cap: 3 },
    });
    assert.equal(q.role, 'developer');
    assert.equal(q.tightest?.name, 'window_runs');
  });

  it('never yields a tightest limit without a name, whatever arrives', () => {
    assert.equal(normaliseQuota({ tightest: { used_pct: 12 } }).tightest, undefined);
    assert.equal(normaliseQuota({ tightest: 'window_runs' }).tightest, undefined);
    assert.deepEqual(normaliseQuota(null), {});
    assert.deepEqual(normaliseQuota('nonsense'), {});
  });
});
