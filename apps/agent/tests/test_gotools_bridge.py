"""Integration tests for the Go sidecar bridge, against the real binary.

The point of running the real ``gotools`` here is that the bridge's whole value
is protocol compatibility, and a mock of the protocol proves compatibility with
the mock. These tests are what justify hand-writing the client instead of taking
the SDK: they exercise the actual wire format, so a change on either side breaks
them.

Skipped rather than failed when the binary is absent. A contributor without Go
installed should still be able to run the Python suite, and a missing sidecar is
an environment fact, not a regression.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.gotools import GoTools, SidecarError, _find_binary, handlers_for
from dakcoder_agent.tools.router import Router
from dakcoder_shared.paths import Workspace


def _integration_available() -> tuple[bool, str]:
    """Whether this machine can run the integration tests, and why not if it cannot.

    Skipping is right on a contributor's laptop without Go. It is wrong in CI,
    where a missing toolchain means a broken pipeline rather than an absent
    capability — and a silent skip leaves the pipeline green while the most
    valuable test in the repository never runs. DAKCODER_REQUIRE_INTEGRATION
    turns the skip into a failure, and the CI job sets it.
    """
    if _find_binary() is None:
        return False, "gotools binary not built; run `make -C gotools build`"
    if shutil.which("go") is None:
        return False, "the Go toolchain is not on PATH"
    return True, ""


_AVAILABLE, _WHY_NOT = _integration_available()
if not _AVAILABLE and os.environ.get("DAKCODER_REQUIRE_INTEGRATION"):
    raise RuntimeError(
        f"DAKCODER_REQUIRE_INTEGRATION is set but the integration tests cannot run: {_WHY_NOT}"
    )


pytestmark = pytest.mark.skipif(not _AVAILABLE, reason=_WHY_NOT)


@pytest.fixture
def reference() -> Workspace:
    root = Path(__file__).resolve().parents[3] / "new-template"
    if not root.is_dir():
        pytest.skip("the reference template is not in this checkout")
    return Workspace.at(root)


@pytest.fixture
def live(reference: Workspace):
    with GoTools(reference.root) as sidecar:
        yield sidecar


@pytest.fixture
def live_router(reference: Workspace, live: GoTools) -> Router:
    return Router(reference, handlers_for(live))


# ── the protocol ────────────────────────────────────────────────────────────


def test_the_handshake_and_a_call_both_work(live: GoTools) -> None:
    reply = live.call("rules_lint", {})
    assert not reply.is_error
    assert json.loads(reply.text)["files_scanned"] > 0


def test_one_process_serves_many_calls(live: GoTools) -> None:
    """The reason for a long-lived process at all: about 30 ms of startup per
    call, across a session with a hundred lint calls, is several seconds of a
    latency budget Part A section 3 calls the single biggest lever."""
    def findings() -> dict:
        payload = json.loads(live.call("rules_lint", {}).text)
        payload.pop("duration_ms", None)  # wall-clock, varies by a millisecond
        return payload

    first = findings()
    for _ in range(4):
        live.call("rules_lint", {})
    assert live._proc is not None and live._proc.poll() is None
    assert findings() == first, "a reused process must give the same answer"


def test_section_signs_survive_the_pipe(live: GoTools) -> None:
    """Citations are the load-bearing part of a rule finding, and every one
    contains U+00A7. An encoding fault here would silently corrupt them all —
    and would look fine on a console that cannot print the character either,
    which is exactly how this class of bug survives review.
    """
    payload = json.loads(live.call("rules_lint", {}).text)
    citations = [
        v.get("citation", "")
        for v in (payload.get("violations") or []) + (payload.get("warnings") or [])
    ]
    assert citations, "expected at least one finding with a citation"
    assert any("§" in c for c in citations)
    assert not any("�" in c for c in citations)


def test_a_bad_tool_name_is_an_error_not_a_hang(live: GoTools) -> None:
    with pytest.raises(SidecarError):
        live.call("no_such_tool", {})


def test_a_dead_sidecar_is_restarted_transparently(live: GoTools) -> None:
    """A rule engine dying is not something the model can act on, so the bridge
    recovers rather than reporting. Once, not in a loop: if it dies twice on the
    same input, the input is the problem."""
    live.call("rules_lint", {})
    live._proc.kill()
    live._proc.wait(timeout=5)
    assert not live.call("rules_lint", {}).is_error


# ── argument mapping ────────────────────────────────────────────────────────


def test_a_comma_separated_path_list_becomes_an_array(live_router: Router) -> None:
    """The model-facing schema takes a string because C1 caps parameters at six.
    The sidecar takes an array because Go generated it from a []string. This is
    the only place the two shapes have to agree."""
    # PLANNER, not AGENT: `rules_lint` left the acting mode once the inner
    # loop was already running it after every edit batch. The bridge this
    # test is about is unchanged, and the gate still reaches the tool through
    # `run_gate_tool`, which bypasses mode filtering by design.
    out = live_router.dispatch(
        "rules_lint", {"paths": "handler/user.go"}, mode=Mode.PLANNER
    )
    assert out.ok
    # Read off `meta`, not by parsing the content. `rules_lint` returns a
    # rendered digest — the raw JSON stopped being the content the day
    # `_render_lint` landed, and this assertion went on parsing prose as JSON
    # and failing for it. The counts the sidecar reported are in `meta` now.
    assert out.meta["files_scanned"] > 0


def test_repo_map_respects_its_token_budget(live_router: Router) -> None:
    out = live_router.dispatch("repo_map", {"max_tokens": 1200}, mode=Mode.PLANNER)
    assert out.ok
    assert json.loads(out.content)["est_tokens"] <= 1200


def test_repo_map_reads_the_generation_from_imports(live_router: Router) -> None:
    """The reference template's go.mod requires api-db while its code imports
    n-api-db. Reading go.mod would label the reference template legacy — which
    it did, before this was fixed on the Go side."""
    out = live_router.dispatch("repo_map", {}, mode=Mode.PLANNER)
    assert json.loads(out.content)["generation"] == "n-api"


def test_a_lint_finding_is_a_success_not_a_tool_failure(live_router: Router) -> None:
    """Marking a finding as ok=False would make the loop treat a lint report as
    a broken tool and retry it — a wasted turn, and noise in the transcript."""
    out = live_router.dispatch("rules_lint", {}, mode=Mode.PLANNER)
    assert out.ok


def test_an_invalid_scaffold_spec_comes_back_with_the_field_paths(
    live_router: Router,
) -> None:
    """The Go side validates specs, because that is where the rules live. Its
    error names every problem with a fix for each, which is better than anything
    this layer could construct — so it is passed through unchanged."""
    out = live_router.dispatch(
        "resource_scaffold",
        {"spec": json.dumps({"name": "9bad", "fields": []})},
        mode=Mode.AGENT,
        approved=True,
    )
    assert not out.ok
    assert out.fix
