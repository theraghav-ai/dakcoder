#!/usr/bin/env bash
# Stop what start.sh started. Redis and LiteLLM are not ours and are left running.
set -uo pipefail
SESSION="${DAKCODER_TMUX_SESSION:-dakcoder}"
tmux kill-session -t "$SESSION" 2>/dev/null && echo "tmux session '$SESSION' killed"
[[ "${1:-}" == "--all" ]] && docker stop dakcoder-postgres >/dev/null && echo "postgres stopped"
exit 0
