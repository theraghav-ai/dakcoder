#!/usr/bin/env bash
# What is up, and what each thing is for. Read-only.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; . "$ROOT/deploy/dakcoder.env"; set +a
export no_proxy="localhost,127.0.0.1,::1" NO_PROXY="localhost,127.0.0.1,::1"

row() { printf '  %-10s %-28s %s\n' "$1" "$2" "$3"; }
probe() { curl -s -o /dev/null -w '%{http_code}' -m 3 "$1" 2>/dev/null || echo '---'; }

echo "dakcoder — local deployment"
echo
printf '  %-10s %-28s %s\n' STATUS COMPONENT WHERE
docker exec dakmithra_redis redis-cli ping >/dev/null 2>&1 \
  && row up "redis (quota counters)" "127.0.0.1:6379 db 3" \
  || row DOWN "redis (quota counters)" "127.0.0.1:6379"
docker exec dakcoder-postgres pg_isready -U postgres >/dev/null 2>&1 \
  && row up "postgres (usage ledger)" "127.0.0.1:55432/dakcoder" \
  || row DOWN "postgres (usage ledger)" "127.0.0.1:55432"
[[ "$(probe "$DAKCODER_MODEL_BASE_URL/models")" != "---" ]] \
  && row up "litellm (model endpoint)" "$DAKCODER_MODEL_BASE_URL" \
  || row DOWN "litellm (model endpoint)" "$DAKCODER_MODEL_BASE_URL"
[[ "$(probe "http://127.0.0.1:$DAKCODER_GATEWAY_PORT/v1/health")" == "200" ]] \
  && row up "gateway" "http://127.0.0.1:$DAKCODER_GATEWAY_PORT" \
  || row DOWN "gateway" "http://127.0.0.1:$DAKCODER_GATEWAY_PORT"
[[ "$(probe "http://127.0.0.1:$DAKCODER_RUNTIME_PORT/v1/health")" =~ ^(200|401)$ ]] \
  && row up "dakcoderd (runtime)" "http://127.0.0.1:$DAKCODER_RUNTIME_PORT" \
  || row DOWN "dakcoderd (runtime)" "http://127.0.0.1:$DAKCODER_RUNTIME_PORT"
[[ -x "$ROOT/gotools/gotools" ]] \
  && row built "gotools (Go sidecar)" "$ROOT/gotools/gotools" \
  || row MISSING "gotools (Go sidecar)" "deploy/build-gotools.sh"
echo
echo "  capability probe:"
curl -s -m 3 "http://127.0.0.1:$DAKCODER_GATEWAY_PORT/v1/health" \
  | "$ROOT/deploy/render-probe.py"
echo
echo "  tmux attach -t ${DAKCODER_TMUX_SESSION:-dakcoder}   ·   logs in deploy/logs/"
