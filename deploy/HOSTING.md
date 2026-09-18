# Hosting dakcoder on this server

This runbook turns the local deployment in [README.md](README.md) into a hosted
one. Hosted means other people, a portal and other agents can run dakcoder
against a GitLab repository **on this server** and get a merge request back.
Everything here is on the server at `/mnt/data/raghav/dakcoder`.

README.md stays the base: how the gateway, runtime, Postgres, Redis and LiteLLM
fit together, and the redeploy command. This file adds, in order:

| Stage | What it gives you | Needs |
|---|---|---|
| [0](#stage-0--update-the-code) | the new code, installed | nothing |
| [1](#stage-1--check-the-runtime-holds-no-secrets) | confirms the runtime no longer holds the gateway's secrets | stage 0 |
| [2](#stage-2--real-sign-in-with-gitlab) | GitLab sign-in, published | a GitLab OAuth application |
| [3](#stage-3--the-control-plane-and-its-runners) | hosted runs: workspaces, isolated runners, merge requests | stage 2, sudo, a GitLab service account |
| [4](#stage-4--other-agents-and-machine-callers-optional) | the agent card, A2A, machine clients | stage 3 |
| [5](#stage-5--browser-front-ends-optional) | CORS for a browser portal | stage 3 |

Each stage leaves the server working. Stop after any of them.

Two paths are used throughout:

```bash
ROOT=/mnt/data/raghav/dakcoder        # the checkout
DATA=/mnt/data/raghav/dakcoder-data   # hosted state; outside the checkout, so no git command touches it
```

---

## Stage 0: update the code

The hosting work is on the `host/phase-0` branch until it is merged. After the
merge, stay on `main` and skip the checkout.

```bash
cd /mnt/data/raghav/dakcoder
git fetch origin
git checkout host/phase-0 && git pull
uv pip install --python .venv/bin/python \
  -e apps/shared -e apps/gateway -e apps/agent -e apps/agentsvc pyyaml
```

Then redeploy with the usual command:

```bash
setsid bash -c 'cd /mnt/data/raghav/dakcoder && \
  deploy/build-gotools.sh && deploy/stop.sh && deploy/start.sh' \
  < /dev/null > deploy/logs/redeploy.log 2>&1 &
tail -f deploy/logs/redeploy.log      # Ctrl-C once it prints the status table
```

Nothing hosted is switched on yet. `deploy/status.sh` should look the same as
before.

## Stage 1: check the runtime holds no secrets

`start.sh` used to start the runtime holding the whole env file, including
`DAKCODER_JWT_SECRET`. With that secret, any code the runtime builds can sign a
token for any user. The launcher now strips it, along with the other gateway
and control-plane secrets. Check the running process:

```bash
for pid in $(pgrep -f 'bin/dakcoderd --workspace'); do
  echo "dakcoderd $pid:"
  tr '\0' '\n' < /proc/$pid/environ \
    | grep -oE '^DAKCODER_(JWT_SECRET|POSTGRES_DSN|REDIS_URL|GITLAB_CLIENT_SECRET|GITLAB_SERVICE_TOKEN|AGENTSVC_TOKEN)=' \
    || echo "  clean"
done
```

Every line should say `clean`.

**Rotate the signing secret if the old runtime ever built code you did not
write.** It held the secret in its environment, and anything it ran could read
it.

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(48))"
# paste it as DAKCODER_JWT_SECRET= in deploy/dakcoder.env, then redeploy
```

Every token minted so far stops working, including the ones you gave
developers. Mint new ones (`.venv/bin/python deploy/gateway_main.py --mint
dev:<user> --mint-hours 12`), or wait for stage 2, after which developers sign
in themselves.

> **What stripping does not cover.** The standing runtime (the tmux window
> `runtime`) runs as your user, so it can still *read* `deploy/dakcoder.env`
> from disk. Stripping keeps the secrets out of its environment, not off the
> disk. Point it only at code you trust. Untrusted code goes through the Docker
> runners of stage 3, which can see nothing but their own workspace.

## Stage 2: real sign-in with GitLab

Until now the gateway has used the dev identity provider, which accepts any
authorization code. That is why sign-in is blocked in nginx, and why tokens are
minted by hand. Stage 3 refuses to start with it.

### 2.1 Register the OAuth application

In GitLab, either **Admin Area → Applications** (instance-wide) or **your group
→ Settings → Applications**, choose **Add new application**:

| Field | Value |
|---|---|
| Name | `dakcoder` |
| Redirect URI (one per line) | `vscode://dop.dakcoder-go/auth/callback`<br>`vscode-insiders://dop.dakcoder-go/auth/callback`<br>`http://127.0.0.1/callback` |
| Confidential | ticked (the gateway keeps the secret) |
| Scopes | `openid`, `profile`, `email`, `read_api` |

The first two are desktop VS Code and VS Code Insiders. The third is VS Code
over Remote-SSH or in a browser, where the extension listens on a random
loopback port. GitLab matches loopback redirect URIs on any port (RFC 8252), so
one entry covers every port.

Save it, and copy the **Application ID** and the **Secret**.

### 2.2 Point the gateway at it

In `deploy/dakcoder.env`, fill in the three lines that are already there
commented out:

```bash
DAKCODER_GITLAB_URL=https://gitlab.cept.gov.in
DAKCODER_GITLAB_CLIENT_ID=<Application ID>
DAKCODER_GITLAB_CLIENT_SECRET=<Secret>
```

Redeploy, then check:

```bash
curl -s localhost:8790/v1/health \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["capabilities"]["identity"])'
# gitlab
```

If it still says `dev`, one of the three is empty.

### 2.3 Publish sign-in

In `deploy/nginx-dakcoder.conf`, delete the `location ~ ^/dakcoder/v1/auth/`
block and the long comment above it. **Keep** the `location =
/dakcoder/v1/auth/delegate` block under it: that route is for the control plane
on loopback and is never published.

Make the edit in the repository and push it, rather than only on the server.
Otherwise the next `git pull` conflicts with it. Then:

```bash
deploy/install-nginx.sh            # dry run; should report identity: gitlab
deploy/install-nginx.sh --apply

curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'content-type: application/json' \
  -d '{}' https://ai.cept.gov.in/dakcoder/v1/auth/start
# 400: the gateway answered (the body was empty on purpose). 403: the block is still there.
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://ai.cept.gov.in/dakcoder/v1/auth/delegate
# 404, always
```

Developers can now run **dakcoder: Sign In**. Tokens you minted before keep
working until they expire.

A signed-in person's identity (their `sub`) is `gitlab:<user id>`. The numeric
user ID is on their GitLab profile page. You need it in stage 3 to restrict a
repository to named people.

## Stage 3: the control plane and its runners

This is the hosted side. **agentsvc**, the control plane on `127.0.0.1:8792`,
does four things:

1. **Leases workspaces.** A workspace is a clone of an allowlisted repository.
2. **Runs one runner per workspace.** A runner is a `dakcoderd` in a locked-down
   container.
3. **Runs sessions one at a time on each workspace.**
4. **Delivers.** Delivering pushes the session's `dakcoder/<session>` branch and
   opens a merge request.

The gateway fronts it: callers use `https://ai.cept.gov.in/dakcoder/v1/runtime/v1/…`
with their own token, and the gateway forwards each request with the caller's
identity attached. Nobody reaches agentsvc or a runner directly.

What a runner gets:

- **Filesystem:** a read-only root, and no Linux capabilities. It runs as your
  uid, never root.
- **Writable mounts:** only its workspace, plus a 512 MB `/tmp`.
- **Module cache:** a shared Go module cache, mounted read-only.
- **Network:** it can reach the gateway's second listener, and nothing else: not
  the internet, not Redis/Postgres/LiteLLM, not another runner.
- **Model token:** it calls the model with a token minted for the workspace's
  owner, scoped to model traffic only. Quota and the ledger charge the person
  whose run it is.

### 3.1 A GitLab service account

Until clones can act as the caller, one service account clones, pushes and
opens merge requests, and only for repositories you list.

The simplest option is a **group access token**. In the group, go to
**Settings → Access tokens → Add new token**:

- **Role:** `Developer` (enough to push `dakcoder/*` branches and open merge
  requests into protected ones).
- **Scopes:** `api`, `read_repository`, `write_repository`.
- **Expiry:** put the date in your calendar. When it passes, clones and
  deliveries fail with 401.

A bot user with a personal access token (same scopes, Developer on each
project) works too.

### 3.2 Data directories and the allowlist

```bash
mkdir -p $DATA/agentsvc $DATA/gomodcache
chmod 700 $DATA
```

Create `$DATA/allowlist.json`. Only repositories listed here can be leased:

```json
[
  {
    "repo": "https://gitlab.cept.gov.in/it-2.0/backend/some-service",
    "owner": "<who answers for this entry>",
    "expires": "2026-12-31",
    "subs": ["*"]
  }
]
```

| Field | Meaning |
|---|---|
| `repo` | The URL callers lease. Case of the host, a trailing `/` and `.git` do not matter. |
| `owner` | A named person. Required: an entry nobody owns never gets removed. |
| `expires` | The last day it can be leased. Existing workspaces are unaffected. |
| `subs` | Who may lease it: `"*"` for anyone signed in, or a list such as `["gitlab:123", "gitlab:456"]`. |

agentsvc reads the file when it starts. After editing it, restart agentsvc
([Day to day](#day-to-day)).

### 3.3 Settings

Append this to `deploy/dakcoder.env`. It is a shell file, so the second line
can refer to the first.

```bash
# -- hosted: the control plane (deploy/HOSTING.md stage 3) --------------------
DAKCODER_AGENTSVC_TOKEN=<python3 -c "import secrets;print(secrets.token_urlsafe(48))">
DAKCODER_RUNTIME_TOKEN=${DAKCODER_AGENTSVC_TOKEN}      # what the gateway presents to it
DAKCODER_RUNTIME_URL=http://127.0.0.1:8792             # the gateway fronts agentsvc
DAKCODER_AGENTSVC_DATA=/mnt/data/raghav/dakcoder-data/agentsvc
DAKCODER_REPO_ALLOWLIST=/mnt/data/raghav/dakcoder-data/allowlist.json
DAKCODER_GITLAB_SERVICE_TOKEN=<the token from 3.1>

DAKCODER_RUNNER_BACKEND=docker
DAKCODER_RUNNER_IMAGE=dakcoder-runner:current
DAKCODER_RUNNER_NETWORK=dakcoder-runners
DAKCODER_RUNNER_GOMODCACHE=/mnt/data/raghav/dakcoder-data/gomodcache
# A container cannot reach the host's loopback. The gateway also listens on the
# runners' bridge, and runners are given that address.
DAKCODER_GATEWAY_RUNNER_LISTEN=172.30.0.1:8790
DAKCODER_RUNNER_GATEWAY_URL=http://172.30.0.1:8790
```

The limits all have defaults. Set any of these only to change them:

| Variable | Default | |
|---|---|---|
| `DAKCODER_MAX_LEASES_PER_USER` | 3 | workspaces one person may hold |
| `DAKCODER_MAX_RUNNING_PER_USER` | 2 | runs one person may have going |
| `DAKCODER_MAX_LEASE_BYTES` | 4 GiB | a bigger clone is refused |
| `DAKCODER_LEASE_TTL_HOURS` | 168 | a workspace is retired this long after it was leased |
| `DAKCODER_RUNNER_IDLE_HOURS` | 0.5 | an idle runner is stopped (never mid-run) |
| `DAKCODER_HOSTED_APPROVAL_TIMEOUT` | 1800 | seconds an approval waits before the run suspends |
| `DAKCODER_RUNNER_CPUS` / `_MEMORY` / `_PIDS` | 2 / 4g / 512 | per runner |

### 3.4 The runners' network and firewall

```bash
sudo deploy/runner-network.sh --apply
```

This reads the address from `DAKCODER_GATEWAY_RUNNER_LISTEN` and does two
things:

1. **Creates the Docker network** `dakcoder-runners`: `172.30.0.0/24` on a
   bridge called `br-dakcoder`, with container-to-container traffic off.
2. **Adds iptables rules** tagged `dakcoder-runners`. From the bridge, a runner
   can reach `172.30.0.1:8790` and nothing else. From anywhere else, nothing
   can reach `172.30.0.1`.

If something on the host already uses `172.30.0.0/24`, the script stops and
says so. Change the address in both variables and run it again. Run it without
`--apply` at any time to see what is in place. It changes nothing.

Firewall rules are lost on reboot. Re-apply them after Docker starts:

```bash
sudo tee /etc/systemd/system/dakcoder-runner-network.service >/dev/null <<'EOF'
[Unit]
Description=dakcoder runners' network and firewall (deploy/HOSTING.md)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/mnt/data/raghav/dakcoder/deploy/runner-network.sh --apply

[Install]
WantedBy=docker.service
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now dakcoder-runner-network
```

If the host runs ufw or firewalld, check with `sudo iptables -S INPUT | head`
that the `dakcoder-runners` rules come first.

### 3.5 The runner image

```bash
deploy/build-runner.sh
docker image ls dakcoder-runner
```

This builds the two wheels from this checkout, rebuilds `gotools`, builds
`dakcoder-runner:<commit>`, and points `dakcoder-runner:current` at it. The
build needs the network (base image, apt, pip, `go install`). The proxy
variables in your shell are passed through. A runner runs its image, not this
checkout, so run this again whenever `apps/agent`, `apps/shared` or `gotools`
change.

### 3.6 Fill the module cache

Runners have no network, so they cannot download Go modules. Each listed
repository's modules must already be in the shared cache. A missing one fails
at once with `module lookup disabled by GOPROXY=off`.

```bash
. deploy/shellenv.sh                       # go on PATH, GOPRIVATE for internal modules
export GOMODCACHE=$DATA/gomodcache

warm() {  # repository URL, branch
  local tmp; tmp="$(mktemp -d)"
  git clone -q --depth 1 --branch "$2" "$1" "$tmp" \
    && (cd "$tmp" && go mod download && go list -deps -test ./... >/dev/null) \
    && echo "warmed $1"
  rm -rf "$tmp"
}

warm https://gitlab.cept.gov.in/it-2.0/backend/some-service.git main
```

Clone with your own credentials: this only reads `go.mod`. Internal modules
are fetched the same way the standing runtime fetches them. Repeat this for
each allowlisted repository, and again when a repository gains dependencies.
Go writes the cache read-only. To empty it, run `go clean -modcache` with
`GOMODCACHE` set, not `rm -rf`.

### 3.7 Start it

```bash
deploy/stop.sh && deploy/start.sh
```

`start.sh` now also:

- checks that the bridge address exists and that the runner image is built,
  stopping with a pointer back here if not;
- mints the control plane's own gateway token into `deploy/logs/agentsvc-jwt`,
  with the `delegate` scope and a 30-day lifetime;
- starts agentsvc in a fourth tmux window, `agentsvc`.

Check it:

```bash
deploy/status.sh                          # agentsvc (control plane): up; runner containers: 0
sudo deploy/runner-network.sh             # gateway: listening on 172.30.0.1:8790
```

Then check the fence from inside a container on the runners' network. Each
target is an address, not a name, so a DNS failure cannot pass for a blocked
connection:

```bash
fence() {  # label URL
  docker run --rm --network dakcoder-runners --entrypoint curl dakcoder-runner:current \
    -k -s -o /dev/null -m 5 -w "$1: %{http_code}\n" "$2"
}
fence gateway http://172.30.0.1:8790/v1/health                      # gateway: 200
fence nginx   "https://$(hostname -I | cut -d' ' -f1)/"               # nginx: 000
fence litellm "http://$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' mlops-litellm):4000/"   # litellm: 000
fence proxy   "${HTTPS_PROXY:-$https_proxy}"                         # proxy: 000
```

The first is the one thing a runner may reach. The others are the host's own
nginx, another container, and the corporate proxy, which is the way to the
internet. Anything other than `200` followed by three `000`s means the rules
are not in force. Run `sudo deploy/runner-network.sh` and read what it reports.

### 3.8 End to end, by hand

This drives the whole thing through the gateway, as a portal would. It uses a
throwaway identity, `test:smoke`, which the `"*"` in the allowlist admits.

```bash
. deploy/shellenv.sh
T=$(.venv/bin/python deploy/gateway_main.py --mint test:smoke --mint-hours 1)
H=(-H "Authorization: Bearer $T" -H 'content-type: application/json')
G=http://127.0.0.1:8790/v1/runtime/v1
field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

# 1. Lease a listed repository. This clones it; a big one takes a minute.
W=$(curl -s "${H[@]}" $G/workspaces \
      -d '{"repo_url":"https://gitlab.cept.gov.in/it-2.0/backend/some-service","ref":"main"}' \
    | tee /dev/stderr | field '["id"]')

# 2. Start a run. The first one on a workspace starts its runner container.
S=$(curl -s "${H[@]}" $G/workspaces/$W/tasks \
      -d '{"task":"Add a short section to README.md on how to run the tests.","intent":"agent"}' \
    | tee /dev/stderr | field '["id"]')

# 3. Watch it. Ctrl-C stops watching; the run carries on.
curl -sN "${H[@]}" $G/sessions/$S/events

# 4. Approvals waiting on you, and answering one.
curl -s "${H[@]}" $G/approvals
curl -s "${H[@]}" -X POST $G/approvals/<approval id> -d '{"decision":"accept"}'

# 5. How it ended.
curl -s "${H[@]}" $G/sessions/$S | python3 -m json.tool | head -40

# 6. Deliver: pushes dakcoder/<session> and opens a REAL merge request.
curl -s "${H[@]}" -X POST $G/sessions/$S/deliver -d '{"title":"dakcoder smoke test"}'

# 7. Release the workspace. Its sessions are archived, and its clone deleted.
curl -s "${H[@]}" -X DELETE $G/workspaces/$W
```

Close the merge request and delete its branch in GitLab afterwards. The run's
model usage appears in the ledger under `test:smoke`.

While it runs, `docker ps --filter name=dakcoder-runner-` shows the runner, and
`docker logs -f dakcoder-runner-$W` shows its output.

## Stage 4: other agents and machine callers (optional)

### The agent card and A2A

```bash
# deploy/dakcoder.env
DAKCODER_PUBLIC_URL=https://ai.cept.gov.in/dakcoder
```

Redeploy. The card describes dakcoder to other agents and points them at the
A2A endpoint:

```bash
curl -s https://ai.cept.gov.in/dakcoder/.well-known/agent-card.json | python3 -m json.tool
```

Other agents send JSON-RPC (`message/send`, `message/stream`, `tasks/get`,
`tasks/cancel`) to `https://ai.cept.gov.in/dakcoder/v1/a2a`. They need a token
with the `a2a` scope. A person's token has every scope. A machine's token has
the scopes it is registered with (next section).

### Machine callers

A portal's backend or another agent cannot sign in through a browser. Register
it for the client-credentials grant.

**1. Make a secret, and keep only its hash.**

```bash
SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))")
echo "$SECRET"                                 # hand this to the client's owner, once
printf %s "$SECRET" | sha256sum | cut -d' ' -f1
```

**2. List the client** in `$DATA/clients.json`:

```json
[
  {
    "client_id": "portal-backend",
    "secret_sha256": "<the sha256 above>",
    "scopes": ["sessions:read", "sessions:write", "workspaces:write", "a2a"],
    "owner": "<who answers for this client>"
  }
]
```

These are the scopes:

| Scope | Allows |
|---|---|
| `sessions:read` | sessions, their events and transcripts |
| `sessions:write` | start runs, send messages, answer approvals, stop runs |
| `workspaces:write` | lease and release workspaces |
| `agenda:write` | the shared agenda |
| `deliver:mr` | push a branch and open a merge request |
| `a2a` | call dakcoder as another agent |
| `llm` | the model proxy |

`delegate` cannot be given to a client.

**3. Point the gateway at the file**, then redeploy:

```bash
DAKCODER_CLIENTS=/mnt/data/raghav/dakcoder-data/clients.json   # in deploy/dakcoder.env
```

**4. The client gets a token** (15 minutes long) like this:

```bash
curl -s https://ai.cept.gov.in/dakcoder/v1/auth/token \
  -d grant_type=client_credentials -d client_id=portal-backend -d client_secret="$SECRET"
```

That route is published only once stage 2.3 has removed the 403 block. A
machine's identity is `client:<client_id>`, which the allowlist's `"*"` admits.
To name it in `subs` instead, add `"client:portal-backend"`.

To revoke a client, delete its entry and redeploy. Tokens it already holds
expire within 15 minutes.

## Stage 5: browser front ends (optional)

A portal that calls the gateway from the browser needs its origin allowed:

```bash
DAKCODER_CORS_ORIGINS=https://portal.example.gov.in    # comma-separated; '*' is refused
```

Redeploy.

---

## Day to day

**Redeploying** is the command from stage 0. Note what it does and does not
cover:

- **Interruptions.** Restarting the gateway cuts every stream in flight.
  Stopping agentsvc stops every runner, which ends the runs they carried: they
  show as `error`, and can be resumed. Redeploy between runs when you can.
- **The runner image.** A redeploy does not rebuild it. After a pull, run
  `git diff --stat HEAD@{1} -- apps/agent apps/shared gotools`. If that shows
  anything, run `deploy/build-runner.sh`. New runners use the new image, and
  idle ones are replaced as the reaper stops them.
- **Monthly minimum.** Redeploy at least every 30 days. The control plane's
  gateway token is minted at start and lasts 720 hours. When it lapses, new
  runners cannot get a model token, and runs fail at their first model call.

**Restart only agentsvc**, for example after editing the allowlist. Run
`tmux attach -t dakcoder`, switch to the `agentsvc` window (`Ctrl-b w`), press
`Ctrl-C` and wait for the prompt, then press `↑` and `Enter`. It stops its
runners on the way out.

**Logs:**

| What | Where |
|---|---|
| gateway, runtime, agentsvc | `deploy/logs/{gateway,runtime,agentsvc}.log` |
| one runner | `docker logs dakcoder-runner-<workspace id>` (gone once it stops) |
| reaper | `agentsvc.log`, lines starting `reaper:` |

**What the reaper does** (every 60 s):

- stops runners idle for `DAKCODER_RUNNER_IDLE_HOURS`, never mid-run;
- retires workspaces older than `DAKCODER_LEASE_TTL_HOURS`, archiving their
  `.dakcoder/sessions` first;
- refreshes the model tokens of runners that are still working.

**Where hosted state lives:**

| Path | What |
|---|---|
| `$DATA/agentsvc/registry.sqlite3` | workspaces, sessions, deliveries |
| `$DATA/agentsvc/leases/<id>/mirror.git` | the control plane's clone (credentials, pushes). Never mounted into a runner. |
| `$DATA/agentsvc/leases/<id>/repo` | the runner's working copy, including `.dakcoder/sessions` |
| `$DATA/agentsvc/archive/` | sessions of retired and released workspaces |
| `$DATA/gomodcache` | the shared, read-only module cache |

**Backups.** The registry and the archive are the only state you cannot
rebuild. Workspaces can be leased again.

```bash
mkdir -p $DATA/backup
.venv/bin/python -c "import sqlite3,sys; sqlite3.connect(sys.argv[1]).backup(sqlite3.connect(sys.argv[2]))" \
  $DATA/agentsvc/registry.sqlite3 $DATA/backup/registry-$(date +%F).sqlite3
tar -C $DATA/agentsvc -czf $DATA/backup/archive-$(date +%F).tgz archive
```

**Disk usage:** `du -sh $DATA/agentsvc/leases/* | sort -h | tail`.

## Turning it off

**Hosting only.** Comment out `DAKCODER_AGENTSVC_TOKEN`,
`DAKCODER_RUNTIME_URL`, `DAKCODER_RUNTIME_TOKEN` and
`DAKCODER_GATEWAY_RUNNER_LISTEN`, then redeploy. The hosted routes answer 404,
and local use by developers is unaffected. `$DATA` is left alone.

**The network too:**

```bash
sudo systemctl disable --now dakcoder-runner-network
sudo deploy/runner-network.sh --remove
docker network rm dakcoder-runners
```

**The code.** Run `git checkout <the commit you were on>` and redeploy.

## Troubleshooting

| Symptom | Cause, and what to do |
|---|---|
| `start.sh`: `no interface has 172.30.0.1` | The runners' network is missing. Run `sudo deploy/runner-network.sh --apply`. |
| `start.sh`: `runner image … is not built` | Run `deploy/build-runner.sh`. |
| gateway: `refusing DAKCODER_GATEWAY_RUNNER_LISTEN with the dev identity provider` | Stage 2 is not done. Health must say `identity: gitlab`. |
| lease answers 403 `not available to lease here` | Not in the allowlist, past `expires`, or the caller's `sub` is not in `subs`. Also check the URL spelling. agentsvc reads the file at start, so restart it after edits. |
| lease answers 502 `could not be cloned` | Check the service token: its expiry, its role on that project, and its scopes. The detail is in `agentsvc.log`. |
| lease answers 413 | The clone is bigger than `DAKCODER_MAX_LEASE_BYTES`. |
| 429 on lease or task | The per-person limits in 3.3. |
| task answers 409 | A run is already going on that workspace. Runs on one workspace go one at a time; lease it twice to run two. |
| runner starts, then the run fails at its first model call | Check the model token. `deploy/logs/agentsvc-jwt` is minted at start and lasts 30 days, so redeploy. If that is not it, the runner cannot reach the gateway: run `fence gateway …` from 3.7. |
| a delivery answers 502 `the branch was pushed but the merge request failed` | Check the service token's `api` scope, and that `DAKCODER_GITLAB_URL` is set. The branch is on GitLab, so you can open the merge request by hand. |
| `docker run failed: … container name … is already in use` | A leftover from an agentsvc that died. `start.sh` and `stop.sh` clear these; by hand, run `docker rm -f dakcoder-runner-<id>`. |
| run fails: `module lookup disabled by GOPROXY=off` | The module cache lacks something. Warm that repository again (3.6). |
| run fails: `go.mod requires go >= 1.xx … GOTOOLCHAIN=local` | The repository wants a newer Go than the image has. Run `GO_IMAGE=golang:1.xx deploy/build-runner.sh`, and move the host's toolchain to match (`deploy/install-go.sh`). |
| run fails: `no space left on device` under `/tmp` | The build cache outgrew the runner's 512 MB `/tmp`. It is set in `runners.py` (`run_argv`). |
| sign-in: GitLab says `The redirect URI included is not valid` | The URI list in 2.1 does not match exactly. Remote-SSH and browser VS Code use `http://127.0.0.1/callback`. |
| a delivery answers 409 about the session's outcome | The run did not end `done`. Deliver anyway by adding `"override": "<why>"` to the body. The reason is recorded on the merge request. |
