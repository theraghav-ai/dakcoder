# Repository-level targets. The Go sidecar has its own Makefile in gotools/;
# this one covers the Python side and the things that span both.
.PHONY: help test test-fast test-integration catalog catalog-check knowledge knowledge-check verify

PY := python

help: ## List the targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | column -t -s "$$(printf '\t')"

test: ## Run every Python test, including the end-to-end run
	@$(PY) -m pytest apps -q

test-fast: ## Skip the ~30s end-to-end run
	@$(PY) -m pytest apps -q -m "not slow"

test-integration: ## Only the tests that need both toolchains
	@DAKCODER_REQUIRE_INTEGRATION=1 $(PY) -m pytest \
		apps/agent/tests/test_gotools_bridge.py \
		apps/agent/tests/test_happy_path.py -q

catalog: ## Write contract C1 — the model-facing tool schemas
	@$(PY) -c "import sys; sys.path[:0]=['apps/agent/src','apps/shared/src']; \
from pathlib import Path; from dakcoder_agent.tools.catalog import as_json, as_markdown; \
Path('docs/TOOL-CATALOG.md').write_text(as_markdown(), encoding='utf-8', newline=''); \
Path('docs/tool-catalog.json').write_text(as_json(), encoding='utf-8', newline=''); \
print('docs/TOOL-CATALOG.md and docs/tool-catalog.json written')"

catalog-check: ## Fail if the published catalogue has drifted from the registry
	@$(PY) -m pytest apps/agent/tests/test_catalog.py -q

# Two copies, deliberately. `packages/knowledge` is where a developer looks and
# what `gotools knowledge --check` compares against in CI; the copy inside the
# agent package is what a deployed wheel actually reads, because there is no
# checkout to walk out there. Written from one generator run so they cannot
# disagree — which they had, the packaged copy holding the whole corpus while
# `packages/knowledge` sat empty and CI's `--check` was the only thing that
# would have said so.
#
# Named `knowledge` because every generated file and .gitlab-ci.yml already say
# "run `make knowledge`". They said it for some time before the target existed.
KB_OUT := packages/knowledge apps/agent/src/dakcoder_agent/knowledge

knowledge: ## Regenerate the knowledge base from new-template's skill.md and SOP.md
	@for out in $(KB_OUT); do \
		(cd gotools && go run ./cmd/gotools knowledge --docs ../new-template --out ../$$out >/dev/null) \
			|| exit 1; \
		echo "wrote $$out"; \
	done

knowledge-check: ## Fail if the committed knowledge base has drifted from its sources
	@cd gotools && go run ./cmd/gotools knowledge --docs ../new-template --check

verify: ## Everything: the sidecar, then the agent
	@$(MAKE) -C gotools ci
	@$(MAKE) test
