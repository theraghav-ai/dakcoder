#!/usr/bin/env bash
# The runners' network: a private bridge from which a runner container reaches
# the gateway's second listener and nothing else. Not the internet, not the
# host's other services (Redis, Postgres, LiteLLM), not another runner.
#
# A runner builds and tests repository code, and that code is untrusted. The
# runtime declining to run `curl` is guidance to a model, not a control; this is
# the control (host-plan §7.2). See deploy/HOSTING.md.
#
#   sudo deploy/runner-network.sh            show what is in place, change nothing
#   sudo deploy/runner-network.sh --apply    create the network, (re)apply the rules
#   sudo deploy/runner-network.sh --remove   take the rules out (the network stays)
#
# Idempotent. The address comes from DAKCODER_GATEWAY_RUNNER_LISTEN in
# deploy/dakcoder.env (read, not sourced: this runs as root), so the gateway's
# listener and these rules cannot disagree. Firewall rules do not survive a
# reboot on their own; HOSTING.md has a systemd unit that runs --apply after
# docker starts.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy/dakcoder.env"
BRIDGE=br-dakcoder
TAG=dakcoder-runners
MODE="${1:---check}"

[[ $EUID -eq 0 ]] || { echo "needs root: sudo $0 $MODE"; exit 1; }
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE"; exit 1; }

env_value() {  # NAME -> its value in dakcoder.env, without a trailing comment or quotes
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1 \
    | sed -e 's/[[:space:]]#.*$//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

LISTEN="$(env_value DAKCODER_GATEWAY_RUNNER_LISTEN)"
if [[ ! "$LISTEN" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.([0-9]+):([0-9]+)$ ]]; then
  echo "set DAKCODER_GATEWAY_RUNNER_LISTEN=<bridge address>:<port> in $ENV_FILE first,"
  echo "e.g. 172.30.0.1:8790 (deploy/HOSTING.md)"
  exit 1
fi
BRIDGE_IP="${BASH_REMATCH[1]}.${BASH_REMATCH[2]}"
SUBNET="${BASH_REMATCH[1]}.0/24"
PORT="${BASH_REMATCH[3]}"
NETWORK="$(env_value DAKCODER_RUNNER_NETWORK)"
NETWORK="${NETWORK:-dakcoder-runners}"

C="-m comment --comment $TAG"
# chain|rule, in the order they must be evaluated. INPUT is what a runner can
# reach on this host: replies to connections the host opened (agentsvc talking
# to its runners), the gateway's listener, and nothing else. The last INPUT
# rule is the other direction: the bridge address is for runners, and a
# neighbour routing to it would otherwise reach the gateway around nginx.
# DOCKER-USER is everything forwarded: off the host, and to another container.
RULES=(
  "INPUT|-i $BRIDGE -m conntrack --ctstate ESTABLISHED,RELATED $C -j ACCEPT"
  "INPUT|-i $BRIDGE -d $BRIDGE_IP -p tcp --dport $PORT $C -j ACCEPT"
  "INPUT|-i $BRIDGE $C -j REJECT"
  "INPUT|! -i $BRIDGE -d $BRIDGE_IP $C -j REJECT"
  "DOCKER-USER|-i $BRIDGE $C -j REJECT"
)

ours() {
  local chain
  for chain in INPUT DOCKER-USER; do
    iptables -S "$chain" 2>/dev/null | grep -E -- "--comment \"?$TAG\"?( |$)" || true
  done
}

remove_rules() {
  local spec parts
  while read -r spec; do
    [[ -n "$spec" ]] || continue
    read -ra parts <<<"${spec//\"/}"
    iptables -D "${parts[@]:1}"  # `-A CHAIN rule...` as printed, deleted as `-D CHAIN rule...`
  done < <(ours)
}

apply_rules() {
  local entry chain rule
  local -A next=([INPUT]=1 [DOCKER-USER]=1)
  iptables -S DOCKER-USER >/dev/null 2>&1 \
    || { echo "no DOCKER-USER chain: is docker running?"; exit 1; }
  remove_rules
  for entry in "${RULES[@]}"; do
    chain="${entry%%|*}"
    rule="${entry#*|}"
    # shellcheck disable=SC2086  # the rule is words, split on purpose
    iptables -I "$chain" "${next[$chain]}" $rule
    next[$chain]=$((next[$chain] + 1))
  done
}

ensure_network() {
  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    local gateway bridge
    gateway="$(docker network inspect -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' "$NETWORK")"
    bridge="$(docker network inspect -f '{{index .Options "com.docker.network.bridge.name"}}' "$NETWORK")"
    if [[ "$gateway" != "$BRIDGE_IP" || "$bridge" != "$BRIDGE" ]]; then
      echo "!! network $NETWORK exists with gateway '$gateway' on bridge '$bridge';"
      echo "   expected $BRIDGE_IP on $BRIDGE. Remove it (docker network rm $NETWORK) and re-run."
      exit 1
    fi
    return
  fi
  if ip -4 route | grep -qF "${SUBNET%.0/24}."; then
    echo "!! something on this host already routes ${SUBNET}: pick another address"
    echo "   for DAKCODER_GATEWAY_RUNNER_LISTEN and DAKCODER_RUNNER_GATEWAY_URL"
    exit 1
  fi
  docker network create --driver bridge \
    --subnet "$SUBNET" --gateway "$BRIDGE_IP" \
    --opt com.docker.network.bridge.name="$BRIDGE" \
    --opt com.docker.network.bridge.enable_icc=false \
    "$NETWORK" >/dev/null
  echo "==> created network $NETWORK ($SUBNET on $BRIDGE)"
}

show() {
  echo "network  $NETWORK"
  if docker network inspect "$NETWORK" >/dev/null 2>&1; then
    echo "  present: $(ip -4 -o addr show dev "$BRIDGE" 2>/dev/null | awk '{print $4}') on $BRIDGE"
  else
    echo "  MISSING (--apply creates it)"
  fi
  echo "rules"
  local found
  found="$(ours)"
  if [[ -n "$found" ]]; then sed 's/^/  /' <<<"$found"; else echo "  none (--apply adds them)"; fi
  echo "gateway"
  if ss -ltn 2>/dev/null | grep -qF " $BRIDGE_IP:$PORT "; then
    echo "  listening on $BRIDGE_IP:$PORT"
  else
    echo "  not listening on $BRIDGE_IP:$PORT (deploy/start.sh starts it)"
  fi
}

case "$MODE" in
  --check) show ;;
  --apply) ensure_network; apply_rules; show ;;
  --remove) remove_rules; show ;;
  *) echo "usage: sudo $0 [--check|--apply|--remove]"; exit 2 ;;
esac
