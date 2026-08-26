#!/usr/bin/env bash
# Put a Go toolchain in $DAKCODER_HOME (default ~/.local/share/dakcoder).
#
# There is no system Go on this host and no root to install one, and the agent
# cannot verify anything it writes without `go build`, `go vet`, `go test` and
# `gofmt`. Lifted out of the official image rather than downloaded, because the
# image is already how gotools is built and go.dev is not reachable from here.
#
# Outside the repository on purpose: toolchain plus module cache is ~20k files,
# and a working tree that size is slow to search and watch whatever .gitignore
# says.
set -euo pipefail
DAKCODER_HOME="${DAKCODER_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/dakcoder}"
IMAGE="${GO_IMAGE:-golang:1.25}"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE"
# The module cache is written read-only by Go, so a plain rm refuses.
[ -d "$DAKCODER_HOME/go" ] && chmod -R u+w "$DAKCODER_HOME/go"
rm -rf "$DAKCODER_HOME/go"
mkdir -p "$DAKCODER_HOME"
cid="$(docker create "$IMAGE")"
trap 'docker rm "$cid" >/dev/null' EXIT
docker cp "$cid:/usr/local/go" "$DAKCODER_HOME/go" >/dev/null
"$DAKCODER_HOME/go/bin/go" version
echo "installed in $DAKCODER_HOME/go"
