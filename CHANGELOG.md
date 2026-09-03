# Changelog

## 0.3.1 — 2026-09-03

Extension `0.3.1`, `dakcoder-agent` `0.3.1`, `dakcoder-shared` `0.3.1`,
`dakcoder-gateway` `0.3.1`. Runtime API unchanged at **1.1**.

**Deploy the gateway before the extension.** A 0.3.1 runtime dispatches Ask
turns as the `ask` role, which a 0.3.0 gateway's table does not contain — every
Ask turn against one would come back "'ask' is not a configured role". The
reverse skew is safe: a 0.3.0 runtime only ever sends `coder`, `fast` and
`embed`, all of which a 0.3.1 gateway routes.

### Added

- **A model, an endpoint and a key per role, all from `deploy/dakcoder.env`.**
  `DAKCODER_MODEL_PLANNER=Qwen3-235B-A22B` and a restart is now the whole of
  "put the Planner on a bigger model"; add `_BASE_URL` and `_API_KEY` and it
  can live on another host on another credential. Anything a role does not name
  it inherits from `DAKCODER_MODEL`, `DAKCODER_MODEL_BASE_URL` and
  `DAKCODER_MODEL_API_KEY`, so a one-model deployment sets three variables and
  is done. The roles are `planner`, `coder`, `ask`, `fast`, `summariser`,
  `embed`, plus `verifier` and `debugger` kept for older clients;
  `DAKCODER_MODEL_ROLES` adds more. See `deploy/README.md` and D-94.
- **`GET /v1/models`** (authenticated) — the routing table in force: role,
  model, endpoint, and which of the three the role overrode. Never the keys.
  `/v1/health` gains role → model, and `deploy/status.sh` prints it.

### Fixed

- **A flaky extension test** that failed roughly seven runs in eight, blocking
  `npm run package`. `clears finished_at when a follow-up puts the session back
  to running` built its fixture with `created_at` and `finished_at` both taken
  `now`, so the frozen clock and the fresh one were the same 0ms and the
  assertion turned on which millisecond each `toISOString()` landed in. It also
  asserted the wrong thing — `elapsedMs >= stopped` asks a new run's clock to be
  at least as long as the old run's, which a follow-up to a two-hour run would
  fail on entirely correct behaviour.
- **Every turn dispatched as `coder` whatever mode it was in**, so `planner`
  and `ask` were role-table entries nothing could reach — planning and
  answering were billed, logged and routed as coding. A mode now carries its
  own role.
- **Compaction recaps ask for `summariser` again.** The role was moved to
  `fast` because the client's vocabulary was three names long and this was not
  one of them. Both halves now read the same `ROLES` tuple, with a test
  asserting they agree, so the name that §6.5 specifies resolves — and the
  summariser can be pointed at a small model without moving the intent
  classifier with it.
- **The capability probe covers every distinct endpoint**, not just the
  default. A role pointed elsewhere was previously unprobed, which is the
  failure mode §4.5 exists to prevent. Endpoints shared between roles still
  cost one pass.
- **The credential guards match `DAKCODER_MODEL*_API_KEY` by shape** rather
  than from a fixed list — in the launcher, the extension's spawn, the
  runtime's own startup refusal and the `.vsix` scanner. A per-role key is
  still a key only the gateway may hold, and a list would have gone quietly out
  of date the first time a role was added.
- **A role with no key anywhere stops the gateway at startup**, naming the role
  and the variable, instead of surfacing as a 502 hours later to whichever
  developer reached that role first.

## 0.3.0 — 2026-09-02

Runtime API **1.1**. Extension `0.3.0`, `dakcoder-agent` `0.3.0`,
`dakcoder-shared` `0.2.0`.

The first release where the loop's constants come from measurements against the
live endpoint rather than from argument. What that measurement found is written
up in [docs/ENDPOINT-CAPABILITIES.md](docs/ENDPOINT-CAPABILITIES.md), and the
executable half is `apps/agent/tests/test_live_endpoint.py` behind
`DAKCODER_LIVE=1`.

### Breaking

- **Five modes became three.** `planner`, `scaffolder`, `coder`, `verifier` and
  `debugger` are now `ask`, `planner` and `agent`. Retired names are still
  accepted on the wire and coerced, and a stored session that carries one still
  reads as English in the panel. Runtime API moves to 1.1 for this: a 1.0 client
  is sent mode values its union does not contain.
- **`dakcoder.defaultMode`** is now `auto` / `ask` / `agent`, defaulting to
  `auto`. The old values still work. `multi` used to map onto `planner`, which
  was also the backend default — so the server could not tell "let the agent
  choose" from "the developer asked to plan", and every message, question or
  not, entered the mode whose instruction is *emit numbered steps*.
- **Settings removed** that nothing read: `runtimeIdleTimeoutMinutes`,
  `showTokenMeter`, `verbose`. `DAKCODER_MODE` is no longer set in the child
  environment; nothing read that either.

### Added

- **`finish`** — a terminal tool for `ask` and `agent`, alongside `submit_plan`
  and `ask_developer`. This is the release's central change. In those two modes
  "I am finished" used to mean *not calling a tool*, and past about six fruitless
  calls Qwen3.8-27B cannot produce a non-action: it repeats its last call. The
  planning phase never had the problem because it had typed terminal actions.
- `POST /v1/credential` — the extension pushes a fresh gateway JWT, so a daemon
  that outlives its token recovers instead of 401-ing every call until restart.
- `tool_choice`, `response_format` and `parallel_tool_calls` on `LLMClient`.
  None of the three had a single use anywhere before.
- `docs/ENDPOINT-CAPABILITIES.md` and `test_live_endpoint.py`.

### Fixed

- **The verifier appearing to run forever.** The inner loop appended ~1,000
  tokens after *every* edit, headlined "199 blocking and 480 advisory findings
  across 49 files" with examples from files the run had never opened — 98% of
  the warnings were outside the change. The model went off to fix them. Now
  scoped and baselined: **816 tokens → 0** when the findings predate the run.
- **The run-start baseline: 80.1s → 6.4s.** `go_test` was 74 of those seconds and
  is taken only when the tests can actually run. The container check was
  `shutil.which("docker")`, which finds Docker Desktop's binary with the daemon
  stopped; it runs `docker info` now, cached per process.
- **`rules_lint` could not fail, then could not pass.** It parsed a body that has
  been rendered prose since `_render_lint` landed, so the check fell back to
  `result.ok` and the blocking contract-lint stage could only fail on a sidecar
  crash. Fixing that without a baseline made it block every run on a legacy
  service. It is now baselined on rule *classes*, so a new file written in the
  house style is not charged for a pattern the house has used thirty times.
- **`swagger_check`'s baseline was empty on every run ever.** `_list(None)`
  returned `["None"]`, so the unscoped call — the one the baseline makes — was
  scoped to a file named `"None"` and matched nothing.
- **`go mod tidy` no longer writes `go.mod` at the gate.** It snapshots, runs,
  compares and restores. A run that asked a question used to finish "1 file
  changed: go.mod", written by the gate's own diagnostic.
- **The gate never ran on an empty change set.** A run that wrote nothing cannot
  fail.
- **The read budget counts lines, not calls.** Ten reads per file cut a model off
  at ~280 lines of a 6,571-line handler. It now tracks delivered line intervals
  and refuses only a range already in context; the ceiling scales with the file.
- **`search_repo` says what it searched.** "no matches for 'Routes' in 0 files …
  that is your answer: it does not" is now "nothing was searched: the glob
  matched no files".
- **A crashed run emits `finish`,** and the panel treats `end` as terminal. A
  backend exception used to freeze the panel on "Working…" forever and swallow
  the next message.
- Pre-flight failures render into the panel; four slash commands that dispatched
  to unregistered ids; a dead daemon retrying forever; no request timeouts; no
  in-flight guard on submit; phantom approvals replayed from finished sessions.
- Mid-stream gateway `event: error` frames raised instead of becoming an empty
  answer; client and gateway timeouts aligned so the client gives up first;
  `EmptyCompletionError` recovered whether or not thinking was requested.
- `gotools` discovery matched no binary that exists in this repository.
- `patch_file(new="")` — a deletion — was refused as a missing argument.
- Token calibration compared message content against a `prompt_tokens` that
  includes tool schemas, so every estimate ran high.

### Known and deliberate

- The depth-6 repeat rate is **not** deterministic (4–5 of 5 across sessions),
  and better tool wording helps variably. Only a named `tool_choice` has been
  5/5 every time. The loop forces rather than argues for that reason.
- `tool_choice: "none"` and `tools: []` are both unusable on this endpoint — the
  first leaks `<tool_call>` markup into `content`, the second does that *or*
  refuses with "I don't have access to file system tools". Neither is a way to
  ask for prose.
