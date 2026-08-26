#!/usr/bin/env bash
# Bring up dakcoder's server side in one tmux session.
#
#   dakcoder-postgres   the usage ledger          (docker, 127.0.0.1:55432)
#   dakmithra_redis     the quota counters        (docker, 127.0.0.1:6379, pre-existing)
#   mlops-litellm       the model endpoint        (docker, 127.0.0.1:4000, pre-existing)
#   gateway             identity, quota, ledger, model proxy   127.0.0.1:8790
#   runtime             dakcoderd — what the extension spawns  127.0.0.1:8791
#
# Redis and LiteLLM are already running on this host and are left alone; this
# script starts what is missing and nothing else.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SESSION="${DAKCODER_TMUX_SESSION:-dakcoder}"
ENV_FILE="$ROOT/deploy/dakcoder.env"
PY="$ROOT/.venv/bin/python"

[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE"; exit 1; }
[[ -x "$PY" ]]       || { echo "missing venv at $ROOT/.venv — see deploy/README.md"; exit 1; }

set -a; . "$ENV_FILE"; set +a

# The corporate proxy must never sit between us and a loopback service. The
# Python clients set trust_env=False, but curl and the tooling in these panes do
# not, and a proxied health check fails in a way that looks like a dead server.
export no_proxy="localhost,127.0.0.1,::1,${no_proxy:-}"
export NO_PROXY="$no_proxy"
export PATH="$ROOT/gotools:$PATH"

mkdir -p "$ROOT/deploy/logs"

# -- dependencies ------------------------------------------------------------

wait_for() {  # name url
  for _ in $(seq 1 60); do
    curl -s -o /dev/null -m 2 "$2" && return 0
    sleep 1
  done
  echo "!! $1 did not come up at $2"; return 1
}

if ! docker ps --format '{{.Names}}' | grep -qx dakcoder-postgres; then
  echo "==> starting Postgres (the usage ledger)"
  docker start dakcoder-postgres >/dev/null 2>&1 || docker run -d \
    --name dakcoder-postgres --restart unless-stopped \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=dakcoder -e POSTGRES_DB=dakcoder \
    -p 127.0.0.1:55432:5432 -v dakcoder-pgdata:/var/lib/postgresql/data \
    postgres:16-alpine >/dev/null
  until docker exec dakcoder-postgres pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
fi
# Idempotent: the DDL is CREATE ... IF NOT EXISTS, and the agent never applies it.
"$PY" -c 'from dakcoder_gateway.ledger import SCHEMA; print(SCHEMA)' \
  | docker exec -i -e PGOPTIONS=--client-min-messages=warning dakcoder-postgres \
      psql -q -U postgres -d dakcoder >/dev/null

docker exec dakmithra_redis redis-cli ping >/dev/null \
  || { echo "!! Redis (dakmithra_redis) is not answering"; exit 1; }

[[ -x "$ROOT/gotools/gotools" ]] \
  || { echo "!! gotools is not built — run deploy/build-gotools.sh"; exit 1; }

# The agent's gates are `go build`, `go vet`, `go test` and `gofmt`. An agent
# that can write Go and not compile it is the failure mode this whole project
# exists to avoid, so a missing toolchain stops the launch rather than being
# discovered on the first verification turn.
DAKCODER_HOME="${DAKCODER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/dakcoder}"
[[ -x "$DAKCODER_HOME/go/bin/go" ]] \
  || { echo "!! the Go toolchain is missing — run deploy/install-go.sh"; exit 1; }

# -- the tmux session --------------------------------------------------------

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "==> tmux session '$SESSION' already exists; killing it"
  tmux kill-session -t "$SESSION"
fi

# The runtime is a *local* deployment and refuses to start holding a model key
# (§4.7). `env -u` is that rule enforced by the launcher rather than trusted.
RUNTIME_ENV="env -u DAKCODER_MODEL_API_KEY -u OPENAI_API_KEY -u LITELLM_API_KEY -u ANTHROPIC_API_KEY"

tmux new-session -d -s "$SESSION" -n gateway -c "$ROOT"
tmux send-keys -t "$SESSION:gateway" \
  ". deploy/shellenv.sh && $PY deploy/gateway_main.py 2>&1 | tee -a deploy/logs/gateway.log" C-m

wait_for gateway "http://127.0.0.1:${DAKCODER_GATEWAY_PORT}/v1/health"

# A 12-hour token, because nothing here refreshes one. In the real deployment
# the extension holds a 15-minute token and rotates it; a long-lived token is a
# hosting convenience and is why the dev IdP is loopback-only.
"$PY" "$ROOT/deploy/gateway_main.py" --mint "dev:${DAKCODER_DEV_USER}" --mint-hours 12 \
  > "$ROOT/deploy/logs/jwt"
chmod 600 "$ROOT/deploy/logs/jwt"

tmux new-window -t "$SESSION" -n runtime -c "$ROOT"
tmux send-keys -t "$SESSION:runtime" \
  ". deploy/shellenv.sh && $RUNTIME_ENV .venv/bin/dakcoderd --workspace \"\$DAKCODER_WORKSPACE\" --port \"\$DAKCODER_RUNTIME_PORT\" 2>&1 | tee -a deploy/logs/runtime.log" C-m

wait_for runtime "http://127.0.0.1:${DAKCODER_RUNTIME_PORT}/v1/health"

tmux new-window -t "$SESSION" -n shell -c "$ROOT"
tmux send-keys -t "$SESSION:shell" \
  ". deploy/shellenv.sh && clear && deploy/status.sh" C-m

echo
echo "==> up. tmux attach -t $SESSION"
"$ROOT/deploy/status.sh"
