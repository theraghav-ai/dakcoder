"""The scripted model, shared by every loop-level suite.

Extracted from ``test_loop.py`` so the regression tests written against the
2026-09-02 audit drive the *same* stub the behavioural suite does. A second copy
would drift, and the first thing it would stop modelling is the thing the
regression is about — ``tool_choice`` being honoured, results being answered,
arguments being schema-shaped.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.gate import GATE
from dakcoder_agent.loop import AgentLoop
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import control
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ChatResult, ToolCall, Usage
from dakcoder_shared.paths import Workspace


class ScriptedClient:
    """Returns preset turns, and records what it was asked with.

    ``response_format`` is the tell for a structured call -- the intent
    classifier and the compaction summariser -- and those are answered from a
    canned object rather than from the script, so a test's turns line up with
    the turns it actually cares about.
    """

    def __init__(self, turns: Sequence[ChatResult], *, kind: str = "change") -> None:
        self.turns = list(turns)
        self.seen_tools: list[list[str]] = []
        self.tool_choices: list[str | None] = []
        self.calls = 0
        #: What the intent classifier answers.
        self.kind = kind

    def chat(
        self, messages, *, tools=None, tool_choice=None, response_format=None, **kwargs
    ) -> ChatResult:
        self.calls += 1
        if response_format is not None:
            name = response_format.get("json_schema", {}).get("name")
            body = {"kind": self.kind} if name == "intent" else {"goal": "scripted"}
            return ChatResult(
                content=json.dumps(body), finish_reason="stop", usage=Usage(prompt_tokens=10)
            )
        self.seen_tools.append([t["function"]["name"] for t in (tools or [])])
        self.tool_choices.append(tool_choice)
        if not self.turns:
            # Numbered, so filler stands for "the model said something" rather
            # than "it said the same thing twice".
            return say(f"nothing further ({self.calls})")
        turn = self.turns.pop(0)
        # `tool_choice` is honoured, because a stub that ignores it lets a test
        # assert the parameter was *sent* while proving nothing about what it
        # does. The endpoint enforces it; so does this.
        if isinstance(tool_choice, dict):
            # A named choice. The endpoint constrains the output to that one
            # tool, so the stub does too -- 5/5 live, and a stub that let the
            # script answer instead would be testing nothing.
            wanted = tool_choice.get("function", {}).get("name", "")
            if not (turn.tool_calls and turn.tool_calls[0].name == wanted):
                self.turns.insert(0, turn)
                # Arguments the named tool actually takes. The endpoint uses
                # guided decoding for a named choice, so what comes back is
                # schema-shaped; a stub that sent the wrong keys would be
                # testing the refusal path and calling it the happy one.
                body = {
                    "submit_plan": {"steps": [{"file": "handler/user.go",
                                               "action": "forced",
                                               "accepts": "go build"}]},
                    "ask_developer": {"questions": ["Which table?"]},
                }.get(wanted, {"answer": "Nothing further to add."})
                return calls((wanted, json.dumps(body)))
        elif tool_choice == "none" and turn.tool_calls:
            self.turns.insert(0, turn)
            return say(f"I cannot call a tool this turn ({self.calls}).")
        elif tool_choice == "required" and not turn.tool_calls:
            self.turns.insert(0, turn)
            return calls(("repo_map", "{}"))
        return turn


def say(text: str) -> ChatResult:
    return ChatResult(content=text, finish_reason="stop", usage=Usage(prompt_tokens=100))


#: Ids are unique across a process, not across a turn. The endpoint's are, and a
#: helper that restarts at zero every turn makes two batches in one run share
#: ids — which reads as "that call was already answered" to every check written
#: against the transcript, including the wire invariant.
_next_id = itertools.count()


def calls(*specs: tuple[str, str]) -> ChatResult:
    return ChatResult(
        tool_calls=[
            ToolCall(id=f"chatcmpl-tool-{next(_next_id):04x}", name=name, arguments=args)
            for name, args in specs
        ],
        finish_reason="tool_calls",
        usage=Usage(prompt_tokens=100),
    )


#: A one-step plan, as `submit_plan` takes it.
PLAN = json.dumps(
    {
        "steps": [
            {
                "file": "handler/user.go",
                "action": "add the Routes method",
                "accepts": "go build ./... clean",
            }
        ]
    }
)


def plan_call() -> ChatResult:
    return calls(("submit_plan", PLAN))


def patch(path: str = "handler/user.go") -> ChatResult:
    return calls(
        (
            "patch_file",
            json.dumps(
                {"path": path, "old": "package handler", "new": "package handler // x"}
            ),
        )
    )


@pytest.fixture
def written(workspace: Workspace) -> Workspace:
    """A workspace the gate will actually gate.

    The ``go.mod`` matters: every Go stage is guarded on the workspace root
    being a module, and without one they all record "workspace root has no
    go.mod" and the report comes back clean. A test asserting on a failing gate
    would then be asserting on a gate that never ran.
    """
    (workspace.root / "go.mod").write_text(
        "module example.test" + chr(10) * 2 + "go 1.24" + chr(10),
        encoding="utf-8",
    )
    (workspace.root / "handler").mkdir(parents=True, exist_ok=True)
    (workspace.root / "handler" / "user.go").write_text(
        "package handler" + chr(10), encoding="utf-8"
    )
    (workspace.root / "bootstrap").mkdir(parents=True, exist_ok=True)
    (workspace.root / "bootstrap" / "bootstrapper.go").write_text(
        "package bootstrap" + chr(10), encoding="utf-8"
    )
    return workspace


@pytest.fixture
def planning_router(router: Router) -> Router:
    """The shared router, plus the two tools that end the planning phase."""
    router.handlers.update(control.HANDLERS)
    return router


@pytest.fixture
def gated(planning_router: Router):
    """Scripted gate stages, so a run's outcome is set by the test not the toolchain.

    A named stage fails **only once the run has changed something**, and that is
    not a convenience -- it is what a failure the run is answerable for looks
    like. The gate takes a baseline before the first edit and reports a stage
    that was already failing as advisory, so a stage scripted to fail from the
    start is correctly excused and the gate comes back clean. Modelling "the
    change broke it" is the only way to test a gate that blocks.

    ``state["pre_existing"]`` is the other half: a stage that fails throughout,
    which must never block.
    """
    state: dict[str, str | None] = {"fail": None, "pre_existing": None}

    for name in {stage.tool for stage in GATE} | {"gofmt", "rules_lint", "go_diagnostics"}:

        def handler(inv, _name=name):
            broke_it = state["fail"] == _name and planning_router.mutations > 0
            if broke_it or state["pre_existing"] == _name:
                return ToolResult.failure(f"{_name}: boom")
            meta = {"violations": 0} if _name == "rules_lint" else {}
            return ToolResult.success(f"{_name}: clean", meta=meta)

        planning_router.handlers[name] = handler
    return state


def build(
    router: Router,
    turns: Sequence[ChatResult],
    *,
    kind: str = "change",
    max_turns: int = 12,
    approve=lambda _r: True,
    cancelled=lambda: False,
) -> tuple[AgentLoop, ScriptedClient]:
    client = ScriptedClient(turns, kind=kind)
    context = ContextManager(mode=Mode.ASK, system_prompt="You are dakcoder.")
    loop = AgentLoop(
        context,
        client,
        router,
        approve=approve,
        cancelled=cancelled,
        max_turns=max_turns,
    )
    return loop, client


# ── invariants (TEST_PLAN §4) ───────────────────────────────────────────────


def declared_and_answered(wire: Sequence[dict]) -> tuple[list[str], list[str]]:
    """Every ``tool_call_id`` the assembled request declares, and every one it answers."""
    declared = [call["id"] for m in wire for call in m.get("tool_calls", ())]
    answered = [m["tool_call_id"] for m in wire if m.get("tool_call_id")]
    return declared, answered


def assert_wire_is_coherent(wire: Sequence[dict]) -> None:
    """Invariant #1: one result per declared call, no result without a call.

    This is the condition for the request being *accepted*, not a style rule. A
    strict OpenAI-compatible endpoint rejects the whole conversation over one
    orphan, and because the message list is append-only the rejection is
    permanent — every later turn and every follow-up carries the same defect.
    """
    declared, answered = declared_and_answered(wire)
    assert sorted(declared) == sorted(answered), (
        f"declared {sorted(declared)} but answered {sorted(answered)}"
    )
    assert len(answered) == len(set(answered)), "a call was answered twice"
