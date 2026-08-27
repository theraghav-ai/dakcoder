# Sourced by every pane in the tmux session, and usable by hand:
#   . deploy/shellenv.sh
#
# One file rather than a long `tmux send-keys` line, because quoting a shell
# command through tmux through bash is how PATH ends up containing the literal
# string "$PATH" — which happened, and which looks like a broken install rather
# than a quoting bug.
_dak_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; . "$_dak_root/deploy/dakcoder.env"; set +a

# The corporate proxy must never sit between us and a loopback service.
export no_proxy="localhost,127.0.0.1,::1,${no_proxy:-}"
export NO_PROXY="$no_proxy"

# The Go sidecar the agent spawns over MCP.
#
# GOTOOLS_PATH is checked before PATH by `_find_binary`, and it is the name the
# extension hands the child (§4.6): inside a real install the sidecar ships under
# a platform-suffixed name in the .vsix and PATH holds no entry for it at all.
# Setting it here makes this standing runtime resolve the sidecar exactly the
# way a spawned one does, rather than through a fallback that only works because
# this happens to be a source checkout.
export PATH="$_dak_root/gotools:$PATH"
[ -x "$_dak_root/gotools/gotools" ] && export GOTOOLS_PATH="$_dak_root/gotools/gotools"

# What the extension stamps on the child. Nothing in the runtime reads it today;
# it is set so this process and a spawned one differ in nothing that matters.
export DAKCODER_MODE=local

# The Go toolchain. The agent's verification gates are `go build`, `go vet`,
# `go test` and `gofmt` — without these on PATH the agent can still write code
# and cannot check any of it, which is the one thing it exists to do. There is
# no system Go on this host, so a private one is unpacked.
#
# It lives *outside* the repository. A toolchain plus a module cache is roughly
# twenty thousand files; inside the working tree that is twenty thousand files
# every editor, watcher and search indexes, and .gitignore does not help with
# any of those — it only hides them from git. Generated things belong outside
# the tree, not merely ignored within it.
export DAKCODER_HOME="${DAKCODER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/dakcoder}"
if [ -x "$DAKCODER_HOME/go/bin/go" ]; then
  export GOROOT="$DAKCODER_HOME/go"
  export GOPATH="${GOPATH:-$DAKCODER_HOME/gopath}"
  export PATH="$GOROOT/bin:$GOPATH/bin:$PATH"
  # Internal modules live on gitlab.cept.gov.in and must not be fetched through
  # the public proxy or checked against the public sum database.
  export GOPRIVATE="${GOPRIVATE:-gitlab.cept.gov.in/*}"
  export GONOSUMDB="$GOPRIVATE" GONOSUMCHECK=1
fi

# A minted access token, if start.sh has left one. Not in dakcoder.env because
# it expires and that file does not.
[ -r "$_dak_root/deploy/logs/jwt" ] && export DAKCODER_JWT="$(cat "$_dak_root/deploy/logs/jwt")"
unset _dak_root
