# Loop, state and context: what is wrong

An audit of `loop.py`, `loopstate.py`, `context.py` and the persistence path
against the 2026 literature on loop engineering, context engineering and durable
agent state. Everything below is a defect I reproduced, not a preference. What I
checked and found sound is listed at the end, briefly, so the length of this file
is not mistaken for the size of the problem.

Sources are listed at the bottom. The short version of what they say: the
failures that kill long-running agents in 2026 are not reasoning failures, they
are **bookkeeping** failures — a verifier that passes because it was handed the
wrong baseline, a guard that is off because its state did not survive a restart,
a context that evicted the thing the model was working from. All four findings
below are of that kind, which is a compliment to the loop and not to the state.

---

## F1 — The baseline is retaken on every developer message, against a workspace the same session already dirtied

**Severity: high. This makes the gate pass on damage the session caused.**

`_take_baseline` exists to answer one question — what was already broken before
this run could break it — and its own docstring is unambiguous about what that
depends on:

> Correctness depends entirely on the timing: taken later, the snapshot contains
> the run's own damage and excuses it. So it is taken now, when the workspace is
> definitely untouched.
> — [loop.py:1374](../apps/agent/src/dakcoder_agent/loop.py#L1374)

On message 2 of a session the workspace is not untouched. Message 1 of the same
session wrote to it.

`carry_from` clearly intends to prevent this — it carries the baseline forward
explicitly ([loop.py:4514](../apps/agent/src/dakcoder_agent/loop.py#L4514)):

```python
self.state.baseline = previous.state.baseline
```

But `loopback` calls `carry_from` at
[loopback.py:342](../apps/agent/src/dakcoder_agent/loopback.py#L342) and then
`run()` at [loopback.py:403](../apps/agent/src/dakcoder_agent/loopback.py#L403),
and `_run` unconditionally overwrites it
([loop.py:1146](../apps/agent/src/dakcoder_agent/loop.py#L1146)):

```python
if decided is Intent.AGENT and not self.state.migration.defers_gate:
    self._take_baseline()          # no `continued` check, no "already taken" check
```

`_take_baseline` has no idempotence guard of its own — it assigns
`self.state.baseline = take_baseline(self.router)` on a fresh thread every time
it is called. So the carried line is dead, and has been.

**Reproduced.** Spying on `_take_baseline` across two messages of one session:

```
first.run("add a handler")            -> 1 baseline
second.carry_from(first)
second.run("now add another", continued=True)
                                      -> 2 baselines   (expected 1)
```

**What it costs.** Message 1 breaks `go build` and ends `unverified` — correctly,
the developer sees it. Message 2 starts, takes a baseline of a workspace with a
broken build, and records `go_build` as pre-existing. `baseline_key="go_build"`
([gate.py:670](../apps/agent/src/dakcoder_agent/gate.py#L670)) now excuses the
stage for the rest of the message. **Message 2 can report `done` on a build its
own session broke.** This is textbook Verifier Theater — "verifier approves but
tests fail in CI" — reached not through a weak verifier but through a correct
verifier handed a poisoned baseline.

It is also the precise inverse of the migration rule you already got right:
`defers_gate` skips the baseline mid-migration *because* "the run's own damage is
the workspace". The same reasoning applies to any multi-message session; only the
migration case was noticed.

**Fix.** Take it once per session, not once per message — the `routes_saved`
pattern, one field:

```python
if decided is Intent.AGENT and not self.state.migration.defers_gate:
    if not self.state.baseline.taken:
        self._take_baseline()
```

`Baseline.taken` already exists and already distinguishes "we did not look" from
"nothing was wrong" ([gate.py:162](../apps/agent/src/dakcoder_agent/gate.py#L162)),
so the guard needs no new state. Add a test asserting one baseline per session
across N messages — the existing `test_no_baseline_is_taken_during_a_migration`
asserts one per *run*, which is what let this through.

---

## F2 — A restart restores the plan and drops every guard that makes the plan safe

**Severity: high. Guards silently switch off mid-task, and two of them fail open.**

`restore_plan` is the restart counterpart of `carry_from`. `carry_from` moves
about eighteen fields; `restore_plan` moves four — `plan`, `plan_summary`,
`plan_forced`, `migration` ([plan.py:136](../apps/agent/src/dakcoder_agent/plan.py#L136)).
Everything else starts empty.

Both `rehydrate.py` and `carry_from` justify this with the same claim:

> The consequence is bounded and one-directional: the agent may repeat a search
> it had already exhausted. It will not skip work it has not done.
> — [rehydrate.py:24](../apps/agent/src/dakcoder_agent/rehydrate.py#L24)

**That claim is true for the efficiency ledgers and false for the guard ledgers.**
`seen_calls`, `last_results`, `dead_ends` are caches — losing them costs a
repeated search, exactly as documented. But `removed`, `retired`, `churn`,
`routes_saved`/`routes_before` and `asked` are not caches. They are the state
that *blocks* things, and losing them does not cost a repeated search, it
removes a guard.

**Reproduced.** After `restore_plan` on a session that had all of them set:

```
{'removed': set(), 'churn': {}, 'retired': set(), 'asked': [],
 'routes_saved': False, 'routes_before': 0, 'tried': []}
```

Three consequences, in severity order:

**(a) The delete-without-replace guard turns off.** `removed` is what
`_deleted_and_not_replaced` reads, and it is the *first* objection `finish` has
to clear ([loop.py:3243](../apps/agent/src/dakcoder_agent/loop.py#L3243)) — your
own comment says why: "A run that finishes here leaves the developer a repository
with four handlers missing." Reload the window after the deletes and before the
rewrites, and the run can finish `done` with `handler/paogen.go` gone. This is
the field failure the ledger was built for, restored to working order by a VS
Code reload.

**(b) The route-parity check is destroyed rather than skipped.** `restore_plan`
*does* restore `migration`, so `migration.active` is true after a restart — but
`routes_saved` is false. `_save_routes` therefore runs again and calls
`route_inventory` with `{"save": ROUTES_BEFORE}`
([loop.py:5384](../apps/agent/src/dakcoder_agent/loop.py#L5384)), overwriting
`.dakcoder/routes-before.json` with an inventory of the **half-converted**
service. Its own docstring names the outcome:

> A phase later it would be a picture of a half-converted one, and comparing the
> end of the migration against that would pass by construction — the routes it
> had already lost would not be in the baseline to be missed.

The gate stage is conditioned on the file existing
([gate.py:782](../apps/agent/src/dakcoder_agent/gate.py#L782)), which is the
right failure mode for a file that was never written — but it cannot detect a
file that was *replaced with a worse one*. So a mid-migration reload does not
skip the check, it silently makes it vacuous. Of the three, this is the one I
would fix first: it is the only one that converts a passing check into a
meaningless one with no trace.

**(c) The developer gets re-asked a settled question.** `asked`/`answered` exist
because a field run asked "which n-api-* versions should I use?" three times.
That ledger is in-process only.

**Fix.** The asymmetry is the bug: `carry_from` is the honest list of what
outlives a message, and `PlanRecord` is a subset of it chosen by what was easy to
serialise. Either widen `PlanRecord` to carry the guard ledgers — they are all
small, JSON-shaped, and already atomically written beside the plan — or, cheaper
and safer for (b) alone, make `routes_saved` derivable from disk:

```python
if self.state.routes_saved or not self.state.migration.active:
    return
if (self.router.workspace.root / ROUTES_BEFORE).is_file():
    self.state.routes_saved = True      # someone already took it; never overwrite
    return
```

And correct the claim in both docstrings. "One-directional" is the sentence that
stops the next reader from checking, and it is the reason (a) and (b) are still
here.

---

## F3 — Bounds are per-message; nothing bounds a session

**Severity: medium. Correct for attended use, wrong for the path you ship for migrations.**

`max_turns` defaults to 40 and is consumed by `for _ in range(self.max_turns)`
inside `_run` ([loop.py:1149](../apps/agent/src/dakcoder_agent/loop.py#L1149)).
A loop is built per message. So a thirty-message migration is thirty independent
forty-turn budgets — 1,200 turns — and nothing anywhere holds that number.

`session.turns` exists but is reporting only; grep confirms it is never compared
against anything ([session.py:373](../apps/agent/src/dakcoder_agent/session.py#L373)).
`_metrics_acc` is reset per run
([loop.py:1031](../apps/agent/src/dakcoder_agent/loop.py#L1031)), so cost is
accounted per message and never summed per session. The quota lives in the
gateway, and `loopback.py`'s own header says it holds none.

The per-message bound is the right control for an attended panel — the developer
is the outer loop, and that is a defensible design. The gap is specifically the
long autonomous path: `/migrate` is the one entry point where nobody is counting,
and it is also the one where turns are most expensive. The literature's "Token
Burn" failure mode is precisely this shape, and its remedy is a daily budget
rather than a per-invocation one.

**Fix.** A session-level turn and estimated-token ceiling that ends the run with
a distinct outcome — not `EXHAUSTED`, which already means "this message ran out"
— plus a running session total on the usage event so the panel can show it. The
kill switch itself already exists (`session.abort`, `cancelled`, `wind_down`);
what is missing is anything that pulls it automatically.

---

## F4 — One capped `read_file` is 22% of the prompt budget, and today's change made it tighter

**Severity: medium. A design tension you chose deliberately, now closer to its edge than it was.**

Current arithmetic, after this session's raise of `planner` and `agent` to 32,768
output tokens:

| | tokens | as % of budget |
|---|---:|---:|
| prompt budget | 219,136 | — |
| compaction threshold (0.70) | 153,395 | 70% |
| retention floor (0.35) | 76,697 | 35% |
| **one capped `read_file`** | **48,000** | **21.9%** |

Which means: **3.2 full-cap reads reach the compaction threshold, and the
retention floor holds 1.6 of them.** A run working across two large handlers at
once loses one of them at every compaction — and re-reading it is what the 48,000
cap was raised to prevent. `projection.py`'s own comment tracks this ratio and is
the right instinct; it is just no longer a comfortable number.

Two honest qualifications. First, this is a tension and not a bug: the slice
ledger and `view.holds` mean the model is *told* it can re-read, which is the
difference between this and the failure two field transcripts died of. Second,
**I moved it in the wrong direction today** — the budget cut from 235,520 to
219,136 lowered the retention floor by 5,735 tokens and took the "capped reads
the floor can hold" figure from 1.72 to 1.60. `test_budget_regression.py` did not
notice, and cannot: its simulation peaks at ~115k, well under the threshold, so
it is blind to exactly this regime. I recorded that limitation in its docstring.

It is also the one finding where the 2026 evidence points somewhere other than
where you went. The strongest measured result in context engineering this year is
subtractive — targeted retrieval of ~5K tokens beating a 100K-token summary on
identical coding tasks — while this design moved deliberately toward large caps
and a window-sized budget. Your reason is field evidence (sliced re-reading
killed real runs) and it beats a general finding about a different corpus. But
the two are close enough now that it is worth a measurement rather than an
argument.

**Fix.** Not a number change — a test. The budget gate needs a second simulation
whose working set is three or four large files, sized to actually cross the
threshold, asserting that a file under active edit survives compaction. That is
the regime the caps were tuned for and the only one nothing currently exercises.

---

## F5 - `_State.__setattr__` fails silently on a name the table does not know

**Severity: latent now, high when it fires. Nothing enforces the table.**

The facade routes by hand-maintained lookup
([loop.py:254](../apps/agent/src/dakcoder_agent/loop.py#L254)):

```python
def __setattr__(self, name, value):
    owner = _OWNER.get(name)
    if owner is None:
        object.__setattr__(self, name, value)   # silently shadows onto the facade
        return
    setattr(getattr(self, owner), name, value)
```

Reads of an unknown name raise `AttributeError`. Writes do not - they land on
the facade instead of the group. **The asymmetry is the defect:** a write to a
mistyped or newly-added-but-unregistered name is accepted, and reads *of that
same name* then return the shadow value, so the code that set it looks correct
while the group field it was meant to reach stays at its default forever.

Demonstrated:

```
s.stalled_turn = 99            # singular: not in _OWNER
s.stalled_turn   -> 99         # reads back fine
s.stalled_turns  -> 0          # the real bound never moved
s.progress.stalled_turns -> 0
```

Every field in these five groups is a termination bound, a guard, or an
invalidation trigger. A counter that silently never reaches `Progress` is a bound
that never fires - `stalled_turns`, `truncations`, `finish_refused` and
`plan_objections` are all of this shape, and all of them ending a run is the
point of them.

**Currently the table is complete** - I diffed `_OWNER` against
`dataclasses.fields` of all five groups and found no missing keys, no stale keys,
and none pointing at the wrong group. So this is latent, not live. But nothing
keeps it that way: there is no test referencing `_OWNER` anywhere in
`apps/agent/tests`, the groups are not slotted, and the class docstring actively
invites divergence by telling new code to reach for the group while 279 call
sites still reach for the facade.

**Fix.** Two lines and a test. Raise on unknown names in `__setattr__` (the
dataclass's own five fields are set through `object.__setattr__` in `__init__`,
so they are unaffected), and add the completeness check:

```python
def test_owner_covers_every_group_field():
    real = {f.name: g for g, cls in GROUPS.items() for f in dc.fields(cls)}
    assert real == _OWNER
```

---

## F6 - A file's pre-edit content stays in context, unmarked, after the agent edits it

**Severity: high. This is the one most likely to be producing the failures you see.**

`context.py`'s opening argument for the slice ledger is this:

> An agent that reads a file, patches it, re-reads and finds both copies in
> context will reason about the stale one. Superseded slices collapse to a stub
> naming where the live lines are.

That covers read -> patch -> **re-read**. It does not cover read -> patch -> *no
re-read*, and that is the ordinary case, because the model has no reason to
re-read a file it believes it is holding.

`_slice_path` returns `(None, None)` for every tool that is not `read_file`, so a
`patch_file` or `write_file` result carries no path and supersedes nothing.
Reproduced with the loop's real semantics - `read_file` with a path, the mutation
without one, exactly as the loop appends them:

```
read_file  handler/user.go (1,31)   -> body enters context
patch_file handler/user.go          -> no path, supersedes nothing

pre-edit content still in context : True
marked stale anywhere             : False
stale_slices                      : 0
coverage still claims lines       : {'handler/user.go': ((1, 31),)}
view.holds('handler/user.go',1,31): True
```

So the model is still looking at the version of the file from before its own
edit, with nothing saying so, and `view.holds` - which `_live_reads` calls "the
authority on what the conversation holds" - answers `True`. It is right about the
bytes and wrong about the file.

**Why this bites specifically.** `patch_file` requires an `old` string that
matches the bytes on disk exactly and uniquely - that uniqueness rule is the whole
design ([fs.py:399](../apps/agent/src/dakcoder_agent/tools/fs.py#L399)). An anchor
quoted from the pre-edit copy sitting in context will not match once the agent's
own earlier edit has touched that region. The failure presents as `patch_file`
refusing to match, repeatedly, on a file the agent just successfully edited -
which reads like a model problem and is a context problem.

**What is already right, and limits the blast radius.** On every mutation the loop
does `self.state.reads.pop(mutation.path, None)`
([loop.py:2649](../apps/agent/src/dakcoder_agent/loop.py#L2649)) - "a file that
was just written is worth reading again". That clears the *loop's* ledger, so
`_re_reading` returns `""` and a re-read is dispatched rather than refused. The
agent is therefore **permitted** to refresh. It is simply never **told** it needs
to.

**Fix.** The invalidation already exists on the loop side; it needs its mirror on
the context side. Give the projection the same signal `reads.pop` gets - on a
mutation to path *p*, stub every earlier read of *p* with a marker saying what
actually happened:

```
[stale read of handler/user.go lines 1-40: you edited this file after reading it;
 re-read it before quoting an anchor from it]
```

That must be a *different* string from the existing `STALE_PREFIX` stub, which
points at a newer read below - here there is none. Note that the existing
supersession pass would produce exactly that false pointer if mutations ever did
carry a path, so this should not be implemented by making `_slice_path` return the
mutation's path.

---

## Checked and sound

**Read this table with a caveat.** Rows marked (code) I verified by reading the
implementation or running it. Rows marked (docs) I checked against this
codebase's comments, which are unusually thorough and which I let do verification
work they should not have done. The (docs) rows are not yet audited and should
not be treated as cleared.

`gate.py`, `projection.py`, `transcript.py`, `compaction.py`, `undo.py` and
`hooks.py` have still not been read end to end, and neither has the bulk of
`loop.py`. What follows is scope, not a clean bill of health.

| Concern | Finding |
|---|---|
| Same model implements and verifies | Not applicable — the gate is deterministic (`go build`/`vet`/`test`/`rules_lint`), which is stronger than the recommended "separate verifier model"  (code) |
| No attempt cap / infinite fix loop | Comprehensively bounded: `MAX_GATE_FAILURES`, `MAX_STALLED_TURNS`, `MAX_REPLANS`, `MAX_FINISH_REFUSALS`, `MAX_PLAN_OBJECTIONS`, `MAX_TRUNCATIONS`, and `churn` for the case where every turn mutates but nothing progresses  (code) |
| Parallel collision | `_dispatch_parallel` is read-only by construction; non-`ToolResult` and raising calls fall back to sequential dispatch  (code) |
| Compaction loses the decision that explains the diff | Summarise-not-truncate, structured `Recap`, nothing deleted — the transcript is append-only and compaction only moves a sidecar  (docs) |
| Governance decay through compaction | Pinned head, `MAX_DIRECTIVES`, one mode instruction; `tried` and the plan cursor re-render every turn rather than living in evictable history  (docs) |
| Loop/context disagreement | **Retracted - see F6.** The mechanism is right (`view.holds` answers from the pass that built the request, so it cannot be stale about the *messages*) but it is not the authority it is documented as: it answers about bytes in context, not about the file on disk (code) |
| Summariser failure silently degrading every compaction | Announced, typed-vs-broad excepts separated, `basic_recap` fallback, metered  (code) |
| Stale reads after a developer edit | mtime check on follow-up ([loop.py:5821](../apps/agent/src/dakcoder_agent/loop.py#L5821))  (code) - but see F6 for the agent's *own* edits |
| No run log | `events.jsonl` per session, recorded before sent  (docs) |
| Cross-thread steer injection | (code) Both `steer` and `drain_steer` hold the session lock, drained once per turn on the loop thread; the `running`/append TOCTOU is closed (BUG L-9) |
| Prefix-cache thrash | `prefix_key` tracks `(tools, system, mode)` separately and reports which moved  (docs) |

---

## Sources

- [cobusgreyling/loop-engineering](https://github.com/cobusgreyling/loop-engineering) — patterns, [failure modes](https://github.com/cobusgreyling/loop-engineering/blob/main/docs/failure-modes.md), [anti-patterns](https://github.com/cobusgreyling/loop-engineering/blob/main/docs/anti-patterns.md)
- [Loop Engineering (Cobus Greyling)](https://cobusgreyling.medium.com/loop-engineering-62926dd6991c) and the [Playbook](https://cobusgreyling.substack.com/p/loop-engineering-playbook)
- [Effective context engineering for AI agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context engineering: memory, compaction, and tool clearing — Claude Cookbook](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools)
- [Context Engineering: A Practical Guide for AI Agents (2026) — Sourcegraph](https://sourcegraph.com/blog/context-engineering)
- [Governance Decay: How Context Compaction Silently Erases Safety Constraints in Long-Horizon LLM Agents](https://arxiv.org/pdf/2606.22528)
- [Durable Execution for AI Agent Runtimes: Checkpointing, Replay, and Recovery — Zylos Research](https://zylos.ai/research/2026-04-24-durable-execution-agent-runtimes/)
- [Why Checkpoints Aren't Durable Execution — Diagrid](https://www.diagrid.io/blog/checkpoints-are-not-durable-execution-why-langgraph-crewai-google-adk-and-others-fall-short-for-production-agent-workflows)
