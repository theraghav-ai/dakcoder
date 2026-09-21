"""The published C1 catalogue must match the registry.

A hand-maintained contract document drifts from the code within weeks, and the
drift is silent: nobody reads a table to check it is still true. Generating it
and failing CI when the file disagrees is the only version of "the documentation
is accurate" that survives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import registry
from dakcoder_agent.tools.catalog import as_json, as_markdown, conformance
from dakcoder_agent.tools.gotools import handlers_for

DOCS = Path(__file__).resolve().parents[3] / "docs"


def test_the_registry_satisfies_c1() -> None:
    """A second check that does not share the first one's code path.

    The registry refuses to construct a violating spec at import. That is a claim
    about a safety property, and a claim worth making is worth verifying from
    outside the thing that makes it.
    """
    assert conformance() == []


def test_the_published_catalogue_is_current() -> None:
    path = DOCS / "TOOL-CATALOG.md"
    if not path.is_file():
        pytest.skip("catalogue not generated yet; run `make tool-catalog`")
    assert path.read_text(encoding="utf-8") == as_markdown(), (
        "docs/TOOL-CATALOG.md is stale. Run `make tool-catalog` and commit the result."
    )


def test_the_published_schemas_are_current() -> None:
    path = DOCS / "tool-catalog.json"
    if not path.is_file():
        pytest.skip("catalogue not generated yet; run `make tool-catalog`")
    assert path.read_text(encoding="utf-8") == as_json(), (
        "docs/tool-catalog.json is stale. Run `make tool-catalog` and commit the result."
    )


def test_the_json_is_a_usable_contract() -> None:
    """What the gateway and the extension bind against.

    Checked as data rather than by eye: the extension renders approval dialogues
    from `approval` and `mutates`, and a field that silently changed name would
    turn every approval into a silent auto-approve.
    """
    payload = json.loads(as_json("1.2.3"))

    assert payload["contract"] == "C1"
    assert payload["version"] == "1.2.3"
    assert payload["limits"]["max_params"] == registry.MAX_PARAMS

    names = {t["name"] for t in payload["tools"]}
    assert names == set(registry.REGISTRY)

    for tool in payload["tools"]:
        assert tool["approval"] in {"none", "conditional", "always"}
        assert tool["provider"] in {"python", "gotools", "gopls"}
        assert isinstance(tool["mutates"], bool)
        assert "parameters" in tool


def test_every_mode_listing_matches_the_registry() -> None:
    payload = json.loads(as_json())
    for mode in Mode:
        assert payload["visible_per_mode"][str(mode)] == list(registry.names_for(mode))


def test_gate_only_tools_are_marked_and_invisible() -> None:
    """They are part of the contract — the extension shows them in a gate report
    — but they are not offered to the model, and the document has to say which."""
    payload = json.loads(as_json())
    gate = {t["name"] for t in payload["tools"] if t.get("gate_only")}
    assert gate == set(registry.gate_tools())
    for mode in Mode:
        assert not gate & set(payload["visible_per_mode"][str(mode)])


def test_unavailable_tools_are_published_with_their_reason() -> None:
    """Kept in the catalogue so it is the whole contract rather than only the
    finished part of it, and marked so nothing binds against them yet."""
    payload = json.loads(as_json())
    unavailable = {t["name"]: t for t in payload["tools"] if t.get("unavailable")}
    assert set(unavailable) == {s.name for s in registry.unavailable()}
    for tool in unavailable.values():
        assert tool["unavailable"]
        assert tool["instead"], "an unavailable tool must name a substitute"


def test_the_markdown_names_every_tool(  ) -> None:
    text = as_markdown()
    for name in registry.REGISTRY:
        assert f"`{name}`" in text


def test_every_sidecar_tool_the_model_is_offered_has_a_handler() -> None:
    """A spec without a handler is a tool the model can call and the router
    cannot dispatch — and nothing else catches it.

    The registry validates C1 at import and the catalogue check keeps the
    published file current, but neither knows whether the bridge can actually
    execute the call. That gap is invisible until a turn spends a call on it and
    gets an error back, which is the most expensive possible place to find out.
    """
    declared = {
        spec.name
        for spec in registry.all_specs()
        if spec.provider is registry.Provider.GOTOOLS
    }
    wired = set(handlers_for(None))

    missing = sorted(declared - wired)
    assert not missing, (
        f"{missing} are offered to the model as gotools tools but the bridge has "
        "no handler for them"
    )


# ── sidecar reports must survive elision ────────────────────────────────────


def test_sidecar_reports_render_as_text_not_json() -> None:
    """The failure that ended a session after seventeen turns.

    `rules_lint` on a legacy service returns 705,000 characters of single-line
    JSON. The context manager caps a tool result at a few thousand tokens, so
    what reached the model was a JSON fragment ending mid-string — unparseable,
    and indistinguishable from a broken tool. It reported the audits as
    "truncated", scoped the call, got another fragment, and repeated until the
    loop-breaker stopped it.

    Text degrades honestly under the same cap: the tail is lost and the findings
    survive. This asserts the bridge renders rather than passes through.
    """
    from dakcoder_agent.tools.gotools import Reply, _render_lint, _report

    payload = {
        "files_scanned": 49,
        "count": 1650,
        "violations": [
            {
                "rule": "domain-tags",
                "path": f"core/domain/x{i}.go",
                "line": i,
                "message": f"field {i} has no db tag",
                "fix": "add a db tag",
            }
            for i in range(1200)
        ],
        "warnings": [],
    }
    result = _report(Reply(json.dumps(payload)), lambda p: _render_lint(p, scope_hint="scope it"))

    assert result.ok
    text = result.content
    assert text.lstrip()[0] != "{", "a JSON blob truncates into nothing readable"
    assert "1,200" in text, "the total must survive even though the rows do not"
    assert "domain-tags" in text
    assert "not shown" in text, "what was omitted is itself a finding"
    # Comfortably inside the cap in context.TOOL_CAPS, with room for the elision
    # marker the context manager would add.
    assert len(text) < 8_000, f"rendered report is {len(text)} chars; it will be elided"


def test_every_audit_declares_a_context_cap() -> None:
    """A tool with no cap falls to the default, which tail-truncates.

    That is wrong for a ranked report in a way that is easy to miss: tail
    truncation keeps the least important findings and drops the N+1 the report
    exists to surface. The four audits were shipped without caps and did exactly
    that.
    """
    from dakcoder_agent.context import TOOL_CAPS

    for name in ("db_roundtrip_audit", "validation_audit", "temporal_audit", "lib_version_check"):
        assert name in TOOL_CAPS, f"{name} has no cap; it would be tail-truncated"
        assert TOOL_CAPS[name].strategy == "head", (
            f"{name} is ranked worst-first, so the head is what must be kept"
        )
