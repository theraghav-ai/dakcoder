#!/usr/bin/env bash
# Build the runner image from this checkout (deploy/runner/Dockerfile).
#
#   deploy/build-runner.sh     build dakcoder-runner:<commit>, and point
#                              dakcoder-runner:current at it
#
# A runner runs what is in its image, not what is in this checkout: an agent
# change reaches hosted runs only once this has been run. Runners started after
# it use the new image; one already running keeps its own until it stops (idle,
# or agentsvc restarting).
#
# The build needs the network (the base image, apt, pip, `go install`). This
# shell's proxy variables are passed to it, and GO_IMAGE if set.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TAG="$(git rev-parse --short HEAD)"
git diff --quiet HEAD -- apps/shared apps/agent gotools deploy/runner || TAG="$TAG-dirty"

echo "==> wheels"
rm -f extension/runtime/dakcoder_shared-*.whl extension/runtime/dakcoder_agent-*.whl
if command -v uv >/dev/null 2>&1; then
  uv build --wheel --out-dir extension/runtime apps/shared
  uv build --wheel --out-dir extension/runtime apps/agent
else
  .venv/bin/python -m build --wheel --outdir extension/runtime apps/shared
  .venv/bin/python -m build --wheel --outdir extension/runtime apps/agent
fi

echo "==> gotools"
deploy/build-gotools.sh

echo "==> dakcoder-runner:$TAG"
args=()
for name in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy GO_IMAGE; do
  if [[ -n "${!name:-}" ]]; then
    args+=(--build-arg "$name")  # the value comes from this environment
  fi
done
# BuildKit, for deploy/runner/Dockerfile.dockerignore: without it the whole
# repository is the build context.
DOCKER_BUILDKIT=1 docker build -f deploy/runner/Dockerfile -t "dakcoder-runner:$TAG" \
  ${args[@]+"${args[@]}"} .
docker tag "dakcoder-runner:$TAG" dakcoder-runner:current
echo "==> dakcoder-runner:current is now dakcoder-runner:$TAG"
