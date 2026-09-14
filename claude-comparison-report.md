# dakcoder vs Claude Code: architecture comparison

Date: 2026-09-14.

Claude Code read from **published third-party analyses** of the March 2026
source-map leak (v2.1.88, `cli.js.map`, ~512,000 lines of TypeScript) and of
earlier deobfuscation work on v1.0.33 — not from the leaked source itself. That
is a deliberate choice and it is also a caveat: everything attributed to Claude
Code below is second-hand, several sources disagree with each other on concrete
numbers, and the ones that disagree are flagged where they matter. Where a claim
is load-bearing for a recommendation, the disagreement is stated rather than
averaged away.

dakcoder read from this checkout at `fd393cc` (0.4.1), plus the changes landed on
2026-09-14. Your side is `apps/agent/src/dakcoder_agent` (`loop.py`,
`context.py`, `projection.py`, `transcript.py`, `compaction.py`, `gate.py`,
`modes.py`, `migration.py`, `hooks.py`, `plan.py`, `loopstate.py`, `undo.py`,
`tools/`), `apps/gateway`, `gotools/`, `packages/knowledge/` and `extension/src`.

This is the same exercise as [docs/CLINE-COMPARISON.md](docs/CLINE-COMPARISON.md)
against a different subject, and it reaches a different conclusion, because
Claude Code is not the same kind of thing Cline is.

---

## 0. The frame, before any table

A comparison that does not state this first is misleading in both directions.

| | Claude Code | dakcoder |
|---|---|---|
| Model | Frontier (Opus/Sonnet 4.5-4.6), plus Haiku for classification | Qwen3.8-27B on self-hosted vLLM via LiteLLM |
| Scope | Any language, any repo, any task | Go services on one template (`n-api-template`) |
| Users | Public, millions, adversarial and untrusted | One government org, one template, known users |
| Size | ~512,000 lines TS, 1,902 files | ~50,000 lines Python + a Go sidecar |
| Distribution | npm CLI + IDE bridges + SDK + cloud | VS Code extension + local daemon + central gateway |
| Trust boundary | Runs arbitrary shell on a user's machine | Runs 9 allow-listed binaries, no shell |

**The model is the axis everything else rotates around.** A large part of what
looks like over-engineering in `loop.py` — forced `tool_choice`, the intercept
ledgers, 24 named bounds, refusing a plan whose steps are directories, making
the model answer with prose when it cannot stop calling tools — is not a
generalisable agent pattern. It is what it costs to get a 27B model to finish a
task. Claude Code does not need `STALLS_BEFORE_ANSWER` because Opus can produce
a non-action; your own `tools/control.py` docstring records that Qwen cannot,
5 times out of 5. Conversely, a lot of what Claude Code carries — the bash AST
parser, the permission classifier, sub-agent IPC, 88 feature flags — is the cost
of shipping to people you do not know, running commands you cannot enumerate.

Both codebases independently reached the same headline conclusion, which is
worth stating because it is the most useful thing in this document: the
[VILA-Lab analysis](https://github.com/VILA-Lab/Dive-into-Claude-Code) measures
Claude Code as **1.6% AI decision logic, 98.4% deterministic infrastructure** —
permission gates, context management, tool routing, recovery. Your
`docs/ARCHITECTURE.md` decision log is 1,700 lines arguing the same case from
first principles. Neither of you believes the agent is the prompt.

---

## 1. Sources

| Source | What it is good for | Caution |
|---|---|---|
| [kubesimplify: what the source actually teaches](https://blog.kubesimplify.com/claude-code-leak-what-the-source-actually-teaches) | `ToolSearch`/`defer_loading`, the five compaction strategies, `promptCacheBreakDetection.ts`, `denialTracking.ts` | Says `query.ts` is 1,729 lines; others say the query engine is 46,000 |
| [tanbiralam/claude-code](https://github.com/tanbiralam/claude-code) | Directory layout: ~50 commands, ~40 tools, ~140 components, `src/bridge/` | A mirror of allegedly-leaked source; claims unverified |
| [karanprasad: reverse-engineering 512K lines](https://karanprasad.com/blog/how-claude-code-actually-works-reverse-engineering-512k-lines) | The most concrete: compaction thresholds, 7-stage permission pipeline, bash parser limits, 5-tier model fallback, terminal renderer | Most numbers appear only here; treat single-sourced figures as indicative |
| [VILA-Lab: Dive into Claude Code](https://github.com/VILA-Lab/Dive-into-Claude-Code) | The best *design* source — seven layers, six design decisions, the assemble/model/execute extension contract | Analytical rather than forensic; a snapshot, explicitly |
| [claudefa.st: everything found](https://claudefa.st/blog/guide/mechanics/claude-code-source-leak) | Subagent execution models, autoDream, feature flags, KAIROS | Feature-inventory framing; unreleased ≠ shipped |
| [sabrina.dev: comprehensive analysis](https://www.sabrina.dev/p/claude-code-source-leak-analysis) | The bash-parser CR/LF disagreement, the compaction-failure BigQuery numbers | Security-incident framing |
| [wavespeed: architecture deep dive](https://wavespeed.ai/blog/posts/claude-code-architecture-leaked-source-deep-dive/) | Three-layer compression, mailbox pattern, telemetry | Short; overlaps the above |
| [Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts) | Prompt assembly: 500+ conditional strings, ~150 system sections, ~70 agent prompts | Extracted, versioned per release |
| [hqman/claude-code-source-code-deobfuscation](https://github.com/hqman/claude-code-source-code-deobfuscation) | Cleanroom deobfuscation of the npm package | README documents method, not findings |
| [shareAI-lab analysis, v1.0.33](https://github.com/shareAI-lab/learn-claude-code) (via [Medium summary](https://medium.com/@sampan090611/claude-code-feels-like-a-senior-dev-heres-what-actually-makes-it-different-and-what-the-49c02b456d9c), [DeepWiki](https://deepwiki.com/myopicOracle/analysis_claude_code_in_English/2.2-message-queue-and-real-time-steering)) | The named internals: `nO` master loop, `h2A` dual-buffer queue, `wU2` compressor | A year older than the leak; names are from obfuscated symbols |
| [layer5](https://layer5.io/blog/engineering/the-claude-code-source-leak-512000-lines-a-missing-npmignore-and-the-fastest-growing-repo-in-github-history/), [zscaler](https://www.zscaler.com/blogs/security-research/anthropic-claude-code-leak) | The leak itself: missing `.npmignore`, 59.8 MB source map, 2026-03-31 | Incident reporting, not architecture |

**Where sources conflict**, this report says so inline. The three biggest:
query-engine size (1,729 vs 46,000 lines), tool count (40 vs 54), and bash
security size (4,437 lines/23 files vs 9,707 lines/3 files/22 validators). None
of the recommendations below depend on which is right.

---

## 2. Summary

| # | Dimension | dakcoder | Claude Code | Edge |
|---|---|---|---|---|
| 1 | Loop shape | Python generator; `run` → `_run` → `_turn` → `_tool_calls`, events teed through one funnel | Async generator `queryLoop` with 7 named continue sites; serialisable mid-turn | Claude Code |
| 2 | Reasoning distribution | Harness decides; model chooses within it | Same, measured at 1.6% / 98.4% | Even — same philosophy |
| 3 | Modes / permission | 3 tool allow-lists (ASK/PLANNER/AGENT) | 6-7 permission modes × 7-stage decision pipeline | Claude Code |
| 4 | Tool catalogue | 38 specs, 25 in AGENT, typed contract C1, published catalogue, CI drift test | ~40-54 tools, per-tool `isReadOnly`/`isDestructive` | dakcoder on contract rigour |
| 5 | Schema delivery | Full per-mode schema list every turn (2.8k-4.4k token prefix, ceilinged in CI) | `defer_loading` + `ToolSearch` meta-tool; fuzzy-matched injection | **Claude Code** |
| 6 | Parallel execution | Opt-in `ToolSpec.parallel`, 7 tools, 4 threads, batch ≤ 6 | Parallel dispatch with a scheduler; read/write separation | Even |
| 7 | Command safety | 9-binary allow-list, no shell, argv only | Full bash AST parser, 15 rejected node classes, 35+ blocked builtins, 50 ms/50k-node budget | Different problems; dakcoder's is safer *and* narrower |
| 8 | Approval | `Approval` enum (none/conditional/always), per-tool `needs_approval`, protected paths, HTTP round trip | 7-stage cascade, rule engine, hooks, LLM classifier, 1 h cache, denial tracking | Claude Code |
| 9 | Canonical vs projected context | Append-only `Transcript` + `CompactionState` sidecar + `Projector`; prefix-hash safety catch | Append-only JSONL + chain patching at read time | **Even — you landed the same idea** |
| 10 | Compaction | 2 strategies (`agentic`, `basic`), 35% retain, thrash detector (3 in 8 turns), one overflow recovery | 5-6 graduated strategies: snip, microcompact, context collapse, autocompact, reactive | **Claude Code** |
| 11 | Compaction triggers | Threshold on budget; reactive on a classified 400 | `effectiveContextWindow − 13,000`; warning at −20,000; circuit breaker after 3 failures | Claude Code |
| 12 | Prompt caching | Prefix-stable by construction; one system prompt for all modes; budgets asserted in tests | 14 tracked cache-break vectors, sticky-on flag latches, deliberate MCP cache boundary | Claude Code |
| 13 | Verification | **Deterministic 11-stage gate**, baselined, scoped, fail-fast, plus a sub-second inner loop after every edit | No built-in gate; verification is hooks + a verification sub-agent prompt | **dakcoder, decisively** |
| 14 | Plan state | Typed `PlanStep` with status from ground truth, `revise_plan`, loop-initiated replan, phased migration state machine | Plan mode, TodoWrite, UltraPlan (unshipped); no typed plan the harness verifies against | **dakcoder** |
| 15 | Sub-agents | **None** | Task tool + 3 execution models (Fork/Teammate/Worktree), isolated context, sidechain transcripts, mailbox IPC | **Claude Code** |
| 16 | Steering | `steer()` drained at turn start; pinned as a directive | `h2A` async dual-buffer queue; mid-stream injection, pause/resume | Claude Code |
| 17 | Loop guards | 24 named bounds, 3 intercept ledgers, dead-end ledger, overlap detection, churn ledger | Denial tracking (3/20), compaction circuit breaker, stall events | **dakcoder** |
| 18 | Memory / knowledge | Generated `SKILL.md` always resident + 17 on-demand references + 10 diagnostic playbooks | 4-level CLAUDE.md hierarchy, auto-memory (<200 lines/25 KB), autoDream consolidation | Claude Code on breadth, dakcoder on drift-proofing |
| 19 | Extensibility | `beforeTool`/`afterTool` hooks only | Hooks (27 events/5 categories), Skills, Plugins (10 component types), MCP client | **Claude Code** |
| 20 | Persistence & resume | Transcript + compaction sidecar + `events.jsonl` + journal + `plan.md` + undo manifest; `rehydrate` | Append-only JSONL, sidechains, global prompt history, chain patching | Even |
| 21 | Undo | Per-path pre-image snapshot, correct on a dirty tree and untracked files | Not documented in these sources | **dakcoder** |
| 22 | Model routing | Role → model at the gateway; one model in practice | Dual-model (Opus reason + Haiku classify), 5-tier fallback chain, 4 providers | Claude Code |
| 23 | Observability | `metrics.py`, OTel, central ledger, `debug.jsonl` prefix-delta recording | 600+ `tengu_` flags, frustration/continue-counter telemetry | Even |
| 24 | UI | VS Code webview + SSE with `since_id` resume | React Fiber reconciler for terminals, Int32Array frame diffing, 10 FPS | Not comparable |

Rough shape: **you lead on verification, plan state, loop discipline and undo.
Claude Code leads on context management, extensibility, delegation, and
everything about running untrusted work at scale.** The single biggest
structural gap is sub-agents; the single cheapest win is deferred tool loading.

---

## 3. Row by row, where it matters

### 3.1 The loop (rows 1, 2)

Claude Code's loop is `async function* queryLoop(...)` — an async generator with
seven explicit "continue sites" where control yields and transitions occur. The
kubesimplify piece is right about why that shape earns its place: a generator
can be paused, resumed, serialised, and can survive a mid-turn error without
losing the turn. The shareAI-lab work on v1.0.33 calls the same thing `nO` and
describes it as a single-threaded master loop.

Yours is also a generator chain — `run` tees every event through one funnel into
metrics and the debug recorder, `_run` drives, `_turn` builds and dispatches,
`_tool_calls` handles the batch — and the funnel discipline is better than
anything the sources describe on Claude Code's side. What you do not have is
*resumable mid-turn state*: an exception inside `_tool_calls` loses the turn,
and resume rebuilds from the transcript rather than from a serialised loop
position. `rehydrate.py` is close, but it restores the conversation, not the
loop.

Both codebases put the intelligence in the harness. That is the agreement worth
noticing: two teams, two orders of magnitude apart in size, independently
concluded that the agent is mostly deterministic plumbing.

### 3.2 Tool schema delivery (row 5) — the clearest cheap win

Claude Code marks tools `defer_loading: true` and exposes a meta-tool,
`ToolSearch`, that the model calls with a query; the harness fuzzy-matches
deferred descriptions and injects the matching schemas. Schemas assemble once at
session start and stay stable, so this does not cost cache stability.

You send the full per-mode schema list on every turn. `test_prompts.py` pins the
cost: ASK 2,800, PLANNER 3,380, AGENT 4,420 tokens — and the ceiling had to be
raised by 20 tokens on 2026-09-14 to add one sentence to a field description.
That test is doing exactly its job, and the pressure it is registering is real:
25 tools in AGENT is already 4.4k tokens of every prompt, and the surveys
(`db_roundtrip_audit`, `validation_audit`, `temporal_audit`,
`lib_version_check`) are in ASK and PLANNER *only because* the prompt cost was
judged not worth it in AGENT — the comment in `registry.py` says so outright.

Deferred loading dissolves that trade. The surveys could be visible everywhere
and cost nothing until asked for. On a 27B model with a 235k window this matters
more than it does for Claude Code, not less: your prefix is a larger fraction of
a smaller effective attention budget.

### 3.3 Context: where you match, and where you do not (rows 9-12)

**You match on the hard part.** `transcript.py` + `compaction.py` +
`projection.py` is the same design Claude Code arrived at: an append-only record
that is never rewritten, a sidecar that says how it projects, and a projection
computed on the way to the model. Your `source_prefix_hash` catch — refuse a
sidecar whose records no longer hash to what they hashed to — is a safety
property none of the Claude Code sources mention. Their equivalent, "chain
patching reconstructs message sequences at read time without destructive disk
edits", is the same instinct without the stated guard.

**You do not match on graduation.** Claude Code has five to six strategies that
fire under different pressure:

1. **Snip** — fast lossy pruning of old messages
2. **Microcompact** — targets *tool outputs only*, persists large reads to disk with a model-visible summary, no API call
3. **Context collapse** — progressive compression of older segments
4. **Autocompact** — full summarisation at a computed threshold
5. **Reactive** — emergency, on an actual `prompt_too_long`/413

You have `agentic` and `basic`, plus one overflow recovery at `retain_pct=0.15`.
Your projection already does caps, slice supersession and repeat collapse, which
covers some of what snip and microcompact do — but it does it *every turn on
everything*, not as a graduated response to pressure, and it never persists a
large read to disk in exchange for a summary.

**Microcompact is the one to steal.** Your single largest context consumer is
tool output — a `read_file` of a 6,571-line handler, a `repo_map`, a
`legacy_audit`. Claude Code writes those to disk and leaves a model-visible
pointer. You already have every piece: `router.workspace`, the
`.dakcoder/sessions/<id>/` directory, `CACHED_RESULT_CHARS`, and a projection
layer that is the right place to apply it. What you do not have is the *step
between* "cap it at insertion" and "compact the whole conversation".

Their thresholds, for calibration ([karanprasad](https://karanprasad.com/blog/how-claude-code-actually-works-reverse-engineering-512k-lines)):

```
effectiveContextWindow = contextWindow − min(modelMaxTokens, 20_000)
autoCompactThreshold   = effectiveContextWindow − 13_000
warningThreshold       = effectiveContextWindow − 20_000
```

For a 200k model: compact at ~167k, warn at ~160k. Yours is 235,520 prompt +
16,384 output + 10,240 reserve, which is the same arithmetic with the reserve
named differently. Your circuit breaker is `_thrashing` (3 compactions in 8
turns); theirs halts compaction entirely after 3 consecutive failures. Theirs is
the one that matters — sabrina.dev reports an internal BigQuery query finding
1,279 sessions with *up to 3,272 consecutive compaction failures*, burning
~250,000 API calls a day. Your `_summariser_failed` path should have the same
hard stop, and on a metered shared GPU budget the argument is stronger.

### 3.4 Verification: your decisive lead (row 13)

None of the ten sources describes anything in Claude Code equivalent to
`gate.py`. What they describe instead is: hooks that *can* run a linter, a
"Verification Agent" prompt that tells a sub-agent "the implementer is an LLM,
verify independently", and the model's own judgement about whether it is done.

You have an 11-stage ordered pipeline — `go_build`, `govalid_gen`, `go_build`
again, `rules_lint`, `swagger_check`, `go_vet`, `go_test`, `go_mod tidy`,
`golangci_lint`, `routes_check`, `govulncheck` — that the model does not choose
to run, cannot skip, and cannot see in its tool list. Every blocking stage is
baselined against the workspace as it arrived and scoped to what the run
touched, so a legacy service's pre-existing 166 violations are not charged to
the run. Plus an inner loop after every mutating batch, sub-second, that
promotes a step from `written` to `done` only when the formatter and contract
linter come back clean over what was written.

`registry.py` states the principle Claude Code's design does not: *"a model that
chooses whether to run `go vet` is one that sometimes does not."* That is the
right call and it is the single thing in this codebase most worth not losing.
The cost is generality — the gate only exists because the target is one template
in one language.

### 3.5 Plan state (row 14)

Claude Code has Plan mode (a tool allow-list, like yours), TodoWrite (a list the
model writes and the harness does not verify), and UltraPlan (unshipped: Opus
4.6, 30-minute thinking window, browser approval UI). None of these is a typed
plan the harness checks work against.

Yours is. `PlanStep` carries `file`, `action`, `accepts`, `phase`, `part` and a
`status` the *loop* sets from the change set — `written` when a mutation lands,
`done` only when the inner gate is clean over it, `failed` when a gate failure
names the file. `_why_not_done()` is a single completion guard. `migration.py`
adds a phase state machine on top, with `plan_objection` refusing a migration
plan that is not phased or whose steps are too big for one reply.

The 2026-09-14 `/migrate` bug is the counter-argument worth recording honestly:
the whole mechanism hangs on `PlanStep.covers`, and one malformed `file` field
(`"go.work, go.work.sum"`) made four independent checks fail at once and put the
run in an infinite delete/restore loop. Ground-truth status tracking is a
stronger design than TodoWrite *and* it has a single point of failure TodoWrite
does not have. The fix landed; the lesson is that a join this load-bearing needs
its inputs validated at the boundary, which is now what `split_paths` does.

### 3.6 Sub-agents: the structural gap (row 15)

You have none. Claude Code has:

- The **Task tool as a first-class registry member** — no special orchestration path; spawning a sub-agent is a tool call
- **Three execution models**: Fork (same process, inherited context), Teammate (separate session), Worktree (isolated git worktree)
- **Isolated context with sidechain transcripts** — the child's history does not enter the parent's context; only the result does
- **Six built-in agent types** plus custom agents in `.claude/agents/*.md`
- **Mailbox IPC** at `~/.claude/work/ipc/`, 500 ms polling, leader-follower, atomic claim; with 13 documented race conditions, which is the honest part

For a migration this is not a nice-to-have. Your own `migration.py` exists
because a forty-handler service cannot be planned in eight steps, and
`plan_objection` refuses a step on a 6,571-line file because one reply cannot
convert it. A read-only Explore sub-agent — isolated context, returns a summary,
costs the parent nothing but the summary — is exactly the shape that makes
"survey all eight handlers before planning phase two" affordable. Today that
survey burns the parent's context and its `MAX_RESEARCH_TURNS` budget.

Start read-only. `docs/CLINE-COMPARISON.md` already lists "read-only subagents"
as outstanding; this is the second independent codebase pointing at it.

### 3.7 Steering (row 16)

Yours drains `self.steer()` at the top of `_turn`, appends each correction as a
user message, and pins it as a directive. That is correct and it is
turn-granular: a correction typed while a 30-second inference is in flight lands
after it.

Claude Code's `h2A` is an async dual-buffer queue sitting between the keyboard
and the loop, supporting pause/resume and mid-stream injection. For a 27B model
that can spend a whole turn on a dead end, mid-stream cancel-and-inject is worth
more to you than to them. You have the abort path already
(`POST /v1/sessions/{id}/abort`); "abort, inject, re-dispatch the same turn" is a
smaller change than a full dual-buffer queue and gets most of the value.

### 3.8 Loop guards: your other lead (row 17)

Claude Code's documented guards are thin by comparison: `denialTracking.ts` is
46 lines (3 consecutive denials or 20 per session → fall back to prompting), a
compaction circuit breaker at 3 failures, and stall events after 30 s.

You have 24 named module-level bounds, each with a recorded field failure in its
docstring, plus three intercept ledgers (repeat, partial, truncated), a dead-end
ledger tools populate themselves, retrieval- and search-overlap detection, and
as of 2026-09-14 a delete/restore churn ledger. `loopstate.py` groups all of it
into five owners with an invalidation rule per group.

This is genuinely better engineering than what the sources attribute to Claude
Code, and it is better *because* the model is worse. Do not let a future refactor
cargo-cult it away on the grounds that "Claude Code doesn't need this".

### 3.9 Extensibility (row 19)

Claude Code: hooks at 27 events across 5 categories; Skills (`SKILL.md` with
YAML frontmatter, progressive disclosure); Plugins with 10 component types; MCP
as a client for third-party servers. VILA-Lab abstracts the contract to three
stages — `assemble()` (what enters context), `model()` (what can be requested),
`execute()` (permission gates and pre/post hooks) — and the rule *"load an
extension's instructions and schemas when they become relevant"*.

You have `hooks.py` with `beforeTool`/`afterTool` and two good safety rules (a
hook cannot silently succeed; a hook cannot impersonate a tool by emitting
`role: tool`). You consume MCP in exactly one direction — a hand-written stdio
client for `gotools` and `gopls` — and you expose MCP via `gotools mcp`. There is
no plugin system and no third-party MCP client config.

That is probably right for now. A single-tenant internal tool does not need a
plugin marketplace. But note that `packages/knowledge/SKILL.md` is already a
Skill in everything but name: YAML frontmatter with `name` and `handle`, always
resident, with 17 references fetched on demand via `search_docs`. You built
progressive disclosure and then built exactly one of them. If a second template
or a second service family ever appears, the generalisation is small.

### 3.10 Prompt structure (row 12, 18)

Claude Code assembles its system prompt from 500+ discrete conditional strings —
~150 system sections, ~70 agent prompts, ~90 data templates — into a reported
32,000+ lines, split into 7 static sections (cached globally) and 13 dynamic
ones, with a deliberate cache-busting boundary placed *after* MCP instructions so
that adding an MCP server does not invalidate the static prefix.

You have one `system.md` under a 1,200-token budget asserted in tests, one mode
overlay under 250 tokens appended as a *user* message, and a state block built
from ground truth at the end of every prompt. Your `prompts/__init__.py`
docstring records why: three separate system prompts cost the previous
implementation three cold prefills per task.

Your approach is better for your situation and it is not a coincidence — a 27B
model given 32,000 lines of conditional instruction would not follow them.
What is worth taking is the *mechanism*, not the volume:
`promptCacheBreakDetection.ts` tracks 14 fields that invalidate the cache
(system prompt hash, tool schema hashes, model, beta headers, effort) and uses
sticky-on latches to stop a feature flag flipping mid-session and breaking the
prefix. You assert prefix stability in tests; you do not *detect* a break at
runtime. On a metered shared endpoint, a cache break you cannot see is money you
cannot attribute.

---

## 4. What to take

### Do now — cheap, and each one closes a gap you have already felt

1. **Deferred tool loading + a `ToolSearch` meta-tool.** The largest single win.
   Set `defer: true` on `ToolSpec`, send deferred tools as name+one-line
   description, and add one meta-tool that injects full schemas on request.
   Immediate effects: AGENT's 4,420-token prefix drops; the four surveys can be
   visible in AGENT without cost (which would have saved the `/migrate` run a
   turn); `test_prompts.py`'s ceiling stops being a tax on documentation.
   *Watch:* a 27B model may not reach for a meta-tool reliably — gate it behind a
   measurement, and keep the 6-8 tools it uses every turn undeferred.

2. **A hard stop on repeated compaction failure.** `_summariser_failed` should
   count, and at 3 consecutive failures stop attempting compaction for the run
   and fall back to `basic` permanently. sabrina.dev's 250,000-wasted-calls-a-day
   figure is what the absence of this costs at scale; on a shared GPU budget with
   a quota ledger, it is your quota it burns.

3. **Runtime cache-break detection.** Hash what goes into the prefix (system
   prompt, tool schema list, model, mode) and emit an event when it changes
   between turns. You already have `metrics.py` and the gateway ledger; a
   "prefix broken on turn N because X" line turns an invisible cost into an
   attributable one.

4. **Validate `PlanStep.file` at the boundary** — done on 2026-09-14, listed here
   because the general lesson is the one to keep: `covers` is the join four
   separate checks depend on, and the same class of bug will recur anywhere a
   single string field is the key between model output and harness state.

### Worth having — a phase of work each

5. **Microcompact.** A third compaction tier between per-turn caps and full
   summarisation: when a single tool result exceeds a threshold, write it to
   `.dakcoder/sessions/<id>/results/<fingerprint>.txt`, replace it in the
   projection with a summary plus the path, and let `read_file` fetch it back.
   No API call. Your projection is already the right seam, and your intercept
   ledger already fingerprints every call.

6. **A read-only Explore sub-agent.** One new tool, isolated `ContextManager`,
   same router in ASK mode, returns a bounded summary into the parent. No
   mailbox, no worktrees, no IPC — Claude Code's own 13 documented race
   conditions are the argument for starting with in-process and read-only. This
   is what makes surveying eight handlers affordable before planning a phase.

7. **Mid-turn steering.** Abort the in-flight request, append the correction,
   re-dispatch the same turn. Reuses the abort path you have.

8. **A serialisable loop position.** Name the points in `_run`/`_turn` where the
   loop can be suspended and resumed, and persist the cursor with the transcript.
   Claude Code's seven continue sites are what make a session resumable mid-turn
   rather than mid-conversation. `rehydrate.py` gets you most of the way.

### Later, if the scope grows

9. **Generalise Skills.** `packages/knowledge/SKILL.md` is one; make the loader
   take N, keyed by which template or service family the workspace matches.
10. **A third-party MCP client.** Only if someone actually needs a tool you do
    not want to write. The gotools bridge proves the protocol work is done.
11. **Role-differentiated models.** The gateway already routes role → model. If a
    smaller/faster model ever lands on the endpoint, `_classify` and
    `_recap_call` are the two obvious consumers — Claude Code's Opus-reasons /
    Haiku-classifies split, with your plumbing already in place.

---

## 5. What not to take

- **A bash AST parser.** Claude Code needs 4,437-9,707 lines of recursive-descent
  parsing, 22-23 validators and a tree-sitter WASM grammar because it runs
  arbitrary shell for untrusted users — and sabrina.dev documents a live bypass
  where two parsers disagree on `\r` tokenisation. Your 9-binary argv allow-list
  with no shell is *more* secure and two orders of magnitude smaller. Keep it.
- **An LLM permission classifier.** The YOLO classifier is a two-stage model call
  (64 tokens fast / 4,096 tokens full) with a 1-hour cache and 60-80% hit rate.
  On your endpoint that is latency and quota spent to avoid a dialog a known user
  can answer. Your `Approval` enum plus protected paths is the right size.
- **88 compile-time feature flags and 600+ runtime flags.** That is the cost of
  shipping unreleased features inside a public binary. You ship to one org.
- **A terminal renderer.** You have a webview.
- **32,000 lines of conditional system prompt.** Your model would not follow it,
  and your `SYSTEM_BUDGET = 1_200` assertion is a better discipline than their
  500-string assembly.
- **Multi-agent mailbox IPC with file locks.** 13 documented race conditions —
  5 privilege escalation, 3 information disclosure, 3 DoS — is the price. If you
  ever need parallel writers, the git-worktree model is the one to copy, not the
  mailbox.

---

## 6. Where you are ahead, so it is not cargo-culted away

1. **The deterministic gate.** Nothing in Claude Code is equivalent. A model that
   chooses whether to verify sometimes does not.
2. **Baselining and scoping.** Every blocking stage is measured against the
   workspace as it arrived and scoped to what the run touched. This is the thing
   that makes an agent usable on a legacy codebase at all, and no source
   describes Claude Code doing it.
3. **Plan status from ground truth.** `written` from the change set, `done` from
   the inner gate, never from the model saying so.
4. **Named bounds with recorded causes.** 24 constants, each with the field
   failure that produced it in its docstring. This is the best documentation in
   the codebase and it is why the `/migrate` bug was diagnosable in an hour.
5. **Undo that is correct on a dirty tree.** Per-path pre-image snapshots rather
   than `git checkout HEAD`. Neither Claude Code nor Cline gets this right in the
   way `undo.py` does.
6. **The contract discipline.** `docs/TOOL-CATALOG.md` + `tool-catalog.json`
   regenerated from the registry with a CI test that fails on drift, plus
   `PREFIX_CEILING` asserting the prompt cost per mode. Claude Code's schemas are
   discoverable only by leaking them.
7. **The gateway.** Auth, quota, ledger and model proxy as an unbypassable
   boundary, with the key never on a developer's machine. Claude Code's
   equivalent is per-user API keys.
8. **Tools that refuse usefully.** `read_file` on a missing path names the three
   closest files that exist and says "do not retry this path";
   `legacy_audit` in the wrong mode lists what AGENT actually has. Every refusal
   in `router.py` names the alternative. This is worth more on a 27B model than
   any single prompt improvement.

---

## 7. The honest summary

dakcoder is a *narrower, more verified, more tightly bounded* agent than Claude
Code, and that is the correct design for a 27B model pointed at one template.
Claude Code is a *broader, more extensible, better-contextualised* agent, and
that is correct for a frontier model pointed at every repository on earth.

The three gaps that are real regardless of scope:

1. **Deferred tool loading** — you are paying 4.4k tokens a turn for tools the
   model uses once a session, and the cost is already distorting design decisions
   (the surveys being withheld from AGENT).
2. **Graduated compaction** — you have two strategies where five earn their
   place, and the missing middle tier is exactly the one that would handle your
   biggest context consumer (large tool results).
3. **Read-only sub-agents** — the thing that makes a forty-handler migration
   plannable without burning the parent's context, and the second independent
   comparison to point at it.

Everything else on the Claude Code side of the ledger is either a consequence of
running untrusted work at scale, or something you have already built.

---

## 8. File map

### dakcoder

| Concern | File |
|---|---|
| Agent loop, bounds, state | `apps/agent/src/dakcoder_agent/loop.py` |
| Loop state groups | `apps/agent/src/dakcoder_agent/loopstate.py` |
| Modes, budgets, reasoning | `apps/agent/src/dakcoder_agent/modes.py` |
| Canonical transcript | `apps/agent/src/dakcoder_agent/transcript.py` |
| Compaction sidecar | `apps/agent/src/dakcoder_agent/compaction.py` |
| Projection | `apps/agent/src/dakcoder_agent/projection.py` |
| Context assembly, layers, budget | `apps/agent/src/dakcoder_agent/context.py` |
| Verification gate | `apps/agent/src/dakcoder_agent/gate.py` |
| Plan and agenda | `apps/agent/src/dakcoder_agent/plan.py`, `tools/control.py` |
| Migration phase machine | `apps/agent/src/dakcoder_agent/migration.py` |
| Tool contract C1 | `apps/agent/src/dakcoder_agent/tools/registry.py` |
| Tool router, six checks | `apps/agent/src/dakcoder_agent/tools/router.py` |
| Hook seam, parallel rule | `apps/agent/src/dakcoder_agent/hooks.py` |
| Undo snapshots | `apps/agent/src/dakcoder_agent/undo.py` |
| Local HTTP + SSE | `apps/agent/src/dakcoder_agent/loopback.py` |
| Prompt assembly | `apps/agent/src/dakcoder_agent/prompts/` |
| Knowledge / the one Skill | `packages/knowledge/SKILL.md`, `references/` |
| Diagnostic playbooks | `apps/agent/src/dakcoder_agent/playbooks/` |
| Go analysis sidecar | `gotools/` |
| Auth, quota, model proxy | `apps/gateway/src/dakcoder_gateway/` |

### Claude Code (as named by the sources; not verified against source)

| Concern | File / symbol |
|---|---|
| Agent loop | `query.ts` → `queryLoop`; `nO` in v1.0.33 |
| Message queue / steering | `h2A` dual-buffer queue |
| Context compression | `microCompact.ts`, `apiMicrocompact.ts`; `wU2` in v1.0.33 |
| Cache-break detection | `promptCacheBreakDetection.ts` (14 fields) |
| Tool base contract | `Tool.ts` (`isReadOnly`, `isDestructive`) |
| Deferred loading | `TOOL_SEARCH_TOOL_NAME = 'ToolSearch'` |
| Denial tracking | `denialTracking.ts` (46 lines, 3/20) |
| Bash security | `bashSecurity.ts` (22-23 numbered checks) |
| Tools / commands / UI | `src/tools/`, `src/commands/`, `src/components/` |
| IDE bridge | `src/bridge/` |
| Sub-agent IPC | `~/.claude/work/ipc/` |

---

*Written for the dakcoder maintainers. Claude Code figures are second-hand from
the sources in §1 and should be treated as indicative; the recommendations in §4
do not depend on any single disputed number.*
