"""Session 05cd8b8369aa, replayed against the real toolchain.

The developer asked for an AGENTS.md. The service's go.mod marked a module its
code imports directly as ``// indirect`` -- drift that predates the session.
The run wrote the AGENTS.md, and then the gate ran ``go mod tidy`` over the
module, charged that drift to the run, and told it to "fix the imports that
caused the drift". Fourteen turns later go.mod no longer listed the module at
all and the service did not build. The pre-run baseline that exists to excuse
exactly this was ``taken == False`` from the first turn to the last.

Every loop test before this one stubbed the gate stages, so they passed while
the live run failed. Nothing here is faked but the model: a real Go module with
the field's drift in it (a local ``replace``, so no network), the real `go`, the
real gotools sidecar, the real `take_baseline` and the real gate. Skipped,
like `test_happy_path.py`, where Go or the sidecar is missing -- and failing
instead under DAKCODER_REQUIRE_INTEGRATION, which CI sets.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from dakcoder_agent import loop as loop_module
from dakcoder_agent.context import ContextManager
from dakcoder_agent.gate import full_gate, take_baseline
from dakcoder_agent.loop import AgentLoop, Intent, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import commands, control, fs, knowledge
from dakcoder_agent.tools.gotools import GoTools, _find_binary, handlers_for
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.paths import Workspace

from scripted import ScriptedClient, calls

if _find_binary() is None:
    _WHY_NOT = "gotools binary not built; run `make -C gotools build`"
elif shutil.which("go") is None:
    _WHY_NOT = "the Go toolchain is not on PATH"
else:
    _WHY_NOT = ""
if _WHY_NOT and os.environ.get("DAKCODER_REQUIRE_INTEGRATION"):
    raise RuntimeError(f"DAKCODER_REQUIRE_INTEGRATION is set but this cannot run: {_WHY_NOT}")

pytestmark = pytest.mark.skipif(bool(_WHY_NOT), reason=_WHY_NOT)

#: The field's drift, reproduced: `main.go` imports `example.test/dep` directly
#: and go.mod calls it indirect, so `go mod tidy` wants `~example.test/dep`.
#: `example.test/dep2` is required correctly, and used by `util/util.go`, so a
#: run that drops that import produces drift of its own.
_FILES: dict[str, str] = {
    "go.mod": (
        "module example.test/svc\n\n"
        "go 1.25\n\n"
        "require (\n"
        "\texample.test/dep v0.0.0 // indirect\n"
        "\texample.test/dep2 v0.0.0\n"
        ")\n\n"
        "replace example.test/dep => ./dep\n\n"
        "replace example.test/dep2 => ./dep2\n"
    ),
    "main.go": (
        "package main\n\n"
        'import (\n\t"fmt"\n\n\t"example.test/dep"\n\t"example.test/svc/util"\n)\n\n'
        "func main() { fmt.Println(dep.ID(), util.Name()) }\n"
    ),
    "util/util.go": (
        'package util\n\nimport "example.test/dep2"\n\n'
        "// Name is what the service calls itself.\n"
        "func Name() string { return dep2.Name() }\n"
    ),
    "dep/go.mod": "module example.test/dep\n\ngo 1.25\n",
    "dep/dep.go": 'package dep\n\n// ID identifies the caller.\nfunc ID() string { return "id" }\n',
    "dep2/go.mod": "module example.test/dep2\n\ngo 1.25\n",
    "dep2/dep2.go": 'package dep2\n\n// Name names the service.\nfunc Name() string { return "svc" }\n',
}

#: `patch_file` arguments, because `write_file` never overwrites and the model
#: edits an existing file the same way. Together they drop util's use of dep2,
#: which leaves `example.test/dep2` unused: drift this run caused.
_DROP_DEP2 = (
    {"path": "util/util.go", "old": 'import "example.test/dep2"\n\n', "new": ""},
    {"path": "util/util.go", "old": "return dep2.Name()", "new": 'return "svc"'},
)


@pytest.fixture
def service(tmp_path: Path) -> Path:
    root = tmp_path / "svc"
    for rel, body in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8", newline="")
    return root


@pytest.fixture
def router(service: Path):
    with GoTools(service) as sidecar:
        yield Router(
            Workspace.at(service),
            {
                **fs.HANDLERS,
                **knowledge.HANDLERS,
                **commands.HANDLERS,
                **control.HANDLERS,
                **handlers_for(sidecar),
            },
        )


@pytest.fixture
def baselines(monkeypatch) -> list:
    """Every baseline the loop takes, measured by the real `take_baseline`."""
    taken: list = []

    def counting(router, **kw):
        result = take_baseline(router, **kw)
        taken.append(result)
        return result

    monkeypatch.setattr(loop_module, "take_baseline", counting)
    return taken


def _loop(router: Router, turns) -> AgentLoop:
    return AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="You are dakcoder."),
        ScriptedClient(turns),
        router,
        approve=lambda _r: True,
        max_turns=10,
    )


def _finish(answer: str = "done"):
    return calls(("finish", json.dumps({"answer": answer})))


def _plan(path: str):
    return calls(
        (
            "submit_plan",
            json.dumps(
                {
                    "summary": f"write {path}",
                    "steps": [{"file": path, "action": "write it", "accepts": "go_build"}],
                }
            ),
        )
    )


def _full_gates(events) -> list[dict]:
    return [e.data for e in events if e.type is EventType.GATE and e.data.get("kind") == "full"]


def test_the_drift_in_the_fixture_is_real(router: Router, service: Path) -> None:
    """The premise, checked against `go` rather than assumed: the module builds,
    and tidy wants to flip the one requirement -- the field's exact state."""
    before = (service / "go.mod").read_bytes()
    built = router.run_gate_tool("go_build", {})
    tidy = router.run_gate_tool("go_mod", {"op": "tidy", "check": "true"})

    assert built.ok, built.content
    assert tidy.meta.get("changed"), tidy.content
    assert tidy.meta.get("findings") == ["~example.test/dep"]
    assert (service / "go.mod").read_bytes() == before, "the check rewrote go.mod"


def test_the_field_session_ends_done_and_never_touches_go_mod(
    router: Router, service: Path, baselines: list
) -> None:
    """The two messages of 05cd8b8369aa, in order: a planner run that ended on
    `finish`, then "write it" -- a new task -- which plans, writes AGENTS.md
    and finishes."""
    go_mod = (service / "go.mod").read_bytes()

    first = _loop(router, [_finish("I will write AGENTS.md.")])
    list(first.run("Create AGENTS.md at the repository root", intent=Intent.AGENT))
    assert first.result is not None

    content = "# Agent notes\n\nRun `go build ./...` before finishing.\n"
    nxt = _loop(
        router,
        [
            _plan("AGENTS.md"),
            calls(("write_file", json.dumps({"path": "AGENTS.md", "content": content}))),
            _finish(),
            _finish(),
        ],
    )
    nxt.context = first.context
    nxt.carry_from(first)
    events = list(nxt.run("write it", continued=True))

    assert nxt.result is not None and nxt.result.outcome is Outcome.DONE, nxt.result
    assert "only documentation changed" in nxt.result.summary
    assert (service / "go.mod").read_bytes() == go_mod
    assert list(nxt.router.touched) == ["AGENTS.md"]
    for report in _full_gates(events):
        assert all(s["skipped"] for s in report["stages"]), report
    # One baseline for the session: the second message's new task found the
    # first one still describing an untouched workspace.
    assert len(baselines) == 1, f"{len(baselines)} baselines taken"
    assert nxt.state.baseline.taken, nxt.state.baseline.why_not


def test_a_go_change_on_a_drifting_module_is_not_charged_with_the_drift(
    router: Router, service: Path, baselines: list
) -> None:
    """The case the docs-only skip does not cover: the run edits Go. The gate
    runs tidy for real, finds the service's own drift, and the baseline has to
    excuse it -- which needs the baseline to have been taken, live, before the
    edit landed."""
    go_mod = (service / "go.mod").read_bytes()
    reword = {"path": "util/util.go", "old": "calls itself.", "new": "calls itself, in logs."}
    agent = _loop(
        router,
        [
            _plan("util/util.go"),
            calls(("patch_file", json.dumps(reword))),
            _finish(),
            _finish(),
        ],
    )
    events = list(agent.run("Reword the comment on util.Name", intent=Intent.AGENT))

    assert agent.result is not None and agent.result.outcome is Outcome.DONE, agent.result
    gates = _full_gates(events)
    assert gates and gates[-1]["baseline"] == "taken", gates[-1:]
    tidy = next(s for s in gates[-1]["stages"] if s["name"] == "go_mod tidy")
    assert not tidy["ok"] and not tidy["blocking"], tidy
    assert (service / "go.mod").read_bytes() == go_mod
    assert len(baselines) == 1 and baselines[0].taken


def test_new_drift_on_top_of_old_drift_is_still_charged(
    router: Router, service: Path
) -> None:
    """The baseline excuses `~example.test/dep` and nothing else. Before the
    flip was keyed, the baseline read "tidy failing, on nothing", which
    `Baseline.charge` excuses whole -- so this run's own drift went through."""
    baseline = take_baseline(router, include_tests=False)
    assert baseline.taken, baseline.why_not
    assert baseline.findings["go_mod"] == frozenset({"~example.test/dep"})

    for edit in _DROP_DEP2:
        done = router.dispatch("patch_file", edit, approved=True)
        assert done.ok, done.content
    report = full_gate(router, ["util/util.go"], baseline=baseline)

    assert not report.ok
    assert report.blocked_by is not None and report.blocked_by.name == "go_mod tidy"
    assert "example.test/dep2" in report.blocked_by.content


def test_a_baseline_survives_the_docs_being_written_while_it_measures(
    router: Router, service: Path
) -> None:
    """The live ordering: the model writes AGENTS.md while `go vet` and the
    unscoped lint are still measuring. Before, any write discarded the snapshot,
    silently."""
    real = router.handlers["rules_lint"]

    def lint_while_the_model_writes(inv):
        router.dispatch(
            "write_file", {"path": "AGENTS.md", "content": "# notes\n"}, approved=True
        )
        return real(inv)

    router.handlers["rules_lint"] = lint_while_the_model_writes
    baseline = take_baseline(router, include_tests=False)

    assert baseline.taken, baseline.why_not
    assert baseline.passed["go_mod"] is False
    assert (service / "AGENTS.md").is_file()


def test_a_baseline_that_a_go_edit_lands_in_says_why_it_was_discarded(
    router: Router, caplog
) -> None:
    real = router.handlers["rules_lint"]

    def lint_while_the_model_edits(inv):
        done = router.dispatch(
            "write_file", {"path": "util/extra.go", "content": "package util\n"}, approved=True
        )
        assert done.ok, done.content
        return real(inv)

    router.handlers["rules_lint"] = lint_while_the_model_edits
    with caplog.at_level("WARNING", logger="dakcoder_agent.gate"):
        baseline = take_baseline(router, include_tests=False)

    assert not baseline.taken
    assert "written while it was being measured" in baseline.why_not
    assert "pre-run baseline discarded" in caplog.text
