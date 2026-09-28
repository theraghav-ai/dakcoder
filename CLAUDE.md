# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

dakcoder is a coding agent for India Post IT 2.0 Go services built on `n-api-template`. It has four parts that ship separately:

- **`apps/agent`** (`dakcoder_agent`): the agent runtime, `dakcoderd`. It holds the loop, the context manager, the tool router and the verification gate. The VS Code extension spawns it locally. In hosted mode it runs one per workspace lease (`--hosted --workspace /workspace`). Same code either way.
- **`apps/gateway`**: identity (GitLab OAuth), quota, ledger and the model proxy. Server only, never in the `.vsix`.
- **`apps/agentsvc`**: the hosted control plane. It handles workspace leases, runner containers, A2A, and delivery as a merge request. Server only. Deployment is in `deploy/HOSTING.md`.
- **`apps/shared`**: token estimation and the wire contracts (`dakcoder_shared/contract/rest.py`, `envelope.py`).
- **`gotools/`**: the Go sidecar, an MCP server. It provides the contract linter (`rules_lint`), legacy audit, scaffolders, `fx_wire`, `repo_map`, the migration call map (`handler_map`, `unit_check`, `impact`) and the knowledge-base generator. `gotools/README.md` documents it in depth, including how to add a rule or change a scaffold template.
- **`extension/`**: the VS Code extension (TypeScript). It spawns the runtime, renders the chat webview (`media/chat/`), and owns approvals, the migration wizard and the diagnostics.

`new-template/` (the reference template) and `pao-back-end-development/` (a real legacy service) are test corpora. Tests that need them skip when they are absent.

## Commands

Python runs from the repo root without installing anything: `pytest.ini` puts every `apps/*/src` on the path. If a `.venv` exists, use its interpreter (`.venv/Scripts/python.exe`); otherwise the system `python` (3.13, with pytest and pytest-asyncio installed) works from the repo root. `make` may not be installed, so each Makefile target is a thin wrapper over the commands below.

```bash
# Python
python -m pytest apps -q -m "not slow"          # fast suite (make test-fast)
python -m pytest apps -q                        # everything, incl. the ~30s end-to-end run
python -m pytest apps/agent/tests/test_migration.py -q -k "phase_closes"   # one test

# Go sidecar (from gotools/)
make ci            # fmt-check, vet, tidy-check, test-race, lint, doc-check, catalog + knowledge checks
make test          # or: go test ./...
make test-short    # no Go toolchain / private modules needed
make golden        # rewrite scaffold snapshots, then read the diff

# Extension (from extension/)
npm run typecheck
npm test                      # node --test unit suites
npm run verify                # contract check, typecheck, tests, build, credential/command/l10n/gotools checks
node scripts/extract-l10n.mjs # regenerate l10n/bundle.l10n.json after adding vscode.l10n.t strings

# Release
python scripts/release.py 0.4.11   # bumps all four versions, rebuilds gotools binaries + wheels, tests, packages the VSIX
```

`python scripts/debug.py latest` replays a recorded session turn by turn. Recording is written to `<workspace>/.dakcoder/sessions/<id>/debug.jsonl` when `dakcoder.debugRecording` or `DAKCODER_DEBUG=1` is on.

## Generated files: never hand-edit, regenerate

Each generated file has a drift check that fails CI:

| File | Regenerate | Source of truth |
|---|---|---|
| `api/tool-catalog.json`, `api/TOOL-CATALOG.md` (contract C1) | `make catalog` | `apps/agent/.../tools/registry.py` `_SPECS` |
| `api/openapi.json`, `api/contract.json`, `api/agent-card.json`, `extension/src/contract.gen.ts` | `make contract` | `apps/shared/.../contract/` |
| `api/contract-baseline.json` | `scripts/contract-baseline.py` (run by release) | the last release; **the contract may only grow** |
| `packages/knowledge/`, `apps/agent/.../knowledge/` | `make knowledge` | Go code in `gotools/internal/kb/`, built from `new-template`'s skill.md and SOP.md |
| `gotools/docs/tool-catalog.json` | `make -C gotools tool-catalog` | the Go MCP server |

Without `make`, the catalog regeneration is the Python one-liner in the root `Makefile` (`catalog:` target).

## Agent architecture: the parts that span files

**One run** (`loop.py`, ~7k lines, `AgentLoop._run`) works like this:

- It picks an intent: ASK, PLANNER or AGENT (`modes.py`). A given intent wins, then facts: an answer to `ask_developer`, work in flight, an active migration. The classifier call comes last.
- It then drives turns: `_turn` → `llm.complete` → `_tool_calls` → `Router.dispatch`.
- A run ends only through a tool call. The terminal tools are `finish`, `submit_plan` and `ask_developer`; prose alone never ends a phase.
- AGENT runs finish through `_verify` → `gate.full_gate` (go build, vet, `rules_lint`, govalid, tidy). The gate is scoped to `router.touched` and charged only against a baseline taken before the first edit. `gate.inner_loop` (gofmt + lint) runs after each edit batch.

**Plan steps** are typed (`tools/control.py` `PlanStep`). Their status is set by the loop from the change set, never by the model. A write marks a step `written`, and `_verify_written` promotes it to `done`.

**Follow-ups:** each developer message builds a new `AgentLoop`. `Loopback._spawn` (`loopback.py`) reuses the session's context and calls `carry_from(previous)`. `_starts_new_task` / `_begin_new_task` decide whether a follow-up is a new task (reset the fields named in `_TASK_SCOPED`, the pinned task and `router.touched`) or a continuation. A new field on the loop state must be classified as task-scoped or session-scoped, or it will leak across tasks.

**Context** (`context.py` `ContextManager`) is the only thing that assembles messages. The layer order is fixed: `system → project (AGENTS.md) → mode → task → recap → working set → plan & directives & state block`.

- Everything above the working set is pinned and survives compaction.
- The head is kept **byte-stable for the prefix cache**. Anything that changes per turn goes in the last (volatile) block.
- `_note_prefix` reports a cache break by name.
- `messages.Message` is frozen on purpose.

**Prompt budget is enforced by tests** in `test_prompts.py`:

- `system.md` must stay at or under 1,200 tokens.
- Each mode overlay (`prompts/modes/*.md`) must stay at or under 250.
- `PREFIX_CEILING` caps system prompt + tool schemas per mode.

Adding or growing a tool schema trips it. Trim first; if you must raise the ceiling, write the reason in the comment block above it, as the existing entries do.

**Adding a tool:**
1. Add a `ToolSpec` to `_SPECS` in `tools/registry.py`. Its checks run at import: snake_case name, description of 200 characters or fewer ending in ".", at most 6 params.
2. Add a handler to the module's `HANDLERS` and merge it in `serve.py`.
3. Name path arguments `path`/`paths`, or they skip workspace confinement and the protected-path approval check (`dakcoder_shared.paths.PROTECTED_GLOBS`).
4. Regenerate the catalog.
5. A gotools-backed tool also needs the Go MCP registration and the `catalog_test.go` want-list.

**Migration** (`migration.py` plus the migration paths in `loop.py`) is a phased roadmap:

- Only a task that starts with `/migration` (`MIGRATION_COMMAND`) starts, resumes or restarts one. The classifier, a plan with `phases`, and resume or restart wording don't. The extension's `/migration` and its `/migrate` alias add the word, and the runtime strips it before the model sees the task.

- The gate is deferred until the last phase closes.
- Files over `BIG_FILE` lines are split into one step per `handler_map` group.
- A split step advances only when `unit_check` confirms its methods are converted.
- State persists per workspace in `.dakcoder/migration/state.json`, which is what a new `/migration` session resumes from. `.dakcoder/migration/plan.md` is the human-readable view, and its shape is a contract with the extension's Migration view.
- The per-session `.dakcoder/sessions/<id>/plan.json` carries only the REST-contract shape.

**AGENTS.md** (`tools/agents_md.py`) is loaded into the project layer each run. Sessions edit only the fenced `dakcoder:notes` section, via `update_agents_md` or `finish`'s `remember` field.

## Conventions in this codebase

- **Comments explain why.** Most carry the field incident (a session id or BUG id) that motivated them. Match that density and keep the history when you change a decision.
- **Every refusal names a working alternative.** Tool results use a `fix=` or the spec's `instead`. A condition the model cannot satisfy must be bounded, because an unbounded push-back is a recurring failure mode here.
- **Best-effort persistence.** Journal, plan record, progress files and hooks swallow their own errors and never fail a run.
- **Shared contracts are additive only.** Unknown event types and fields must be ignored by clients. REST shapes in `contract/rest.py` are strict: extra fields fail `test_contract.py`.
- **Extension conventions.**
  - User-facing strings go through `vscode.l10n.t('literal')`.
  - Prompts sent to the model are deliberately *not* localised.
  - Switches on tool names always have a default arm.
- **Test style.** Tests are named as sentences describing the behaviour. `apps/agent/tests/scripted.py` provides `ScriptedClient` and the `build`/`calls` helpers for driving whole loops. Loops built with `AgentLoop.__new__` in tests skip `__init__`, so new attributes read in helper paths need a safe default.
