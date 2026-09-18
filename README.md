# dakcoder

Usage

python scripts/release.py 0.3.7

You choose the version; it's validated as major.minor.patch (VS Code rejects anything else). Written in Python rather than shell because scripts/ is already Python by convention, and it runs identically from PowerShell or Git Bash.

Flags: --skip-tests, --full-tests, --no-isolation (offline wheel builds), --keep-tracked-vsix, --allow-dirty.

What it does
Preflight — checks node/npm/go/git, extension/node*modules, and picks an interpreter that actually has build (prefers .venv, falls back to the current one).
Bumps all four declarations — extension/package.json plus the three pyproject.tomls. Each regex is anchored to the top-level key; I verified each matches exactly once, so nested "version" keys deeper in package.json can't be hit. Line endings are preserved.
Rebuilds the gotools binaries — 4 platforms, stamped with the new version, checksum manifest regenerated.
Rebuilds both wheels — deletes every old dakcoder_agent-*/dakcoder*shared-* wheel first, then builds shared before agent.
Python tests — fast suite by default.
npm run package — typecheck, extension tests, esbuild, credential/command/l10n/checksum checks, then the VSIX.
Verifies the artifacts — the step that catches the bug I hit manually yesterday.
Updates .gitignore.

python -m build --wheel --outdir extension/runtime apps/shared
python -m build --wheel --outdir extension/runtime apps/agent
cd extension && npm run package

The IT 2.0 backend coding agent for Go services on `n-api-template`.

Turns _"add a `Pension` resource with CRUD and a status filter"_ into a
compiling, FX-wired, swagger-visible set of Go files — verified by the compiler
and a static template linter before a human sees the diff.

**Start here: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — what is built,
and the decision log explaining why each part is the way it is.

---

## Layout

```
gotools/            the Go analysis and scaffolding sidecar — built
  cmd/gotools/        CLI: lint · legacy-audit · scaffolders · repo-map ·
                      doc-check · tool-catalog · knowledge · mcp
  internal/           workspace · rules · spec · scaffold · fxwire · gopatch ·
                      repomap · kb · catalog · naming · mcpserver
  docs/               TOOL-CATALOG.md (contract C1) · DIVERGENCES.md

apps/
  shared/             dakcoder-shared — token estimation, contracts
  agent/              dakcoder-agent — context manager, mode config
  gateway/            dakcoder-gateway — not started (server only, never
                      in the .vsix; see ARCHITECTURE D-36)

packages/knowledge/ the agent's knowledge base — generated, do not edit
api/                generated contracts, each with a drift check:
                      TOOL-CATALOG.md  C1, model-facing tools   `make catalog`
                      contract.json    API version, C2 events,  `make contract`
                                       REST routes (the extension's
                                       contract.gen.ts is built from it)
                      openapi.json     every request and       `make contract`
                                       response shape
docs/               ARCHITECTURE.md
plan*.md            the programme plan: shared context, Part A, Part B

new-template/               the reference template — the contract, and the
                            corpus every rule is held against
pao-back-end-development/   a real pre-template service — the legacy corpus
```

Both corpora are inputs to the test suites. Tests that need them skip cleanly
when they are absent.

## Running things

```bash
# Go sidecar
cd gotools
make ci                # fmt · vet · tidy · race tests · lint · the three contract checks
make baseline          # the corpus assertions, by hand
make scaffold-demo     # scaffold a resource into a throwaway copy and lint it

# Python spine
python -m pytest apps -q
```

`gotools/README.md` covers the sidecar in detail — what it checks, what it
writes, and the design notes behind both.

### Watching a run turn by turn

Recording captures, for every turn, the exact request sent, the loop's own state
in its five groups, the context accounting, and the raw response — into
`.dakcoder/sessions/<id>/debug.jsonl` in the workspace, beside the transcript
and the plan.

**Turn it on.** You do not start the runtime yourself; the extension spawns it,
and it inherits the environment VS Code was launched with — so an environment
variable set in a shell reaches it only if VS Code was started from that shell.
Use the setting instead:

> Settings → `dakcoder.debugRecording` → on, then **Reload Window**.

`DAKCODER_DEBUG=1` still works for anyone starting `dakcoderd` by hand.

**Read it back**, from the workspace you ran against:

```bash
python scripts/debug.py                     # which sessions have a recording
python scripts/debug.py latest              # the newest one, turn by turn
python scripts/debug.py latest --prompt 14  # the exact request sent on turn 14
python scripts/debug.py <id> -C /path/to/workspace
```

`scripts/debug.py` is the wrapper for a checkout, where `dakcoder_agent` is not
installed; inside the runtime's own venv the module is `python -m
dakcoder_agent.debug` with the same arguments.

Off by default, one `is None` check per seam when off. Cheap when on because the
prompt is prefix-stable by design: turn 1 stores the assembled request whole and
every turn after stores only the messages that changed, so replay is exact
rather than approximate. `apps/agent/src/dakcoder_agent/debug.py` has the
reasoning.

## What is built

All of it: the agent loop and tool router (`apps/agent`), the gateway — auth,
quota, ledger and model proxy (`apps/gateway`), the shared contracts
(`apps/shared`), the Go sidecar (`gotools/`) and the VS Code extension
(`extension/`). This section used to say those four were "not built yet", which
was true when it was written and had been wrong for some time; an audit is a poor
way to find out what your own README claims.

`ARCHITECTURE_AUDIT.md` is the current map, `AUDIT.md` and `BUGS.md` the known
defects, `CHANGE_PLAN.md` the order they are being fixed in, and `task.md` what
has actually landed.
