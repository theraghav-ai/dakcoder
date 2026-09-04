"""The state contract: what the loop asserts is true, and what counts as progress.

Every test here covers a mechanism the loop did not have, and each of those gaps
produced the same class of failure in the field: a run that could not tell what
it had done from what it had said it would do.

The four mechanisms, and the failure each replaces:

* **The state block.** The loop held ``router.touched``, the plan, the gate
  verdict and the dead ends the whole time and showed the model none of it.
  Twelve ``append_user`` sites and not one carried an inventory. So the model's
  only account of its own progress was a transcript that contains its own
  intentions in the same voice as its results, and "I will write migration.md"
  and "migration.md is written" are one message apart.

* **Per-step plan status.** Progress was a set difference between the plan's
  paths and ``router.touched``, computed on demand and stored nowhere. It could
  not say "written, and the gate rejected it".

* **Novelty.** ``informed`` meant "dispatched and not mode-refused", so a search
  that found nothing, a search that found what an earlier one already found, and
  a report identical to one three turns back all counted as progress -- and
  ``stalled_turns``, which gates the whole ``must_answer`` rescue, reset on every
  one of them.

* **The replan.** Every response to a wrong plan was a *stop*. There was no path
  from the acting mode back to planning, so "this approach cannot work" was not
  a thing the run could conclude.

Driven by the shared scripted model, like ``test_loop.py``. What is asserted is
the loop's state after a run rather than the text it produced, because the text
is the thing that was never trustworthy.
"""

from __future__ import annotations

import json

from dakcoder_agent.context import PINNED_LAYERS, Eviction, Layer
from dakcoder_agent.loop import (
    Intent,
    _fingerprint,
    MAX_FINISH_REFUSALS,
    MAX_REPLANS,
    MAX_RETRIEVAL_REPEATS,
    MAX_STALLED_TURNS,
    MAX_UNEXPLAINED_FINISH_REFUSALS,
    STALLS_BEFORE_ANSWER,
    _salvaged,
)
from dakcoder_agent.modes import Mode
from dakcoder_agent.tools import control, registry
from dakcoder_agent.tools.router import Router
from dakcoder_shared.envelope import ToolResult
from dakcoder_shared.llm import ChatResult, ToolCall

from scripted import (  # noqa: E402 - the shared scripted model
    build,
    calls,
    patch,
    plan_call,
    say,
)
from scripted import gated, planning_router, written  # noqa: F401,E402


def state_block(loop) -> str:
    """The block as the model would receive it, from the assembled prompt.

    Read out of ``context.build()`` rather than by calling ``_state_block``
    directly, because the thing under test is whether it *reaches the model* --
    which is precisely what was missing.
    """
    return "\n\n".join(
        m.content for m in loop.context.build() if m.layer is Layer.DIRECTIVE
    )


# -- the state block ---------------------------------------------------------


def test_the_state_block_names_the_files_the_run_actually_wrote(
    planning_router: Router, gated, written
) -> None:
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    block = state_block(loop)
    assert "Files this run has written" in block
    assert "handler/user.go" in block
    assert "it does not exist" in block, (
        "the block must say what the absence of a path means, not only list presences"
    )


def test_the_state_block_says_none_rather_than_going_quiet(
    planning_router: Router, gated, written
) -> None:
    """A run that has written nothing is the case the model gets wrong.

    An empty inventory rendered as an absent section reads exactly like a
    section that was never there, and the model falls back on the transcript --
    which is where its own unfulfilled intentions live.
    """
    loop, _client = build(planning_router, [plan_call(), say("I have read enough.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    block = state_block(loop)
    assert "Files this run has written: none" in block
    assert "has been written yet" in block


def test_the_state_block_is_assembled_last(planning_router: Router, gated, written) -> None:
    """It is rebuilt every turn, so where it sits is a cost decision.

    ``build`` puts the volatile layer at the end, measured: the same edit costs
    11 tokens of prefill there against 75,764 in the pinned task block at 100
    turns. A state block anywhere above the working set would re-prefill the
    conversation on every single turn.
    """
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    layers = [m.layer for m in loop.context.build()]
    assert layers[-1] is Layer.DIRECTIVE, "the state block must be the last message"
    assert Layer.DIRECTIVE in PINNED_LAYERS, "and compaction must never reach it"


def test_rebuilding_an_unchanged_state_block_is_free(
    planning_router: Router, gated, written
) -> None:
    """Idempotent, or the prefix cache pays for a turn in which nothing happened."""
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    before = loop.context.build()[-1]
    loop.context.set_state(before.content)
    assert loop.context.build()[-1] is before, "an identical rebuild must not replace it"


# -- per-step plan status ----------------------------------------------------


def test_a_written_step_is_marked_done(planning_router: Router, gated, written) -> None:
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert [s.status for s in loop.state.plan] == [control.DONE]
    assert loop._unwritten_targets() == []


def test_a_step_the_gate_rejects_is_failed_not_done(
    planning_router: Router, gated, written
) -> None:
    """"Written" and "accepted" are different claims.

    The old plan state could express only the first, so a file that had been
    written and rejected was indistinguishable from one that was finished -- and
    the run reported it as work completed.
    """
    gated["fail"] = "go_build"
    # Named the way a compiler names it. `_gate_blamed` looks known path strings
    # up in the blocking stage's output rather than parsing it, so a stage that
    # fails without naming a file leaves every step `done` -- which is correct,
    # and is what the default scripted failure does.
    # Mutation-aware, exactly as the `gated` fixture is: the gate takes a
    # baseline before the first edit, so a stage that fails from the start is
    # correctly excused as pre-existing and never blocks. Only "the change broke
    # it" produces a blocking failure to be blamed on a file.
    planning_router.handlers["go_build"] = lambda inv: (
        ToolResult.failure("handler/user.go:3:6: undefined: Routes")
        if planning_router.mutations > 0
        else ToolResult.success("go_build: clean")
    )
    loop, _client = build(
        planning_router, [plan_call(), patch(), say("Done.")], max_turns=3
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    step = loop.state.plan[0]
    assert step.status == control.FAILED, "the gate named this file"
    assert "go_build" in step.note
    assert step.open, "a rejected step is still work the run owes"


def test_an_unwritten_step_stays_pending(planning_router: Router, gated, written) -> None:
    loop, _client = build(planning_router, [plan_call(), say("I have read enough.")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert [s.status for s in loop.state.plan] == [control.PENDING]
    assert loop._unwritten_targets() == ["handler/user.go"]


def test_a_skipped_step_survives_the_resync_and_is_not_owed(
    planning_router: Router, gated, written
) -> None:
    """The one status the loop does not overrule.

    ``done``/``failed``/``pending`` are read off the workspace and the gate.
    ``skipped`` is a judgement the model made and justified through
    ``revise_plan``, and a resync that reset it would be the loop arguing with a
    decision it is not positioned to take.
    """
    loop, _client = build(planning_router, [plan_call(), say("x")])
    list(loop.run("add Routes", intent=Intent.AGENT))

    loop.state.plan = (
        control.PlanStep(
            file="handler/user.go",
            action="add Routes",
            accepts="go build",
            status=control.SKIPPED,
            note="the method already exists",
        ),
    )
    loop._sync_plan()

    assert loop.state.plan[0].status == control.SKIPPED
    assert loop.state.plan[0].note == "the method already exists"
    assert loop._unwritten_targets() == [], "a justified skip is not outstanding work"


# -- novelty: dispatched is not the same as informed -------------------------


def _search(pattern: str, locations: list[str]) -> ToolResult:
    body = "\n".join(locations) or "no matches"
    return ToolResult.success(
        f"{len(locations)} match(es)\n{body}",
        meta={"scanned": 9, "hits": len(locations), "locations": locations,
              **({} if locations else {"informed": False})},
    )


def _search_turn(pattern: str) -> ChatResult:
    return calls(("search_repo", json.dumps({"pattern": pattern})))


def test_a_search_that_finds_nothing_is_not_progress(
    planning_router: Router, gated, written
) -> None:
    """The single line that made the stall detector unreachable.

    A zero-match ``search_repo`` returns ok=True -- correctly, because a
    zero-match search is a real finding and reporting it as a failure makes the
    model retry it. ``informed`` counted every ok dispatch, so a model rephrasing
    a fruitless search reset ``stalled_turns`` on every turn, forever.
    """
    planning_router.handlers["search_repo"] = lambda inv: _search(inv.arg("pattern"), [])
    loop, _client = build(
        planning_router,
        [plan_call()] + [_search_turn(f"Handler{i}") for i in range(4)],
        max_turns=6,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.stalled_turns >= 2, (
        "four fruitless searches with four different patterns must register as a stall"
    )


def test_a_rephrased_search_reaching_the_same_places_is_not_progress(
    planning_router: Router, gated, written
) -> None:
    """`_fingerprint` is byte-exact over the arguments, so "Handler", "handler"
    and "Handler\\(" are three questions to it and one to anybody else. What
    separates them is where the answers point."""
    hits = ["handler/user.go:3", "handler/user.go:5"]
    planning_router.handlers["search_repo"] = lambda inv: _search(inv.arg("pattern"), hits)
    loop, _client = build(
        planning_router,
        [plan_call()] + [_search_turn(p) for p in ("Handler", "handler", "Handler.")],
        max_turns=5,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.retrieval_repeats.get("search_repo", 0) >= 2
    assert loop.state.stalled_turns >= 1, "the same matches twice is not two findings"
    note = [m for m in loop.context.build() if "added nothing you had not been given" in m.content]
    assert note and str(note[0].role) == "user", (
        "the model is told, and told as a user message rather than as a fake tool result"
    )


def test_search_repo_is_never_withdrawn_however_often_it_repeats(
    planning_router: Router, gated, written
) -> None:
    """`search_docs` is withdrawn when it is exhausted because the corpus cannot
    acquire new sections mid-run. The workspace *does* change, so taking away the
    tool a run navigates with would be a worse failure than the one it prevents."""
    hits = ["handler/user.go:3"]
    planning_router.handlers["search_repo"] = lambda inv: _search(inv.arg("pattern"), hits)
    loop, client = build(
        planning_router,
        [plan_call()] + [_search_turn(f"p{i}") for i in range(MAX_RETRIEVAL_REPEATS + 2)],
        max_turns=8,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.retrieval_repeats["search_repo"] >= MAX_RETRIEVAL_REPEATS
    assert all("search_repo" in offered for offered in client.seen_tools[1:])


def test_an_identical_answer_from_a_different_call_is_not_progress(
    planning_router: Router, gated, written
) -> None:
    """The check `_fingerprint` structurally cannot make: it hashes the question,
    and the loop that happens in the field is one question asked three ways."""
    body = "handler/user.go  func (h *UserHandler) Routes()" * 12
    planning_router.handlers["repo_map"] = lambda inv: ToolResult.success(body)
    planning_router.handlers["git_status"] = lambda inv: ToolResult.success(body)
    loop, _client = build(
        planning_router,
        [
            plan_call(),
            calls(("repo_map", "{}")),
            calls(("git_status", "{}")),
        ],
        max_turns=4,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.stalled_turns == 1, "the second identical body added nothing"


def test_a_mutation_is_always_progress(planning_router: Router, gated, written) -> None:
    """Two identical `write_file` results are two files written, and the second
    is not the run standing still -- so mutations skip the digest entirely."""
    loop, _client = build(
        planning_router, [plan_call(), patch(), patch("bootstrap/bootstrapper.go")], max_turns=4
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.stalled_turns == 0


def test_compaction_clears_the_result_digests(planning_router: Router, gated, written) -> None:
    """BUG L-10's shape, arriving through a new ledger.

    The digests answer "you have already been shown this". After an eviction the
    model has *not* been shown it, and keeping them would suppress the one
    re-dispatch that could recover what compaction removed.
    """
    loop, _client = build(planning_router, [plan_call(), patch(), say("Done.")])
    list(loop.run("add Routes", intent=Intent.AGENT))
    loop.state.seen_digests.add("repo_map:deadbeef")
    loop._forget_evicted(Eviction(messages=3, paths=("handler/user.go",)))
    assert loop.state.seen_digests == set()


# -- the replan --------------------------------------------------------------


def test_two_failing_gates_hand_the_run_back_to_the_planner(
    planning_router: Router, gated, written
) -> None:
    """The move the loop did not have.

    Every other answer to a wrong plan was a stop. The gate is a function of the
    files, so a mode that has twice failed to produce the files it needs is not
    rescued by being asked a third time in the same words.
    """
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), patch(), say("Done."), say("Still done.")], max_turns=5
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.gate_failures == 0, "the budget counts attempts at one strategy"
    assert loop.state.replans == 1
    assert loop.state.mode is Mode.PLANNER
    assert any("back in the planning phase" in m.content for m in loop.context.build())


def test_the_replan_records_what_it_ruled_out(
    planning_router: Router, gated, written
) -> None:
    """What makes it a replan rather than a re-roll.

    Without the record it is the same model, the same context and a fresh guess,
    and the guess lands back on the approach just abandoned.
    """
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), patch(), say("Done."), say("Still done.")], max_turns=5
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert any("go_build" in entry for entry in loop.state.ruled_out), loop.state.ruled_out
    assert "Already ruled out" in state_block(loop)


def test_the_replan_is_bounded(planning_router: Router, gated, written) -> None:
    """A second replan has no new information to plan from. The run has produced
    two gate reports and one abandoned strategy by then; a planner given those
    and still stuck is one the developer needs to see."""
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), patch()] + [say("I cannot fix this.")] * 12, max_turns=20
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.replans <= MAX_REPLANS
    assert loop.result.outcome in ("unverified", "no_progress", "exhausted")


def test_a_planner_that_will_not_replan_is_not_reported_as_done(
    planning_router: Router, gated, written
) -> None:
    """The bug the replan introduced, as a test.

    A prose-only planner turn means "nothing to plan", and on a first pass that
    is a legitimate DONE. After a replan it is not: files are on disk and a gate
    has failed on them, so "nothing was executed and nothing was touched" is
    false on both clauses.
    """
    gated["fail"] = "go_build"
    loop, _client = build(
        planning_router, [plan_call(), patch()] + [say("I cannot fix this.")] * 8, max_turns=14
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.result.outcome != "done"
    assert loop.router.touched, "the run did write something"


def test_revise_plan_replaces_the_plan_without_ending_the_phase(
    planning_router: Router, gated, written
) -> None:
    """The voluntary version. The acting mode keeps its write tools and carries
    straight on -- it is not a terminal tool and not a mode switch."""
    revision = calls(
        (
            "revise_plan",
            json.dumps(
                {
                    "steps": [
                        {"file": "handler/user.go", "action": "add Routes",
                         "accepts": "go build", "status": "done"},
                        {"file": "bootstrap/bootstrapper.go", "action": "wire it",
                         "accepts": "go build"},
                    ],
                    "ruled_out": "the generated validator cannot be edited by hand",
                }
            ),
        )
    )
    loop, _client = build(
        planning_router, [plan_call(), patch(), revision, say("Done.")], max_turns=5
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.mode is Mode.AGENT, "revising is not a phase transition"
    assert [s.file for s in loop.state.plan] == [
        "handler/user.go",
        "bootstrap/bootstrapper.go",
    ]
    assert loop.state.plan[0].status == control.DONE
    assert any("generated validator" in e for e in loop.state.ruled_out)


def test_revise_plan_refuses_a_revision_with_no_reason(planning_router: Router) -> None:
    """`ruled_out` is required and that is the whole design of the tool."""
    out = planning_router.dispatch(
        "revise_plan",
        json.dumps({"steps": [{"file": "a.go", "action": "x", "accepts": "y"}]}),
        mode=Mode.AGENT,
    )
    assert not out.ok
    assert "ruled_out" in out.content


def test_a_revised_plan_cannot_claim_work_that_was_never_written(
    planning_router: Router, gated, written
) -> None:
    """The status field round-trips so a revision keeps finished work finished --
    which means a model could claim a step is done when it is not. The next
    resync reads the workspace and corrects it."""
    revision = calls(
        (
            "revise_plan",
            json.dumps(
                {
                    "steps": [
                        {"file": "bootstrap/bootstrapper.go", "action": "wire it",
                         "accepts": "go build", "status": "done"},
                    ],
                    "ruled_out": "the handler approach does not compile",
                }
            ),
        )
    )
    loop, _client = build(planning_router, [plan_call(), revision, say("x")], max_turns=4)
    list(loop.run("add Routes", intent=Intent.AGENT))

    step = loop.state.plan[0]
    assert step.status == control.PENDING, "the workspace overrules the claim"
    assert loop._unwritten_targets() == ["bootstrap/bootstrapper.go"]


def test_a_stall_with_a_plan_left_replans_before_it_gives_up(
    planning_router: Router, gated, written
) -> None:
    """`no_progress` on a run with a plan and a replan left reports a strategy,
    not the task."""
    same = "handler/user.go  func (h *UserHandler) Routes()" * 12
    planning_router.handlers["repo_map"] = lambda inv: ToolResult.success(same)
    loop, _client = build(
        planning_router,
        [plan_call()] + [calls(("repo_map", "{}"))] * (MAX_STALLED_TURNS + 2),
        max_turns=MAX_STALLED_TURNS + 4,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.replans == 1
    assert any("asked for nothing new" in e for e in loop.state.ruled_out)


# -- the collapsed surveys ---------------------------------------------------


def test_the_five_survey_tools_are_one() -> None:
    for gone in (
        "legacy_audit",
        "db_roundtrip_audit",
        "validation_audit",
        "temporal_audit",
        "lib_version_check",
    ):
        assert registry.get(gone) is None, f"{gone} should have been folded into audit"
    spec = registry.get("audit")
    assert spec is not None
    assert spec.parameters["properties"]["kind"]["enum"] == [
        "legacy",
        "db",
        "validation",
        "temporal",
        "libs",
    ]


def test_the_acting_mode_is_not_offered_the_surveys_or_a_third_linter() -> None:
    """`rules_lint` runs automatically after every edit batch and again at the
    gate. A third path to the same check cost 134 tokens of the one prefix with
    no room in it."""
    agent = set(registry.names_for(Mode.AGENT))
    assert "audit" not in agent
    assert "rules_lint" not in agent
    assert "revise_plan" in agent, "what that room paid for"


def test_the_gate_still_reaches_rules_lint(planning_router: Router) -> None:
    """Removing it from a mode must not remove it from the harness: the gate
    dispatches with ``gate=True``, which bypasses mode filtering by design."""
    planning_router.handlers["rules_lint"] = lambda inv: ToolResult.success("clean")
    out = planning_router.run_gate_tool("rules_lint", {})
    assert out.ok


# -- truncation --------------------------------------------------------------


def test_the_truncation_message_names_the_file_it_lost() -> None:
    """The prefix that did arrive still holds the path, and `path` is almost
    always the first key. Saying it turns a guess into a fact: the reported run
    was told only "your call to write_file arrived cut off" and had to work out
    which of three files that was."""
    cut = ToolCall(
        id="1",
        name="write_file",
        arguments='{"path": "handler/user_test.go", "content": "package handler\\n\\nfunc Test',
    )
    assert _salvaged(cut, "path") == "handler/user_test.go"


def test_salvage_returns_nothing_rather_than_guessing() -> None:
    cut = ToolCall(id="1", name="write_file", arguments='{"cont')
    assert _salvaged(cut, "path") == ""


def test_the_finish_answer_is_bounded() -> None:
    """It said "in full", on the one call that ends the run, whose arguments are
    serialised inside `max_tokens` along with every other call in the reply."""
    answer = registry.get("finish").parameters["properties"]["answer"]["description"]
    assert "150 words" in answer
    assert "in full" not in answer


# -- a stalled acting phase is pushed at the work, not at the exit -----------
#
# The whole section is one field transcript. "write tests for 10 handlers":
# the planner named two test files, the acting phase re-read one 6,571-line
# handler on turns 17 and 18, `stalled_turns` reached 2 at exactly the moment it
# is designed to, and the remedy -- a named `tool_choice` on `finish` -- ended
# the run with neither test file written. The stall detector was right and its
# answer was wrong.


def _finish(answer: str, blocked: str = "") -> ChatResult:
    body = {"answer": answer}
    if blocked:
        body["blocked"] = blocked
    return calls(("finish", json.dumps(body)))


def test_a_stalled_acting_phase_with_work_left_is_not_forced_to_finish(
    planning_router: Router, gated, written
) -> None:
    """BUG L-2's shape on the path it was never applied to.

    `MAX_RESEARCH_TURNS` already asks "is there outstanding work?" before it
    points a stalled phase anywhere. The stall path did not, so two repeated
    reads were enough to end a run that had written nothing its plan named.
    """
    read = calls(("read_file", json.dumps({"path": "handler/user.go"})))
    loop, client = build(
        planning_router,
        [plan_call()] + [read] * (STALLS_BEFORE_ANSWER + 2),
        max_turns=STALLS_BEFORE_ANSWER + 4,
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    forced = [c for c in client.tool_choices if c]
    assert forced, "a stall must still force something"
    assert all(c == "required" for c in forced), (
        f"a stalled phase with unwritten targets must be pushed at the work, "
        f"not at `finish`; got {forced}"
    )
    nudge = [m for m in loop.context.build() if "plan set out to write" in m.content]
    assert nudge, "and told which files are outstanding"
    assert "handler/user.go" in nudge[-1].content


def test_a_stalled_read_only_phase_is_still_forced_to_answer(
    planning_router: Router, gated
) -> None:
    """The other half, unchanged. ASK has no plan and no files to write, so a
    stall there really does mean "say what you found" -- and `finish` is the one
    lever measured to work at that depth."""
    read = calls(("read_file", json.dumps({"path": "handler/user.go"})))
    loop, client = build(
        planning_router,
        [read] * (STALLS_BEFORE_ANSWER + 2),
        kind="question",
        max_turns=STALLS_BEFORE_ANSWER + 3,
    )
    list(loop.run("what does the handler do", intent=Intent.ASK))

    named = [c for c in client.tool_choices if isinstance(c, dict)]
    assert named, "a stalled question must be made to answer"
    assert named[0]["function"]["name"] == "finish"


# -- an unexplained abandonment is not a decision ----------------------------


def test_a_finish_that_abandons_the_plan_without_a_reason_is_refused_twice(
    planning_router: Router, gated, written
) -> None:
    """The loop made a promise in the refusal and did not keep it.

    "Call `finish` again and say which and why in `blocked` -- that will be
    taken at face value." The model called it again with `blocked` empty and it
    was taken at face value anyway, so a run ended on its own statement of
    intent: "I need to read the handler source files, then write the tests."
    """
    excuse = _finish("I need to read the handler files first, then write the tests.")
    loop, _client = build(
        planning_router, [plan_call()] + [excuse] * 4, max_turns=6
    )
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.finish_refused == MAX_UNEXPLAINED_FINISH_REFUSALS
    second = [m for m in loop.context.build() if "will not be read as a reason again" in m.content]
    assert second, "the second refusal must say what is actually missing"
    assert loop.result.outcome != "done", loop.result.summary


def test_a_finish_that_says_why_is_believed_after_one_push(
    planning_router: Router, gated, written
) -> None:
    """The distinction the two limits exist for.

    A model that decided against a step and said so is entitled to be believed:
    this reads paths out of the plan, not out of the work, and is not the arbiter
    of whether the step was still needed. What it is not entitled to is silence.
    """
    explained = _finish(
        "The method is already present.",
        blocked="handler/user.go already declares Routes; writing it again would duplicate it.",
    )
    loop, _client = build(planning_router, [plan_call()] + [explained] * 3, max_turns=5)
    list(loop.run("add Routes", intent=Intent.AGENT))

    assert loop.state.finish_refused == MAX_FINISH_REFUSALS, "pushed once, then believed"
    assert loop.result.outcome != "done" or not loop._unwritten_targets()


# -- telling a stuck reader where the rest of the file is --------------------


def _seed_read(loop, path: str, *, covered: list, total: int) -> None:
    """Put the loop in the state a partly-read large file leaves it in.

    Both halves, because `_live_reads` deliberately asks the *context* what the
    model can still see rather than trusting the ledger -- the ledger records how
    often a file was asked for, the context records which lines are in front of
    the model, and the prior audit's root cause was those two answers coming
    from the same place.
    """
    from dakcoder_agent.loop import _ReadLedger

    ledger = _ReadLedger(lines=total, calls=len(covered))
    for low, high in covered:
        ledger.add(low, high)
        loop.context.append_tool_result(
            "read_file",
            f"lines {low}-{high} of {path}",
            tool_call_id=f"seed-{path}-{low}",
            path=path,
            line_range=(low, high),
        )
    loop.state.reads[path] = ledger


def test_a_replayed_partial_read_says_which_lines_are_still_unseen(
    planning_router: Router, gated, written
) -> None:
    """"Ask something narrower -- a line range" is correct and not actionable.

    A model shown the first hundred lines of a 6,571-line handler does not know
    where the rest is, and every number it could guess is a guess -- so it asks
    the same question again and the turn is spent. The ledger holds the answer:
    it records the intervals actually delivered and the file's length.
    """
    loop, _client = build(planning_router, [say("x")])
    _seed_read(loop, "handler/paogen.go", covered=[(1, 102)], total=6571)

    call = ToolCall(
        id="1", name="read_file", arguments=json.dumps({"path": "handler/paogen.go"})
    )
    hint = loop._unseen_hint(call)

    assert "6,571 lines" in hint
    assert "starts at line 103" in hint
    assert 'read_file(path="handler/paogen.go", start=103' in hint
    assert "search_repo" in hint, "and the other way out of a large file"


def test_the_hint_skips_past_every_range_already_delivered(
    planning_router: Router, gated, written
) -> None:
    """A model working front to back has several spans by the time it gets
    stuck. The next gap is the one worth naming; listing all of them is the same
    problem in a longer form."""
    loop, _client = build(planning_router, [say("x")])
    _seed_read(loop, "handler/paogen.go", covered=[(1, 200), (201, 900)], total=6571)

    hint = loop._unseen_hint(
        ToolCall(id="1", name="read_file", arguments=json.dumps({"path": "handler/paogen.go"}))
    )
    assert "starts at line 901" in hint


def test_the_hint_reaches_the_model_through_the_cached_replay(
    planning_router: Router, gated, written
) -> None:
    """Where it has to land. The cached intercept fires before `_re_reading`
    because the fingerprint matches, so this is the message a repeated
    whole-file read actually receives."""
    loop, _client = build(planning_router, [say("x")])
    _seed_read(loop, "handler/paogen.go", covered=[(1, 102)], total=6571)

    call = ToolCall(
        id="1", name="read_file", arguments=json.dumps({"path": "handler/paogen.go"})
    )
    fingerprint = _fingerprint(call)
    loop.state.last_results[fingerprint] = "package handler"
    loop.state.partial_results[fingerprint] = 240_000

    intercepted = loop._intercept(call, fingerprint)
    assert intercepted is not None
    body, _said, kind = intercepted
    assert kind == "cached"
    assert "starts at line 103" in body


def test_no_unseen_hint_once_the_whole_file_has_been_delivered(
    planning_router: Router, gated, written
) -> None:
    """Nothing to point at, so nothing is said. A hint naming a range that does
    not exist is worse than none."""
    loop, _client = build(planning_router, [say("x")])
    _seed_read(loop, "handler/user.go", covered=[(1, 20)], total=20)

    hint = loop._unseen_hint(
        ToolCall(id="1", name="read_file", arguments=json.dumps({"path": "handler/user.go"}))
    )
    assert hint == ""


def test_a_terminal_call_is_never_answered_from_the_result_cache(
    planning_router: Router, gated, written
) -> None:
    """Caching the answer to "are we done?" cannot be right.

    A model repeating an excuse sends the same `finish` arguments twice, and the
    second was answered "from the previous result; use it and move to the next
    step" -- so the phase never ended, `_phase_ended` never ran, and the counter
    that decides whether to believe an abandonment never advanced.
    """
    loop, _client = build(planning_router, [say("x")])
    call = ToolCall(id="1", name="finish", arguments=json.dumps({"answer": "done"}))
    loop.state.last_results[_fingerprint(call)] = "an earlier answer"

    assert loop._intercept(call, _fingerprint(call)) is None, (
        "a transition must always dispatch"
    )
