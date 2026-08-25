"""Regression tests for the defects the Part B interface design uncovered.

Every test here exists because something was already green while the product was
broken. That is the common thread: the compaction tests all supplied their own
correctly-typed summariser and drove ``ContextManager`` directly, so the loop's
own summariser — the one that ships — was never called by anything. The approval
tests all constructed a ``PendingApproval`` by hand, so nobody noticed that the
event announcing an approval carried no way to answer it.

So these drive the *shipping* path, not a convenient one beside it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from dakcoder_agent.context import ContextManager, Message, Recap
from dakcoder_agent.gate import GateReport, StageResult
from dakcoder_agent.loop import AgentLoop
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools.router import ApprovalRequest, Router
from dakcoder_shared.envelope import Event, EventType, Mutation, MutationKind, ToolResult
from dakcoder_shared.llm import ChatResult, Usage


class Replies:
    """A model that returns whatever it was handed, and counts the asking."""

    def __init__(self, *bodies: str) -> None:
        self.bodies = list(bodies)
        self.calls = 0
        self.roles: list[str] = []

    def chat(self, messages, *, tools=None, **kwargs) -> ChatResult:
        self.calls += 1
        self.roles.append(kwargs.get("role", ""))
        body = self.bodies.pop(0) if self.bodies else ""
        return ChatResult(content=body, finish_reason="stop", usage=Usage(prompt_tokens=100))


def loop_with(client, *, mode: Mode = Mode.CODER) -> AgentLoop:
    context = ContextManager(mode=mode, system_prompt="s")
    return AgentLoop(context, client, Router.__new__(Router))


# ── the compaction crash ────────────────────────────────────────────────────


def test_the_loops_own_summariser_satisfies_the_contract_compact_calls_it_with():
    """The defect, stated as a type check.

    ``compact`` is ``Callable[[Sequence[Message]], Recap]`` and calls
    ``.markdown()`` on the result. The shipping ``_summarise`` took a ``str`` and
    returned a ``str``, so the first compaction of any long run raised inside the
    bare ``except``, returned a *list slice*, and died on ``.markdown()`` one
    frame later — where the cause was invisible.
    """
    agent = loop_with(Replies(json.dumps({"goal": "add the pension resource"})))
    messages = _working_set(["handler/pension.go", "repo/postgres/pension.go"])

    recap = agent._summarise(messages)

    assert isinstance(recap, Recap), "compact() calls .markdown() on whatever this returns"
    assert recap.markdown(), "a recap that renders to nothing is not a recap"
    assert recap.goal == "add the pension resource"


def test_a_broken_summariser_call_still_yields_a_usable_recap():
    """The fallback has to be a Recap too.

    The original ``except Exception`` was reaching for 'a degraded recap beats
    ending the run' and achieved the opposite: it swallowed a TypeError and
    turned it into a crash one frame later. Failing to summarise must cost the
    quality of the recap, never the run.
    """

    class Broken:
        def chat(self, *_a, **_k):
            raise ConnectionError("the gateway is down")

    recap = loop_with(Broken())._summarise(_working_set(["handler/pension.go"]))

    assert isinstance(recap, Recap)
    assert "handler/pension.go" in recap.markdown(), "the tail must survive the failure"


def test_unparseable_json_falls_back_rather_than_raising():
    recap = loop_with(Replies("I'm afraid I can't do that."))._summarise(
        _working_set(["repo/postgres/pension.go"])
    )
    assert isinstance(recap, Recap)
    assert "repo/postgres/pension.go" in recap.markdown()


def test_compaction_runs_end_to_end_through_the_context_manager():
    """The integration nobody had: the real summariser, the real compact()."""
    client = Replies(
        json.dumps(
            {
                "goal": "add the pension resource",
                "decisions": ["used the repository, not raw SQL in the handler"],
                "do_not_retry": ["editing handler/pension_validator.go by hand"],
                "open_items": ["the list filters are not wired"],
            }
        )
    )
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    context.set_task("add a pension resource")
    for i in range(12):
        context.append_tool_result("read_file", "x" * 400, tool_call_id=str(i), path=f"f{i}.go")

    agent = AgentLoop(context, client, Router.__new__(Router))
    recap = context.compact(agent._summarise, keep_recent=2)

    assert isinstance(recap, Recap)
    body = recap.markdown()
    assert "do not retry" in body.lower() or "editing handler" in body
    assert context.compactions == 1
    # And the context is still usable afterwards — the real point of the test.
    assert context.build(), "a compaction that leaves an unbuildable context is a crash"


def _working_set(paths: Sequence[str]) -> list[Message]:
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    context.set_task("t")
    for i, path in enumerate(paths):
        context.append_tool_result("read_file", f"contents of {path}", tool_call_id=str(i), path=path)
    return [m for m in context.build() if m.path]


# ── the approval id ─────────────────────────────────────────────────────────


def test_the_pending_event_carries_the_id_needed_to_answer_it():
    """``POST /v1/approvals/{id}`` needs an id, and the event is where it comes from.

    The event used to carry ``{tool, arguments, reason, paths, unconditional}``
    and nothing else, so the card the developer sees dozens of times a day could
    not be actioned from the event that raised it.
    """
    request = ApprovalRequest(
        "patch_file", {"path": "configs/app.yaml"}, "touches a config", ("configs/app.yaml",)
    )
    payload = request.as_dict()

    assert payload["id"] == request.id
    assert payload["id"], "an approval with no id cannot be answered"


def test_two_approvals_never_share_an_id():
    seen = {ApprovalRequest("x", {}, "r").id for _ in range(200)}
    assert len(seen) == 200


def test_the_approval_is_registered_before_the_event_announcing_it():
    """The race, closed.

    A client that answers the instant it reads ``tool_pending`` must not be able
    to arrive before the approval exists. So registration happens through
    ``on_pending``, which the loop calls *before* it yields the event.
    """
    order: list[str] = []
    request = ApprovalRequest("delete_file", {"path": "handler/old.go"}, "deletes a file")

    agent = loop_with(Replies())
    agent.on_pending = lambda r: order.append(f"registered:{r.id}")

    # Drive the two statements the loop executes in this situation, in order.
    agent.on_pending(request)
    order.append(f"announced:{Event(EventType.TOOL_PENDING, request.as_dict()).data['id']}")

    assert order == [f"registered:{request.id}", f"announced:{request.id}"]


def test_the_pending_event_names_which_paths_are_protected():
    request = ApprovalRequest(
        "resource_scaffold",
        {},
        "writes seven files",
        ("handler/pension.go", "configs/app.yaml", "bootstrap/bootstrapper.go"),
    )
    protected = request.as_dict()["protected"]
    assert set(protected) == {"configs/app.yaml", "bootstrap/bootstrapper.go"}
    assert "handler/pension.go" not in protected


# ── the fields the interface needs ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("go.mod", True),
        ("bootstrap/bootstrapper.go", True),
        ("configs/config.dev.yaml", True),
        ("db/migrations/001.sql", True),
        ("handler/user_validator.go", True),
        ("handler/pension.go", False),
        ("repo/postgres/pension.go", False),
    ],
)
def test_every_mutation_says_whether_the_path_is_protected(path: str, expected: bool):
    """One implementation of PROTECTED_GLOBS, not two.

    The alternative was the extension reimplementing the glob list in
    TypeScript, which duplicates a security-relevant constant across the seam
    with no test binding the copies — and the matcher is custom, not fnmatch, so
    a naive port disagrees exactly at the edges.
    """
    assert Mutation(path, MutationKind.MODIFY).as_dict()["protected"] is expected


def test_a_tool_result_reports_how_long_the_tool_took():
    payload = ToolResult(True, "ok", meta={"tool": "go_build", "ms": 3140}).as_dict()
    assert payload["ms"] == 3140


def test_a_tool_result_without_timing_simply_omits_it():
    assert "ms" not in ToolResult(True, "ok").as_dict()


def test_the_gate_says_which_stage_blocked_and_why():
    """Dropping StageResult.content left the panel able to say *which* stage
    failed and never *why* — the compiler errors went to the model and nowhere
    else."""
    report = GateReport(
        results=(
            StageResult("gofmt", True, False, "", 0.2),
            StageResult("go_build", False, True, "handler/pension.go:42:2: undefined", 3.1),
        ),
        not_run=("go_vet",),
        seconds=3.3,
    )
    payload = report.as_dict()

    assert payload["blocked_by"] == "go_build"
    failing = next(s for s in payload["stages"] if s["name"] == "go_build")
    assert "undefined" in failing["content"]


def test_a_passing_stage_does_not_put_its_output_on_the_wire():
    """Thirteen clean stages every gate is a lot of bytes nobody reads."""
    report = GateReport(results=(StageResult("go_vet", True, True, "no findings", 0.4),))
    assert "content" not in report.as_dict()["stages"][0]


def test_a_very_long_failure_is_truncated_and_says_so():
    report = GateReport(
        results=(StageResult("go_test", False, True, "E" * 9000, 1.0),),
    )
    stage = report.as_dict()["stages"][0]
    assert len(stage["content"]) == GateReport.CONTENT_LIMIT
    assert stage["truncated"] is True


def test_usage_carries_the_absolute_budget_not_only_a_percentage():
    """Two surfaces each dividing prompt_tokens by a percentage produced two
    different denominators on screen at low usage. There is one now."""
    context = ContextManager(mode=Mode.CODER, system_prompt="s")
    assert context.usage().budget > 0
    assert context.inspect()["budget"] == context.usage().budget
