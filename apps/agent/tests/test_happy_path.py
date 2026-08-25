"""The MVP happy path, end to end, with nothing faked but the model.

Part A section 11.1: adding a resource to ``n-api-template`` is a fixed seven-file
recipe, and the reason a backend agent is worth building at all. This test runs it
against a real copy of the reference service — real ``gotools`` sidecar, real
``text/template`` scaffolder, real ``go build`` and ``go vet`` against the real
private modules.

Only the model is scripted, and that is the point. Everything a model could get
wrong here is a spec, and the spec is validated on the Go side; everything
downstream of the spec is deterministic. So a scripted model tests exactly what
the real one would exercise, without the variance.

It takes roughly thirty seconds, which is why it is marked slow. It is also the
single most valuable test in the repository: it is the only one that would catch
a contract drift between the Python tool layer and the Go sidecar, and drift
there is invisible to both sides' unit tests. The first time it ran it found
three — ``fx_wire`` wanting a bare constructor name, ``project_scaffold`` taking
two specs rather than one, and ``list_filters`` rejecting the ``type`` field that
plan.md section 10.1's own example envelope includes.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import AgentLoop, Outcome
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import commands, fs, knowledge
from dakcoder_agent.tools.gotools import GoTools, _find_binary, handlers_for
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ChatResult, ToolCall, Usage
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


pytestmark = [pytest.mark.slow, pytest.mark.skipif(not _AVAILABLE, reason=_WHY_NOT)]

#: Note the absence of `type` on the list filter: the scaffolder derives it from
#: the named field, so supplying it is an error. plan.md section 10.1's example
#: includes it, and the shipped schema is the authority.
SPEC = {
    "name": "Pension",
    "table": "pensions",
    "route_base": "/pensions",
    "operations": ["create", "list", "get", "update", "delete"],
    "fields": [
        {"go": "PPONumber", "json": "ppo_number", "db": "ppo_number", "type": "string",
         "validate": "required", "sql": "VARCHAR(20) NOT NULL"},
        {"go": "Amount", "json": "amount", "db": "amount", "type": "float64",
         "validate": "required", "sql": "DECIMAL(12,2) NOT NULL"},
        {"go": "Status", "json": "status", "db": "status", "type": "string",
         "validate": "oneof=active suspended closed", "sql": "VARCHAR(16) NOT NULL"},
    ],
    "list_filters": [{"go": "Status", "form": "status"}],
}

EXPECTED_FILES = {
    "core/domain/pension.go",
    "db/pensions.sql",
    "repo/postgres/pension.go",
    "handler/response/pension.go",
    "handler/pension.go",
    "handler/request.go",
    "bootstrap/bootstrapper.go",
}


def say(text: str) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop", usage=Usage(prompt_tokens=100))


def call(name: str, args: dict) -> ChatResult:
    return ChatResult(
        tool_calls=[ToolCall(id="chatcmpl-tool-01", name=name, arguments=json.dumps(args))],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100),
    )


class Scripted:
    def __init__(self, turns):
        self.turns = list(turns)

    def chat(self, messages, *, tools=None, **kw):
        return self.turns.pop(0) if self.turns else say("done")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    """One run, shared by every assertion below.

    Module-scoped because the run takes about thirty seconds and every assertion
    is a different question about the same outcome. Re-running it per test would
    turn a thirty-second test into a five-minute one for no extra coverage.
    """
    source = Path(__file__).resolve().parents[3] / "new-template"
    if not source.is_dir():
        pytest.skip("the reference template is not in this checkout")

    work = tmp_path_factory.mktemp("service") / "pisapi"
    shutil.copytree(source, work, ignore=shutil.ignore_patterns(".git", "bin", "*.exe"))

    workspace = Workspace.at(work)
    with GoTools(work) as sidecar:
        router = Router(
            workspace,
            {**fs.HANDLERS, **knowledge.HANDLERS, **commands.HANDLERS, **handlers_for(sidecar)},
        )
        agent = AgentLoop(
            ContextManager(mode=Mode.PLANNER, system_prompt="You are dakcoder."),
            Scripted([
                say(
                    "1. Call resource_scaffold with the Pension spec.\n"
                    "   Accepts: go build ./... clean\n"
                    "2. Confirm the repository and handler are wired with fx_wire.\n"
                    "   Accepts: FxRepo and FxHandler updated"
                ),
                call("resource_scaffold", {"spec": json.dumps(SPEC)}),
                call("fx_wire", {"kind": "repo", "ctor": "NewPensionRepository"}),
                call("fx_wire", {"kind": "handler", "ctor": "NewPensionHandler"}),
                say("The Pension resource is scaffolded and wired."),
            ]),
            router,
            approve=lambda _r: True,
        )
        events = list(
            agent.run("Add a Pension resource", acceptance=["go build ./... clean"])
        )
    return agent, events, workspace


def gates(events):
    return [e for e in events if e.type is EventType.GATE and e.data.get("kind") == "full"]


# ── the outcome ─────────────────────────────────────────────────────────────


def test_the_run_finishes_with_a_clean_gate(run) -> None:
    agent, _events, _ws = run
    assert agent.result.outcome == Outcome.DONE, agent.result.summary


def test_it_wrote_the_seven_files_of_the_recipe(run) -> None:
    _agent, _events, workspace = run
    for rel in EXPECTED_FILES:
        assert (workspace.root / rel).is_file(), f"{rel} was not written"


def test_the_scaffolded_code_actually_compiles(run) -> None:
    """The one assertion that cannot be faked.

    Everything else here checks that files appeared with plausible contents. This
    checks that the Go compiler, resolving real private modules from
    gitlab.cept.gov.in, accepts what the templates produced.
    """
    _agent, events, _ws = run
    stages = {s["name"]: s for s in gates(events)[-1].data["stages"]}
    assert stages["go_build"]["ok"]
    assert stages["go_vet"]["ok"]


def test_the_contract_linter_passes_on_the_generated_code(run) -> None:
    """The scaffolder's output must satisfy the same rules the agent enforces on
    hand-written code. A generator exempt from its own linter is a generator that
    teaches the wrong pattern."""
    _agent, events, _ws = run
    stages = {s["name"]: s for s in gates(events)[-1].data["stages"]}
    assert stages["rules_lint"]["ok"]


def test_the_new_routes_would_reach_the_swagger_document(run) -> None:
    _agent, events, _ws = run
    stages = {s["name"]: s for s in gates(events)[-1].data["stages"]}
    assert stages["swagger_check"]["ok"]


# ── the details that only a real run exposes ────────────────────────────────


def test_the_scaffolder_wires_fx_itself_and_fx_wire_says_so(run) -> None:
    """resource_scaffold already registers both constructors, so a following
    fx_wire finds them present. The sidecar calls that success, and it has to
    stay success: a Debugger re-running fx_wire on an already-wired handler must
    not read "already registered" as "the wiring did not work"."""
    _agent, events, _ws = run
    wired = [
        e for e in events
        if e.type is EventType.TOOL_RESULT and e.data["name"] == "fx_wire"
    ]
    assert len(wired) == 2
    assert all(e.data["ok"] for e in wired)
    assert all("already registered" in e.data["content"] for e in wired)


def test_the_first_gate_catches_the_templates_own_go_mod_drift(run) -> None:
    """A finding about the reference template, reproduced every run.

    Its go.mod requires api-db while its code imports n-api-db, so the first
    `go mod tidy` at the gate is never a no-op — on a service the agent has not
    otherwise changed the dependencies of. The gate blocks, tidy has already
    applied the fix, and the next gate passes. That is the sequence working:
    drift detected, corrected, confirmed.
    """
    _agent, events, workspace = run
    assert len(gates(events)) >= 2, "expected the first gate to fail and a second to pass"

    first = {s["name"]: s for s in gates(events)[0].data["stages"]}
    assert not first["go_mod tidy"]["ok"]
    assert "n-api-db" in (workspace.root / "go.mod").read_text(encoding="utf-8")


def test_go_mod_appears_in_the_change_list(run) -> None:
    """Because tidy really did rewrite it. A file the run changed and did not
    report is a file the developer merges without reading."""
    agent, _events, _ws = run
    assert "go.mod" in agent.result.mutations


def test_the_ddl_is_written_but_never_applied(run) -> None:
    """Part A section 7.2: sql_migrate is deliberately absent. Applying DDL from
    an agent turn is the one irreversible action with no `git restore`."""
    _agent, events, workspace = run
    ddl = (workspace.root / "db" / "pensions.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE" in ddl
    assert not any(
        e.data["name"].startswith(("sql_", "psql"))
        for e in events
        if e.type is EventType.TOOL_CALL
    )


def test_the_generated_files_keep_the_templates_line_endings(run) -> None:
    """The whole repository is CRLF. A generated file with LF endings shows as
    entirely changed the first time anything touches it."""
    _agent, _events, workspace = run
    raw = (workspace.root / "core" / "domain" / "pension.go").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") > 0


def test_no_committed_credential_was_ever_read_into_an_event(run) -> None:
    """The reference template ships twelve committed credentials. Nothing in a
    run's event stream may carry one: the stream is logged, traced, and shown in
    a UI that people screenshot."""
    _agent, events, workspace = run
    secrets = set()
    for config in (workspace.root / "configs").glob("*.yaml"):
        for line in config.read_text(encoding="utf-8").split("\n"):
            if any(k in line.lower() for k in ("password", "secret", "accesskey", "token")):
                value = line.split(":", 1)[-1].strip().strip("\"'")
                if len(value) > 7:
                    secrets.add(value)

    assert secrets, "expected the reference configs to contain credentials to check against"
    blob = json.dumps([e.data for e in events], default=str)
    for secret in secrets:
        assert secret not in blob, "a committed credential reached the event stream"
