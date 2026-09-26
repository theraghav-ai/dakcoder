#!/usr/bin/env bash
# Stop what start.sh started. Redis and LiteLLM are not ours and are left running.
set -uo pipefail
SESSION="${DAKCODER_TMUX_SESSION:-dakcoder}"

# The control plane stops its runners on the way out, but only when asked to
# leave. Killing the session hangs it up instead, and its runner containers
# would outlive it: each one holds its workspace's name, and the next
# `docker run --name` for that workspace fails. So ask first, and wait.
if tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -qx agentsvc; then
  tmux send-keys -t "$SESSION:agentsvc" C-c
  for _ in $(seq 1 60); do
    pgrep -f 'bin/dakcoder-agentsvc' >/dev/null || break
    sleep 1
  done
fi
tmux kill-session -t "$SESSION" 2>/dev/null && echo "tmux session '$SESSION' killed"
# Whatever is left belongs to a control plane that died rather than stopped.
_runners="$(docker ps -aq --filter name=dakcoder-runner- 2>/dev/null)"
[[ -n "$_runners" ]] && docker rm -f $_runners >/dev/null && echo "runner containers removed"
# The same for process runners (DAKCODER_RUNNER_BACKEND=process): one that
# outlives its control plane keeps serving the code it was started with, so a
# redeploy that leaves it running has not redeployed the agent.
pkill -f 'bin/dakcoderd --hosted' && echo "runner processes stopped"
[[ "${1:-}" == "--all" ]] && docker stop dakcoder-postgres >/dev/null && echo "postgres stopped"
exit 0
