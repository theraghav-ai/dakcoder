# Hosting dakcoder on this machine

to mint the gateway token

cd /mnt/data/raghav/dakcoder && . deploy/shellenv.sh
.venv/bin/python deploy/gateway_main.py --mint dev:localdev --mint-hours 500

to deploy everything in server

cd /mnt/data/raghav/dakcoder
git pull
setsid bash -c 'cd /mnt/data/raghav/dakcoder && \
  deploy/build-gotools.sh && deploy/stop.sh && deploy/start.sh' \
  < /dev/null > deploy/logs/redeploy.log 2>&1 &



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

## Choosing a model per role

Which model answers as the Planner, the Coder, the summariser and the rest is
an operator's decision, taken in `deploy/dakcoder.env` and applied by
restarting the gateway. There is no code change and no redeploy: the runtime
sends a *role* name and the gateway is the only side that resolves it to a
model — which is also the control that stops a developer routing to a model
nobody budgeted for (§15.4).

Three defaults cover the common case, one endpoint serving everything:

```bash
DAKCODER_MODEL_BASE_URL=http://127.0.0.1:4000/v1
DAKCODER_MODEL_API_KEY=sk-…
DAKCODER_MODEL=Qwen3.8-27B
```

Any role can then name its own model, its own endpoint and its own key, and
inherits whatever it does not name:

```bash
DAKCODER_MODEL_PLANNER=Qwen3-235B-A22B            # a bigger model to plan with
DAKCODER_MODEL_PLANNER_BASE_URL=http://10.0.0.9:4000/v1   # …on another host
DAKCODER_MODEL_PLANNER_API_KEY=sk-…               # …with its own credential
DAKCODER_MODEL_SUMMARISER=Phi-4-mini-instruct     # a small one for recaps
```

| role | where it is used |
| --- | --- |
| `planner` | Plan mode turns |
| `coder` | Agent mode turns — reading, writing, running the gates |
| `ask` | Ask mode turns, which hold no write tool |
| `fast` | the intent classifier: one call, a two-key schema, 64 tokens |
| `summariser` | compaction recaps |
| `embed` | the embeddings endpoint |
| `verifier`, `debugger` | retired as modes, kept as roles so an older runtime or a stored session naming one is routed rather than refused |

`DAKCODER_MODEL_ROLES=reviewer` adds a role beyond these; it is additive, so
the built-in ones are always configured — the runtime calls `fast` and
`summariser` on its own and a table missing them would fail the first
compaction of the day.

Two things make a change here verifiable rather than something you infer from
behaviour, which is the whole reason it is configuration:

- **`GET /v1/models`** (authenticated) publishes the table in force — role,
  model, endpoint, and which of the three the role overrode. Never the keys.
  `/v1/health` publishes role → model only, since it is unauthenticated.
- **The capability probe runs against every distinct endpoint**, not just the
  default. A second endpoint that has stopped sending the usage chunk or
  rejects `chat_template_kwargs` shows up as a named failure at startup, which
  is what §4.5 exists for; probing only the default would leave every role
  pointed elsewhere unchecked.

A role left without a key anywhere stops the gateway at startup, naming the
role and the variable. The alternative is a 502 hours later, for whichever
developer happens to reach that role first.

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

The credential invariant is *not* relaxed, and per-role keys do not relax it
either — they widen the *shape* that has to be kept off a laptop, not the set
of processes that may hold one. The gateway is still the only process holding a
model key; `start.sh` strips every `DAKCODER_MODEL*_API_KEY` in the environment
before spawning the runtime (matched by shape, not from a list that would go
out of date the first time a role was added), the extension does the same at
spawn, and `local_config` refuses to build a configuration that has one (§4.7).

## Measuring whether the context window is big enough

Three records, in increasing order of detail. All of them are already being
written; none of them costs a model call to read.

**1. One line per run, in `deploy/logs/runtime.log`.** Enough to notice that a
run was shaped by the window rather than by the task:

```
2026-09-03T10:00:51 INFO dakcoder_agent.loop  run 9f2c unverified in 180 turn(s):
  peak prompt 233000/235520 tokens (89% of the window), 5 compaction(s) discarding
  755000 tokens, 1 truncation(s), 40 file(s) evicted then re-read,
  3 read(s) refused as already held, 2600000 bytes of source read
```

`DAKCODER_LOG_LEVEL=debug` in `deploy/dakcoder.env` turns on per-turn detail.
The default is `info`, which is this line and the warnings around it.

**2. The report, across every run a workspace has journalled.**

```bash
.venv/bin/python scripts/context-report.py --workspace /path/to/the/repo
.venv/bin/python scripts/context-report.py --workspace /path/to/the/repo --json
```

Reads `.dakcoder/sessions/*/events.jsonl` — written by the runtime as it goes,
so this works on runs that have already happened, including ones recorded before
the accounting existed. It separates two things a claim has to keep apart:

* **pressure** — a compaction fired, or a reply was cut off. Real, but a
  threshold can be moved and a budget can be retuned, so on its own it argues
  about tuning rather than about the window.
* **loss** — a file was evicted and then read again, or a read was refused
  because the content was already held. That is the evidence: a window large
  enough for the task produces none of it at any threshold.

The last section is the one that settles it. It totals the *unique source* each
run had to read and puts it against the prompt budget, so a task whose files
alone exceed the window is visible as arithmetic rather than as an argument.
It counts only `read_file` bytes — not the system prompt, the tool schemas, the
plan, the assistant messages or any other tool result — so it is a floor on what
the task needed.

**3. The gateway ledger, in Postgres, which is the billing-grade record.**
Every metered turn, with the endpoint's own token counts rather than the agent's
estimate:

```sql
-- per session: how big the prompts got, and how much was cache
SELECT session_id, mode, count(*) AS turns,
       max(prompt_tokens) AS peak_prompt, sum(prompt_tokens) AS total_prompt,
       sum(completion_tokens) AS completion, sum(reasoning_tokens) AS reasoning,
       round(100.0 * sum(cached_tokens) / nullif(sum(prompt_tokens), 0), 1) AS cache_pct
FROM usage_events GROUP BY session_id, mode ORDER BY peak_prompt DESC LIMIT 20;

-- how close the prompts get to the 262,144-token window
SELECT width_bucket(prompt_tokens, 0, 262144, 10) * 26214 AS bucket_ceiling,
       count(*) AS turns
FROM usage_events GROUP BY 1 ORDER BY 1;

-- the agent's estimate against the endpoint's truth, which is what the
-- prompt budget is enforced with
SELECT round(avg(estimated_tokens::numeric / nullif(prompt_tokens, 0)), 3) AS mean_ratio,
       min(estimated_tokens::numeric / nullif(prompt_tokens, 0)) AS worst_under,
       max(estimated_tokens::numeric / nullif(prompt_tokens, 0)) AS worst_over
FROM usage_events WHERE prompt_tokens > 0;
```

That last query is also how to decide whether `OUTPUT_RESERVE` in
`apps/agent/src/dakcoder_agent/modes.py` is bigger than it needs to be: it is
sized for estimator error, and this measures the error.

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

**Off the host**, over the published URL, nothing needs configuring: since
v0.1.0 the extension's `dakcoder.serverGatewayUrl` already defaults to
`https://ai.cept.gov.in/dakcoder`.

What *is* needed is a credential, because this host does not publish sign-in.
Mint one and hand it over:

```bash
deploy/gateway_main.py --mint dev:<user> --mint-hours 12
```

The developer runs **dakcoder: Enter Gateway Token** and pastes it. The
extension checks it against `/v1/quota` before storing it in the OS keychain, so
a token that will not work is refused at the prompt rather than at the first
turn. **dakcoder: Sign In** reaches the same place — it catches the 403 from
`/v1/auth/start`, shows the reason, and offers the token box.

Two consequences of a minted token, both by design:

- **It does not refresh.** There is no `/v1/auth/refresh` to call here, so the
  extension does not schedule one. When the twelve hours are up the developer is
  asked for another, at the moment the old one actually stops working.
- **Doctor says so.** `/v1/health` reports `identity: "dev"`, and the
  *Identity provider* row turns that into a warning with an "Enter a token"
  button. Once GitLab OAuth is configured and health reports `identity:
  "gitlab"`, that row goes green on its own and sign-in works normally — delete
  the 403 block from `nginx-dakcoder.conf` at the same time.

**On the host**, driving the loopback API directly:

```jsonc
"dakcoder.gatewayUrl": "http://127.0.0.1:8790",
"dakcoder.pythonPath": "/mnt/data/raghav/dakcoder/.venv/bin/python",
"dakcoder.goPath":     "~/.local/share/dakcoder/go/bin/go",
"dakcoder.gotoolsPath":"/mnt/data/raghav/dakcoder/gotools/gotools"
```

`dakcoder.gatewayUrl` is the override and wins over `serverGatewayUrl`.

The extension spawns its own `dakcoderd`; the `runtime` window here is a
standing one for driving the loopback API by hand.
