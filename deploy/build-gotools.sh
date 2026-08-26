#!/usr/bin/env bash
# Build the Go sidecar. There is no Go toolchain on this host, so it is built in
# a container and the binary — statically linked, as Go builds are without cgo —
# runs on the host afterwards. The module cache is a named volume, so a rebuild
# does not re-download the world.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo local)"
docker run --rm \
  ${HTTP_PROXY:+-e HTTP_PROXY="$HTTP_PROXY"} ${HTTPS_PROXY:+-e HTTPS_PROXY="$HTTPS_PROXY"} \
  -v "$ROOT/gotools":/src -v dakcoder-gomod:/go/pkg/mod -w /src \
  golang:1.25-alpine \
  go build -trimpath -ldflags "-s -w -X main.Version=$VERSION" -o gotools ./cmd/gotools
"$ROOT/gotools/gotools" version
