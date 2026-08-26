# Hosting dakcoder on this machine

Everything the server side needs, and one command that brings it up.

```bash
deploy/start.sh          # bring it all up in a tmux session called "dakcoder"
tmux attach -t dakcoder  # gateway · runtime · a shell with the env loaded
deploy/status.sh         # what is up, and where
deploy/stop.sh           # kill the session (--all also stops Postgres)
```

Nothing in here is imported by `apps/`. The gateway package deliberately ships
no `main` — `create_app` takes a fully-wired `Gateway` because the wiring is a
deployment decision, not a library one (ARCHITECTURE D-36) — and this directory
is that decision for this host.

## What runs

| Component | Where | Started by |
|---|---|---|
| LiteLLM — the model endpoint | `127.0.0.1:4000` | already running (`mlops-litellm`) |
| Redis — quota counters | `127.0.0.1:6379` db 3 | already running (`dakmithra_redis`) |
| Postgres — the usage ledger | `127.0.0.1:55432/dakcoder` | `start.sh` (`dakcoder-postgres`) |
| **gateway** — identity, quota, ledger, model proxy | `127.0.0.1:8790` | `start.sh`, tmux window `gateway` |
| **dakcoderd** — the runtime the extension spawns | `127.0.0.1:8791` | `start.sh`, tmux window `runtime` |
| `gotools` — the Go sidecar, over MCP on stdio | `gotools/gotools` | `build-gotools.sh`, spawned per session |
| Go toolchain — the agent's verification gates | `$DAKCODER_HOME/go` | `install-go.sh` |

Redis and LiteLLM were already up and are left alone. The ledger gets its own
Postgres rather than sharing LiteLLM's: they are separate systems of record, and
LiteLLM's spend tables are a cross-check on ours, not a home for them (§16.6).

## First-time setup

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e apps/shared -e apps/gateway -e apps/agent pyyaml
deploy/install-go.sh        # no system Go on this host
deploy/build-gotools.sh     # builds in a container, runs on the host
deploy/start.sh
```

Generated things live **outside** the tree, in
`$DAKCODER_HOME` (default `~/.local/share/dakcoder`): the Go toolchain and its
module cache are about twenty thousand files, and a working tree that size is
slow to search and watch no matter what `.gitignore` says. The only exceptions
are `.venv/` and the `gotools` binary, which are where every tool expects them.

`deploy/dakcoder.env` holds the secrets and is gitignored; copy
`deploy/dakcoder.env.example` and fill in the three blanks. `deploy/shellenv.sh`
is what every tmux pane sources, and is worth sourcing by hand too — it sets
`PATH` for `go`/`gotools`, keeps the corporate proxy away from loopback, and
exports the minted `DAKCODER_JWT`.

## Two things this deployment does that production must not

**The identity provider is a local stand-in.** No GitLab OAuth application is
configured on this host, so the gateway runs `DevIdentity`, which accepts any
authorization code and returns a fixed developer. It refuses to start off
loopback, and `/v1/health` reports `identity: "dev"` so the fact is visible
rather than assumed. Setting `DAKCODER_GITLAB_URL`, `_CLIENT_ID` and
`_CLIENT_SECRET` switches to the real adapter with no other change.

**The runtime holds a 12-hour token.** Access tokens are 15 minutes by design,
and rotating them is the extension's job (§15.2); nothing here does that, so
`start.sh` mints a long one with `gateway_main.py --mint`. That is also why the
dev IdP is loopback-only — the two decisions are the same decision.

The credential invariant is *not* relaxed. The gateway is the only process
holding the LiteLLM key; `start.sh` strips every model-credential variable from
the runtime's environment before spawning it (`env -u …`), and `local_config`
refuses to build a configuration that has one (§4.7).

## Verifying it works

```bash
. deploy/shellenv.sh
curl -s localhost:8790/v1/health | python3 -m json.tool     # capability probe
curl -s localhost:8791/v1/health -H "Authorization: Bearer $DAKCODER_GATEWAY_TOKEN"
docker exec dakcoder-postgres psql -U postgres -d dakcoder \
  -c 'select sub, mode, billed_tokens, latency_ms from usage_events order by id desc limit 5;'
docker exec dakmithra_redis redis-cli -n 3 keys 'q:*'
```

A turn through the proxy should leave one row in `usage_events` and move the
Redis counters — reservation, then reconcile against the usage chunk.

## Publishing it at https://ai.cept.gov.in/dakcoder/

```bash
deploy/install-nginx.sh            # show what would change
deploy/install-nginx.sh --apply    # copy the snippet, add one include, test, reload
deploy/install-nginx.sh --remove   # back it out
```

The snippet is [nginx-dakcoder.conf](nginx-dakcoder.conf). It goes to
`/etc/nginx/snippets/dakcoder.conf`; `api.conf` gains exactly one `include` line
inside the 443 server block, is backed up first, and is restored if `nginx -t`
fails — a config that does not parse takes down every other service on that
vhost.

Two things it does deliberately:

- **`proxy_buffering off` and a 660s read timeout.** The model proxy is SSE.
  Buffered, it becomes a slow non-streaming endpoint with no error to explain
  it; timed out at nginx's 60s default, a long coder turn is truncated rather
  than reported.
- **`/dakcoder/v1/auth/` returns 403.** The dev identity provider accepts any
  authorization code — published, that is an open door onto the shared GPU
  budget. Everything else is published and still requires a dakcoder JWT. Mint
  one on the host with `deploy/gateway_main.py --mint dev:<user> --mint-hours 12`.
  Configure GitLab OAuth, confirm `/dakcoder/v1/health` reports
  `"identity": "gitlab"`, then remove that block.

## Pointing the VS Code extension at it

```jsonc
"dakcoder.gatewayUrl": "http://127.0.0.1:8790",
"dakcoder.pythonPath": "/mnt/data/raghav/dakcoder/.venv/bin/python",
"dakcoder.goPath":     "~/.local/share/dakcoder/go/bin/go",
"dakcoder.gotoolsPath":"/mnt/data/raghav/dakcoder/gotools/gotools"
```

The extension spawns its own `dakcoderd`; the `runtime` window here is a
standing one for driving the loopback API by hand.
