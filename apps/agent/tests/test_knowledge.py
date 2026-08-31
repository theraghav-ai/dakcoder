"""Tests for the knowledge base the agent retrieves from — above all, that the
migration SOP is actually reachable in the shapes an agent asks in.

The knowledge base exists in two generated, committed copies: the checkout copy
at packages/knowledge and the packaged copy inside dakcoder_agent, which is the
one a deployed wheel has. gotools' knowledge-check holds them in lockstep; the
tests here hold the Python side to its half of the bargain — finding a copy at
all, and retrieving the right document from it.
"""

from __future__ import annotations

from pathlib import Path

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.knowledge import _knowledge_root
from dakcoder_agent.tools.router import Router


def _search(router: Router, query: str) -> str:
    out = router.dispatch("search_docs", {"query": query}, mode=Mode.PLANNER)
    assert out.ok, out.content
    return out.content


def test_the_packaged_copy_is_found_without_a_checkout() -> None:
    """The deployed runtime is a wheel in a venv: no packages/ ancestor to walk.

    The packaged copy must be found from the module's own location, because a
    knowledge base that exists only in git is one the agent answers "not
    installed" about — which is exactly what shipped before this."""
    root = _knowledge_root()
    assert root is not None, "no knowledge base found at all"
    assert (root / "SKILL.md").is_file()
    assert (root / "references" / "legacy-migration.md").is_file()


def test_the_two_committed_copies_are_identical() -> None:
    """knowledge-check enforces this in gotools CI; asserting it here too means
    a drifted copy fails the suite the developer actually runs."""
    packaged = Path("apps/agent/src/dakcoder_agent/knowledge")
    checkout = Path("packages/knowledge")
    for path in sorted(checkout.rglob("*.md")):
        twin = packaged / path.relative_to(checkout)
        assert twin.is_file(), f"{twin} missing from the packaged copy"
        assert twin.read_bytes() == path.read_bytes(), f"{twin} drifted"


def test_a_migration_question_retrieves_the_sop(router: Router) -> None:
    """The queries an agent actually sends when handed a conversion task."""
    for query in (
        "migrate legacy service to new template",
        "convert gin handler to n-api serverRoute",
        "template conversion SOP steps",
    ):
        content = _search(router, query)
        assert "legacy-migration" in content, (
            f"{query!r} did not surface the migration SOP"
        )


def test_the_sop_rules_that_fail_at_runtime_are_retrievable(router: Router) -> None:
    """The rules that motivated the integration: each fails silently or at
    runtime if unknown, so each must come back for its own question."""
    cases = {
        "array slice request payload validator dive": "validate:\"dive\"",
        "query parameters not binding GET request": "form",
        "protovalidate version pin": "v0.10.1",
        "govalid Validate method conflict": "govalid",
    }
    for query, must_carry in cases.items():
        content = _search(router, query)
        assert must_carry in content, (
            f"{query!r} came back without {must_carry!r}"
        )
