# dakcoder

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
