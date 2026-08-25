# gotools — build, test and release.
#
# Every target here is also what CI runs, so a green local `make ci` means a
# green pipeline. Divergence between the two is how "works on my machine"
# starts.

BINARY      := gotools
PKG         := gitlab.cept.gov.in/it-2.0/dakcoder/gotools
VERSION     ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)
LDFLAGS     := -s -w -X main.Version=$(VERSION)
GOFLAGS     := -trimpath

# Platforms shipped inside the .vsix (Part B §4.5).
PLATFORMS := windows/amd64 darwin/arm64 darwin/amd64 linux/amd64 linux/arm64

# Corpora the baseline tests assert against.
TEMPLATE := ../new-template
LEGACY   := ../pao-back-end-development

.DEFAULT_GOAL := help
.PHONY: help build install test test-race test-short cover lint fmt fmt-check vet tidy-check ci dist clean baseline bench golden scaffold-demo doc-check doc-pin tool-catalog tool-catalog-check knowledge knowledge-check

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

build: ## Build the binary for the host platform
	go build $(GOFLAGS) -ldflags "$(LDFLAGS)" -o $(BINARY)$(shell go env GOEXE) ./cmd/gotools

install: ## Install into GOBIN
	go install $(GOFLAGS) -ldflags "$(LDFLAGS)" ./cmd/gotools

test: ## Run the test suite
	go test ./...

test-race: ## Run the test suite under the race detector
	go test -race ./...

test-short: ## Run the tests that need no Go toolchain or private modules
	go test -short ./...

# Deliberately per-package, without -coverpkg.
#
# -coverpkg looks like the right answer here, because several packages are
# exercised mostly from another package's tests — spec/ from the scaffold tests,
# workspace/ from the rules tests. But the merged profile it produces is wrong:
# it reported one-line accessors that every single test calls at 40%, and the
# total 8 points below the honest per-package figure. A coverage number that is
# quietly wrong is worse than one that is quietly incomplete.
#
# Where a package reads low, the fix is a direct test, not a flag.
cover: ## Run tests and report per-package coverage
	go test -coverprofile=coverage.out -covermode=atomic ./...
	@go tool cover -func=coverage.out | tail -1

golden: ## Rewrite the scaffold golden snapshots, then review the diff
	go test ./internal/scaffold/ -update
	@echo "golden files rewritten — review the diff before committing"

doc-check: build ## Verify every rule citation against skill.md / SOP.md
	@./$(BINARY)$(shell go env GOEXE) doc-check --docs $(TEMPLATE)

doc-pin: build ## Re-pin the reference documents after reviewing the rules they affect
	@./$(BINARY)$(shell go env GOEXE) doc-check --docs $(TEMPLATE) --update

tool-catalog: build ## Write contract C1 — the published tool schemas
	@./$(BINARY)$(shell go env GOEXE) tool-catalog

tool-catalog-check: build ## Fail if the committed catalogue is stale
	@./$(BINARY)$(shell go env GOEXE) tool-catalog --check

knowledge: build ## Build the agent's knowledge base from skill.md / SOP.md
	@./$(BINARY)$(shell go env GOEXE) knowledge --docs $(TEMPLATE)

knowledge-check: build ## Fail if the committed knowledge base is stale
	@./$(BINARY)$(shell go env GOEXE) knowledge --docs $(TEMPLATE) --check

bench: ## Benchmark the analyser against the real corpora
	go test -run '^$$' -bench . -benchmem ./internal/rules/

# testdata is excluded from formatting on purpose. The scaffold golden snapshots
# are byte-exact fixtures, and two of them are deliberately CRLF because the
# reference template is (see .gitattributes). They also carry a .golden
# extension so gofmt cannot see them at all; this exclusion is the second lock,
# not the first.
GO_SOURCES = $(shell find cmd internal -name '*.go' -not -path '*/testdata/*')

fmt: ## Format (testdata excluded — see above)
	gofmt -w $(GO_SOURCES)

fmt-check: ## Fail if anything is unformatted
	@out=$$(gofmt -l $(GO_SOURCES)); if [ -n "$$out" ]; then echo "unformatted:"; echo "$$out"; exit 1; fi

vet: ## go vet
	go vet ./...

lint: ## golangci-lint (skipped with a warning if not installed)
	@command -v golangci-lint >/dev/null 2>&1 \
		&& golangci-lint run \
		|| echo "golangci-lint not installed — skipping (install: go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest)"

tidy-check: ## Fail if go.mod/go.sum are not tidy
	@cp go.mod go.mod.bak; cp go.sum go.sum.bak
	@go mod tidy
	@if ! diff -q go.mod go.mod.bak >/dev/null || ! diff -q go.sum go.sum.bak >/dev/null; then \
		mv go.mod.bak go.mod; mv go.sum.bak go.sum; \
		echo "go.mod/go.sum are not tidy — run 'go mod tidy'"; exit 1; \
	fi
	@rm -f go.mod.bak go.sum.bak

baseline: build ## Assert the corpus baselines by hand
	@echo "── reference template (must be clean) ──"
	@./$(BINARY)$(shell go env GOEXE) lint --root $(TEMPLATE)
	@echo "── legacy service (must report findings) ──"
	@./$(BINARY)$(shell go env GOEXE) legacy-audit --root $(LEGACY) --quiet || true

scaffold-demo: build ## Scaffold a resource into a throwaway copy of the template and lint it
	@rm -rf .demo && cp -r $(TEMPLATE) .demo && rm -rf .demo/.git
	@printf '%s' '{"name":"Pension","fields":[{"go":"PpoNumber","type":"string","validate":"required","sql":"varchar(20) NOT NULL"},{"go":"Amount","type":"float64"},{"go":"Status","type":"string","validate":"oneof=active suspended closed"}],"list_filters":[{"go":"Status"}]}' > .demo-spec.json
	@./$(BINARY)$(shell go env GOEXE) resource-scaffold --root .demo --spec .demo-spec.json
	@echo "-- the scaffolded resource must lint clean --"
	@./$(BINARY)$(shell go env GOEXE) lint --root .demo
	@echo "output is in .demo/ -- inspect it, then run 'make clean'"

ci: fmt-check vet tidy-check test-race lint doc-check tool-catalog-check knowledge-check ## Everything CI runs

dist: ## Cross-compile every shipped platform into dist/
	@mkdir -p dist
	@for p in $(PLATFORMS); do \
		os=$${p%/*}; arch=$${p#*/}; ext=""; \
		[ "$$os" = "windows" ] && ext=".exe"; \
		echo "  $$os/$$arch"; \
		GOOS=$$os GOARCH=$$arch CGO_ENABLED=0 \
			go build $(GOFLAGS) -ldflags "$(LDFLAGS)" \
			-o dist/$(BINARY)-$$os-$$arch$$ext ./cmd/gotools || exit 1; \
	done
	@cd dist && sha256sum $(BINARY)-* > SHA256SUMS 2>/dev/null || shasum -a 256 $(BINARY)-* > SHA256SUMS
	@echo "checksums written to dist/SHA256SUMS"

clean: ## Remove build output
	rm -rf dist coverage.out .demo .demo-spec.json $(BINARY) $(BINARY).exe
