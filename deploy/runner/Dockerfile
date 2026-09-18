# The runner image: one hosted dakcoderd per workspace lease (host-plan §7.2).
#
#   deploy/build-runner.sh     the wheels, the sidecar, then this
#
# Built from the repository root, after the shared and agent wheels are in
# extension/runtime and the sidecar is built; Dockerfile.dockerignore keeps the
# rest of the repository out of the context. The control plane runs it
# read-only, as its own uid (the owner of the lease's working copy; never root),
# with no capabilities, and with that working copy the only writable mount
# besides /tmp (runners.DockerBackend.run_argv is the whole command).
#
# The Go toolchain is the same image deploy/install-go.sh lifts the host's from,
# so a hosted runner and a local runtime build with the same Go (§7.3). The
# linters are pinned by build argument; keep them at the versions the host's
# `/v1/health` toolchain block reports, or the two will disagree about a gate.
ARG GO_IMAGE=golang:1.25
FROM ${GO_IMAGE}

ARG GOLANGCI_LINT_VERSION=v2.1.6
ARG SWAG_VERSION=v1.16.4
ARG BUF_VERSION=v1.50.0

RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 python3-pip python3-venv git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN GOBIN=/usr/local/bin go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@${GOLANGCI_LINT_VERSION} \
 && GOBIN=/usr/local/bin go install github.com/swaggo/swag/cmd/swag@${SWAG_VERSION} \
 && GOBIN=/usr/local/bin go install github.com/bufbuild/buf/cmd/buf@${BUF_VERSION} \
 && GOBIN=/usr/local/bin go install golang.org/x/vuln/cmd/govulncheck@latest

COPY extension/runtime/dakcoder_shared-*.whl extension/runtime/dakcoder_agent-*.whl /tmp/wheels/
RUN python3 -m venv /opt/dakcoder \
 && /opt/dakcoder/bin/pip install --no-cache-dir /tmp/wheels/*.whl \
 && rm -rf /tmp/wheels
COPY gotools/gotools /usr/local/bin/gotools

# Writable only where the control plane mounts: the lease's working copy at
# /workspace, the shared module cache at /gomodcache, and /tmp.
ENV PATH=/opt/dakcoder/bin:/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin \
    GOTOOLS_PATH=/usr/local/bin/gotools \
    GOCACHE=/tmp/gocache \
    GOMODCACHE=/gomodcache \
    HOME=/tmp
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin runner
USER 10001
WORKDIR /workspace
EXPOSE 8791
