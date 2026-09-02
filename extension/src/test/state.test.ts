/**
 * Tests for the run state machine, without a VS Code host.
 *
 * `RunState` is the single derivation of every number a surface shows, so an
 * error here shows up everywhere at once — which is also why it is worth testing
 * hard. The de-duplication rules matter most: the server emits `assistant` and
 * then `plan` from the same text on the planner's final turn, and `error`
 * followed by `finish` carrying the identical string. Rendered naively, the plan
 * prints twice and every failure states itself twice.
 */

import { strict as assert } from 'node:assert';
import { before, describe, it } from 'node:test';
import * as path from 'node:path';
import Module from 'node:module';

import type { WireEvent } from '../protocol';

// ── a minimal `vscode` stand-in ─────────────────────────────────────────────
//
// RunState imports `vscode` for EventEmitter and Disposable only. Rather than
// boot an Electron host to exercise a pure state machine, the module is
// resolved to a shim. If RunState ever reaches for a real editor API, this
// throws by absence rather than passing on a stub that quietly lies.

class Emitter<T> {
  private handlers: ((value: T) => void)[] = [];
  readonly event = (handler: (value: T) => void) => {
    this.handlers.push(handler);
    return { dispose: () => (this.handlers = this.handlers.filter((h) => h !== handler)) };
  };
  fire(value: T): void {
    for (const handler of [...this.handlers]) handler(value);
  }
  dispose(): void {
    this.handlers = [];
  }
}

const vscodeShim = {
  EventEmitter: Emitter,
  Disposable: { from: (...items: { dispose(): void }[]) => ({ dispose: () => items.forEach((i) => i.dispose()) }) },
  l10n: { t: (template: string, ...args: unknown[]) => template.replace(/\{(\d+)\}/g, (_m, i) => String(args[Number(i)])) },
  window: { createOutputChannel: () => ({ info() {}, warn() {}, error() {}, debug() {}, trace() {}, show() {} }) },
  workspace: { getConfiguration: () => ({ get: () => undefined }) },
  ThemeIcon: class { constructor(public id: string) {} },
  ThemeColor: class { constructor(public id: string) {} },
};

const originalResolve = (Module as unknown as { _resolveFilename: Function })._resolveFilename;
(Module as unknown as { _resolveFilename: Function })._resolveFilename = function (
  request: string,
  ...rest: unknown[]
) {
  if (request === 'vscode') return path.join(__dirname, '__vscode__.js');
  return originalResolve.call(this, request, ...rest);
};
require.cache[path.join(__dirname, '__vscode__.js')] = {
  id: '__vscode__',
  filename: path.join(__dirname, '__vscode__.js'),
  loaded: true,
  exports: vscodeShim,
} as unknown as NodeModule;

// eslint-disable-next-line @typescript-eslint/no-var-requires
const { RunState } = require('../session-state') as typeof import('../session-state');

// ── a scripted client ───────────────────────────────────────────────────────

function event(id: number, type: string, data: Record<string, unknown> = {}): WireEvent {
  return { id, type, data };
}

class ScriptedClient {
  constructor(private script: WireEvent[]) {}
  connections = 0;
  lastSinceId = -1;

  async *events(_sessionId: string, sinceId: number): AsyncGenerator<WireEvent> {
    this.connections += 1;
    this.lastSinceId = sinceId;
    for (const e of this.script) {
      if (e.id > sinceId) yield e;
    }
  }
  async session(): Promise<never> {
    throw new Error('not used');
  }
}

function stateWith(script: WireEvent[]) {
  const client = new ScriptedClient(script);
  const state = new RunState({ client: client as never });
  return { state, client };
}

/** Drive the ingest path directly; `attach` is async and this is a state test. */
function feed(state: InstanceType<typeof RunState>, events: WireEvent[]): void {
  const ingest = (state as unknown as { ingest(e: WireEvent): void }).ingest.bind(state);
  for (const e of events) ingest(e);
}

// ── tests ───────────────────────────────────────────────────────────────────

describe('RunState — the picture it builds', () => {
  it('tracks mode, turn and attempt from turn_start', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'planner' }),
      event(2, 'turn_start', { turn: 7, mode: 'coder', attempt: 2 }),
    ]);
    assert.equal(state.turn, 7);
    assert.equal(state.modeId, 'coder');
    assert.equal(state.attempt, 2, 'the retry after a failed gate is what a developer watches');
  });

  it('resolves a tool row in place rather than appending a second one', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'tool_call', { id: 'c1', name: 'read_file', arguments: { path: 'handler/pension.go' } }),
      event(2, 'tool_result', { id: 'c1', name: 'read_file', ok: true, content: '…', mutations: [], ms: 42 }),
    ]);
    const tools = state.transcript.filter((e) => e.kind === 'tool');
    assert.equal(tools.length, 1, 'a call and its result are one row, not two');
  });

  it('records every touched path with its protected flag', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'tool_result', {
        id: 'c1',
        name: 'patch_file',
        ok: true,
        content: 'ok',
        mutations: [
          { path: 'handler/pension.go', kind: 'modify', protected: false },
          { path: 'configs/app.yaml', kind: 'modify', protected: true },
        ],
      }),
    ]);
    const paths = state.mutations.map((m) => m.path);
    assert.deepEqual(paths.sort(), ['configs/app.yaml', 'handler/pension.go']);
  });

  it('reports the context meter from the server budget, never by dividing', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'usage', {
        prompt_tokens: 18200,
        completion_tokens: 400,
        cached_tokens: null,
        budget: 32768,
        budget_used_pct: 55.5,
        reasoning_tokens: 1400,
      }),
    ]);
    assert.equal(state.usage?.budget, 32768);
    const meter = state.contextMeter();
    assert.ok(meter?.includes('32'), `expected the absolute budget in "${meter}"`);
  });

  it('never renders cache 0% when the server did not report it', () => {
    // `cache 0%` reads as a failure. The absence of a number is an unknown, and
    // saying so is the only honest rendering.
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'usage', {
        prompt_tokens: 100,
        completion_tokens: 10,
        cached_tokens: null,
        budget: 32768,
        budget_used_pct: 1,
        reasoning_tokens: 0,
      }),
    ]);
    const label = state.cacheLabel();
    assert.ok(!/\b0\s*%/.test(label), `cacheLabel() must not read as 0%, got "${label}"`);
  });
});

describe('RunState — the de-duplication rules', () => {
  it('does not print the plan twice', () => {
    // The planner's final turn emits `assistant` and then `plan` from the same
    // `result.chat.content`. Rendered naively that is the same text twice.
    const text = '1. Scaffold the resource.\n2. Wire it into FX.';
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'planner' }),
      event(2, 'assistant', { text }),
      event(3, 'plan', { text, steps: 2 }),
    ]);
    const assistants = state.transcript.filter((e) => e.kind === 'assistant');
    assert.equal(assistants.length, 0, 'the assistant row is superseded by the plan block');
    assert.equal(state.plan?.steps.length, 2);
  });

  it('keeps an assistant message that is not the plan', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'planner' }),
      event(2, 'assistant', { text: 'Looking at the handler first.' }),
      event(3, 'plan', { text: '1. Scaffold.', steps: 1 }),
    ]);
    assert.equal(state.transcript.filter((e) => e.kind === 'assistant').length, 1);
  });

  it('does not state the same failure twice', () => {
    // The unverified exit emits `error {message}` then `finish {summary}` with
    // the identical string.
    const summary = 'the gate is still failing at go_build after 2 attempts';
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'error', { message: summary }),
      event(2, 'finish', { outcome: 'unverified', summary, turns: 9, mutations: [] }),
    ]);
    const errors = state.transcript.filter((e) => e.kind === 'error');
    assert.equal(errors.length, 0, 'the error is superseded by the finish row that repeats it');
    assert.equal(state.status, 'unverified');
  });

  it('keeps an error that the finish does not repeat', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'error', { message: 'the gateway refused the connection' }),
      event(2, 'finish', { outcome: 'error', summary: 'something else entirely', turns: 1, mutations: [] }),
    ]);
    assert.equal(state.transcript.filter((e) => e.kind === 'error').length, 1);
  });
});

describe('RunState — the additive-only contract', () => {
  it('ignores an event type this build has never heard of', async () => {
    // C2's guarantee is what lets the .vsix and the wheel version separately.
    // An unknown type is a row we skip, not an error we show.
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'coder' }),
      event(2, 'telemetry_sample', { anything: true }),
      event(3, 'assistant', { text: 'still working' }),
    ]);
    // An assistant row is held briefly so a `plan` twin can supersede it. The
    // twin never comes here, so the 400 ms backstop is what commits the row —
    // and waiting for it is the point: a run whose last word never appeared
    // because no further event arrived would be the real bug.
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal(state.turn, 1);
    assert.equal(state.transcript.filter((e) => e.kind === 'error').length, 0);
    assert.equal(state.transcript.filter((e) => e.kind === 'assistant').length, 1);
  });

  it('ignores unknown fields on a type it does know', () => {
    const { state } = stateWith([]);
    feed(state, [event(1, 'turn_start', { turn: 3, mode: 'coder', experiment: 'x', nested: { a: 1 } })]);
    assert.equal(state.turn, 3);
  });
});

describe('RunState — the transcript cap', () => {
  it('evicts the oldest rows past 500 rather than growing forever', () => {
    const { state } = stateWith([]);
    const events: WireEvent[] = [];
    for (let i = 1; i <= 700; i += 1) {
      events.push(event(i, 'assistant', { text: `message ${i}` }));
    }
    feed(state, events);
    assert.ok(state.transcript.length <= 500, `expected ≤500 rows, got ${state.transcript.length}`);
  });
});

describe('RunState — status', () => {
  before(() => {});

  it('marks the five resumable outcomes resumable and done not', () => {
    for (const [outcome, expected] of [
      ['unverified', true],
      ['exhausted', true],
      ['aborted', true],
      ['done', false],
    ] as const) {
      const { state } = stateWith([]);
      feed(state, [event(1, 'finish', { outcome, summary: 's', turns: 1, mutations: [] })]);
      assert.equal(state.resumable, expected, outcome);
    }
  });
});

describe('RunState — the conversation', () => {
  it('keeps what the developer said, not just what the agent replied', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'user', { text: 'add a Pension resource' }),
      event(2, 'turn_start', { turn: 1, mode: 'planner' }),
      event(3, 'assistant', { text: 'here is the plan' }),
      event(4, 'finish', { outcome: 'done', summary: 'done', turns: 1, mutations: [] }),
      event(5, 'user', { text: 'and now the handler' }),
    ]);
    const said = state.transcript.filter((e) => e.kind === 'user').map((e) => (e as { text: string }).text);
    assert.deepEqual(said, ['add a Pension resource', 'and now the handler']);
  });

  it('clears finished_at when a follow-up puts the session back to running', () => {
    const { state } = stateWith([]);
    const base = {
      id: 's1',
      task: 't',
      workspace: 'w',
      created_at: new Date().toISOString(),
      summary: '',
      mutations: [],
      events: 0,
      resumable: false,
      queued: 0,
      winding_down: false,
    };
    state.hydrate({ ...base, status: 'done', finished_at: new Date().toISOString() } as never);
    const stopped = state.elapsedMs;
    state.hydrate({ ...base, status: 'running', finished_at: null } as never);
    assert.equal(state.status, 'running');
    assert.ok(
      state.elapsedMs >= stopped,
      'a frozen elapsed clock reads as a run that has stalled',
    );
  });
});

describe('RunState — transient events', () => {
  /*
   * The server does not persist `assistant_delta` or `heartbeat`, so it does not
   * spend an id on them either: it stamps them with the id the *next* stored
   * event will get. Running them through the monotonic guard therefore drops
   * every delta after the first, then drops the authoritative `assistant`
   * message that shares their id — and leaves `lastId` pointing past an answer
   * that was never delivered, so the next reconnect skips it too.
   */
  it('does not let a delta consume the id of the message it precedes', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(4, 'assistant_delta', { text: 'pack' }),
      event(4, 'assistant_delta', { text: 'age handler' }),
      event(4, 'assistant', { text: 'package handler' }),
      // An `assistant` is held for one event, in case a `plan` repeats it. The
      // finish releases it; the hold is not what this test is about.
      event(5, 'finish', { outcome: 'done', summary: 'shipped', turns: 1, mutations: [] }),
    ]);
    const said = state.transcript.filter((e) => e.kind === 'assistant');
    assert.equal(said.length, 1, 'the assistant message was swallowed by its own deltas');
    assert.equal((said[0] as { text: string }).text, 'package handler');
  });

  it('does not advance the resume cursor past an unpersisted event', async () => {
    const script = [event(1, 'assistant', { text: 'first' })];
    const { state, client } = stateWith(script);
    feed(state, [event(2, 'heartbeat'), event(2, 'assistant_delta', { text: 'x' })]);
    state.attach('s1');
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(client.lastSinceId, 0, 'a heartbeat is not a place to resume from');
    state.dispose();
  });
});


describe('RunState — the numbers a surface shows', () => {
  function summary(over: Record<string, unknown> = {}) {
    return {
      id: 's1',
      task: 'migrate the pension service',
      workspace: '/w',
      status: 'running',
      created_at: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
      finished_at: null,
      summary: '',
      mutations: [],
      events: 0,
      resumable: true,
      queued: 0,
      winding_down: false,
      ...over,
    } as never;
  }

  /*
   * BUG EXT-11. `_startedAt` was set once, with `??=`, and never moved. The
   * second run of a conversation therefore reported the age of the
   * *conversation*: a follow-up sent a moment ago on a session opened three
   * hours earlier drew "Elapsed 3h" in the status bar, and `extension.ts`
   * derives the panel's clock from the same number.
   */
  it('measures the current run, not the age of the session', () => {
    const { state } = stateWith([]);
    const threeHoursAgo = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();
    state.hydrate(summary({ created_at: threeHoursAgo }));
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'planner' }),
      event(2, 'finish', { outcome: 'done', summary: 'first run', turns: 1, mutations: [] }),
    ]);
    const firstRun = state.elapsedMs;
    assert.ok(firstRun >= 3 * 60 * 60 * 1000, 'the first run really did span the session');

    // The follow-up. The wire says running again, with no finish time.
    state.hydrate(summary({ created_at: threeHoursAgo, finished_at: null }));
    assert.ok(
      state.elapsedMs < 60_000,
      `a follow-up starts its own clock; got ${state.elapsedMs} ms`,
    );
    assert.equal(state.status, 'running', 'a session that is running again is not still finished');
  });

  it('starts a fresh clock for a turn that arrives after a finish', () => {
    // The same run-boundary, reached without a hydrate: a second window, or a
    // slash command, sent the follow-up and this panel only sees the turn.
    const { state } = stateWith([]);
    state.hydrate(summary());
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'coder' }),
      event(2, 'finish', { outcome: 'done', summary: 'done', turns: 1, mutations: [] }),
    ]);
    const frozen = state.elapsedMs;
    feed(state, [event(3, 'turn_start', { turn: 2, mode: 'coder' })]);
    assert.notEqual(state.elapsedMs, frozen, 'the clock was still frozen at the last run');
    assert.ok(state.elapsedMs < 60_000, 'and it did not resume from the session start');
  });

  /*
   * BUG EXT-12. The status bar divided `cached_tokens` by `budget` — the size
   * of the context window — while the webview divided it by `prompt_tokens`.
   * Same event, two percentages, and the one the status bar showed was wrong
   * whenever the prompt was smaller than the window, which is always.
   */
  it('states one cache percentage, over the prompt it was sent', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'usage', {
        prompt_tokens: 10_000,
        completion_tokens: 500,
        cached_tokens: 8_000,
        budget: 128_000,
        budget_used_pct: 7.8,
        reasoning_tokens: 0,
      }),
    ]);
    assert.equal(state.cacheLabel(), '80%', 'cache hit rate is of the prompt, not of the window');
    assert.ok(
      state.contextMeter()?.includes('cache 80%'),
      `the meter must agree with the label; got ${String(state.contextMeter())}`,
    );
  });

  it('says nothing rather than 0% when the endpoint did not report a cache', () => {
    const { state } = stateWith([]);
    feed(state, [
      event(1, 'usage', {
        prompt_tokens: 10_000,
        completion_tokens: 500,
        cached_tokens: null,
        budget: 128_000,
        budget_used_pct: 7.8,
        reasoning_tokens: 0,
      }),
    ]);
    assert.equal(state.cacheLabel(), 'not reported');
    assert.equal(state.contextMeter()?.includes('cache'), false);
  });
});

describe('RunState — what reaches a raw consumer', () => {
  /*
   * BUG EXT-13. The re-emission sat above the monotonic guard, so a server that
   * resumed inclusively handed every `onDidReceive` consumer the duplicate that
   * `ingest` itself was about to drop: the panel appended the row twice, the
   * three trees applied it twice, and a repeated `gate` event put a second
   * re-run offer in front of the developer.
   */
  it('does not re-emit an event it has already seen', () => {
    const { state } = stateWith([]);
    const seen: number[] = [];
    state.onDidReceive((e) => seen.push(e.id));
    feed(state, [
      event(1, 'turn_start', { turn: 1, mode: 'coder' }),
      event(2, 'assistant', { text: 'hello' }),
      // The reconnect that resumed one event too early.
      event(1, 'turn_start', { turn: 1, mode: 'coder' }),
      event(2, 'assistant', { text: 'hello' }),
      event(3, 'finish', { outcome: 'done', summary: 'done', turns: 1, mutations: [] }),
    ]);
    assert.deepEqual(seen, [1, 2, 3], 'a duplicate must be dropped before anyone renders it');
  });

  it('still re-emits the transient events, which carry no cursor', () => {
    const { state } = stateWith([]);
    const kinds: string[] = [];
    state.onDidReceive((e) => kinds.push(e.type));
    feed(state, [
      event(4, 'assistant_delta', { text: 'pack' }),
      event(4, 'assistant_delta', { text: 'age' }),
      event(4, 'heartbeat'),
      event(4, 'assistant', { text: 'package' }),
    ]);
    assert.deepEqual(kinds, ['assistant_delta', 'assistant_delta', 'heartbeat', 'assistant']);
  });
});

describe('RunState — raising an approval', () => {
  /**
   * The card is drawn by the renderer from the raw event, but the *answer* is
   * owned by `ApprovalService`, which bootstraps from `present()`. Nothing puts
   * an approval in front of that service except this emitter, so a silent
   * emitter is a run that stalls until the runtime times the call out and
   * records it as a rejection. Both entry points are covered because they fail
   * independently: one is the live frame, the other is a session reopened after
   * the frame has gone.
   */
  function approvalsRaisedBy(
    drive: (state: InstanceType<typeof RunState>) => void,
  ): string[] {
    const { state } = stateWith([]);
    const raised: string[] = [];
    state.onDidRequestApproval((approval) => raised.push(approval.id));
    drive(state);
    return raised;
  }

  it('raises a live tool_pending frame', () => {
    const raised = approvalsRaisedBy((state) =>
      feed(state, [
        event(1, 'tool_pending', {
          id: 'a1',
          tool: 'patch_file',
          arguments: { path: 'handler/message.go' },
          reason: 'writes a file',
          paths: ['handler/message.go'],
          protected: [],
          unconditional: false,
        }),
      ]),
    );
    assert.deepEqual(raised, ['a1'], 'nothing would ever ask the developer to answer');
  });

  it('raises an approval that was already waiting when the session was opened', () => {
    const raised = approvalsRaisedBy((state) =>
      state.hydrate({
        id: 's1',
        task: 'add a repo function',
        workspace: '/w',
        status: 'running',
        created_at: new Date(0).toISOString(),
        finished_at: null,
        summary: '',
        mutations: [],
        events: 0,
        resumable: true,
        queued: 0,
        winding_down: false,
        pending_approvals: [
          {
            id: 'a2',
            tool: 'write_file',
            arguments: { path: 'repo/postgres/message.go' },
            reason: 'writes a file',
            paths: ['repo/postgres/message.go'],
            protected: [],
            unconditional: false,
          },
        ],
      }),
    );
    assert.deepEqual(
      raised,
      ['a2'],
      'an approval that outlived its frame is unanswerable unless hydrate re-raises it',
    );
  });

  it('does not raise an answered approval replayed from a running transcript', () => {
    // BUG EXT-3. Opening a *running* session hydrated from a tree summary with
    // no transcript, so the stream replayed the whole thing through the live
    // path: every approval the run had ever raised came back as a card with
    // Accept and Reject on it, and the poller then toasted "recorded as a
    // rejection" for each one. `openSession` fetches the transcript for a
    // running session too, and a transcript is history whatever the status.
    const raised = approvalsRaisedBy((state) =>
      state.hydrate({
        id: 's1',
        task: 'add a repo function',
        workspace: '/w',
        status: 'running',
        created_at: new Date(0).toISOString(),
        finished_at: null,
        summary: '',
        mutations: [],
        events: 2,
        resumable: true,
        queued: 0,
        winding_down: false,
        transcript: [
          event(1, 'tool_pending', {
            id: 'answered',
            tool: 'write_file',
            arguments: { path: 'a.go' },
            reason: 'writes a file',
            paths: ['a.go'],
            protected: [],
            unconditional: false,
          }),
          event(2, 'tool_result', { id: 'answered', name: 'write_file', ok: true }),
        ],
        pending_approvals: [],
      }),
    );
    assert.deepEqual(raised, [], 'a decided approval must never come back as a live card');
  });
});
