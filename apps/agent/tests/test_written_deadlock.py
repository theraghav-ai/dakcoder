"""One field session: 6d923ab574ae, 115 turns, `no_progress`, two files changed.

A migration of a real service. By turn 74 it had converted both handlers of the
`handlers` phase and the inner loop had left each at ``written`` with
``rules_lint`` dirty on it -- the status that exists to say "the file is there
and the work is not finished". From turn 75 the run emitted one byte-identical
128-token reply on 24 of its remaining 41 turns, through three modes, three tool
lists and six developer messages, one of which was "you are hallucinating".

It was not confusion. There was no legal move. Four rules read one status, and
they did not agree on it:

* `_close_phase` counted ``written`` as settled, so `handlers` closed with both
  steps dirty.
* `_why_not_done` counts ``written`` as unfinished, so every `finish` was
  refused: "have been written but the formatter and the contract linter are not
  clean on them yet. Make the change..."
* `plan_objection` refuses steps belonging to a closed phase, so every plan that
  proposed making that change was rejected: "these steps belong to handlers,
  which has already closed."
* `_work_in_flight` and `_open_targets` count ``written`` as settled, so each
  follow-up opened in PLANNER with no write tools, and the one forced AGENT turn
  had `patch_file` taken out of its request.

The loop ordered the work and forbade it, in two user messages seven apart, on
every run. Meanwhile nothing counted the repeated *reply* -- there are ledgers
for a repeated call, search, question and `finish` answer, and all four watch an
argument -- so the only bound that ever fired was `stalled_turns`, which is per
message and restarted five times.

Every test here failed against the commit before the fix.
"""

from __future__ import annotations

import json
from pathlib import Path

from dakcoder_shared.paths import Workspace

from dakcoder_agent.context import ContextManager
from dakcoder_agent.loop import (
    MAX_REPLY_REPEATS,
    REPLY_PROSE_REPEATS,
    REPLY_REPEATS_BEFORE_ANSWER,
    AgentLoop,
    Outcome,
    _State,
)
from dakcoder_agent.migration import MigrationState, Phase, plan_objection
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.plan import PlanRecord
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.router import Router
from scripted import ScriptedClient, build, calls, say  # noqa: E402
from scripted import gated, planning_router, written  # noqa: F401,E402

#: The roadmap the field session actually submitted, trimmed to the three
#: phases the incident is about.
ROADMAP = (
    Phase(name="branch", covers="cut the branch", parts="check, cut"),
    Phase(name="handlers", covers="convert every HTTP handler", parts="objection, objectionfile"),
    Phase(name="grpc-handlers", covers="convert the gRPC handlers", parts="objectiongrpc, publicacctgrpc"),
)


def _loop(root: Path | None = None) -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop.router = Router(Workspace(root or Path.cwd()))
    loop.state = _State()
    loop.state.mode = Mode.AGENT
    loop.session_id = ""
    loop.context = ContextManager(mode=Mode.AGENT, system_prompt="s")
    loop._plan_record = PlanRecord()
    return loop


def _mid_migration(root: Path | None = None) -> AgentLoop:
    """The session at turn 75: branch cut, `handlers` open, both steps written."""
    loop = _loop(root)
    loop.state.migration = MigrationState(active=True, branch="template-conversion")
    loop.state.migration.adopt(ROADMAP)
    loop.state.migration.close("branch")
    loop.state.plan = (
        PlanStep(
            "handler/objectionfile.go",
            "convert FileHandler",
            "go_build",
            phase="handlers",
            status="written",
            note="written; rules_lint is not clean on it yet",
        ),
        PlanStep(
            "handler/objection.go",
            "fix the lint findings",
            "go_build",
            phase="handlers",
            status="written",
            note="written; rules_lint is not clean on it yet",
        ),
    )
    return loop


# ── 1. the deadlock ─────────────────────────────────────────────────────────


def test_a_written_step_holds_its_phase_open() -> None:
    """The one line. `handlers` closed at turn 80 with both steps dirty, and
    from that moment the run was ordered to clean them and refused every plan
    that said so."""
    loop = _mid_migration()

    assert loop._close_phase() == "", "a written step is not a settled step"
    assert loop.state.migration.phase_named("handlers").status == "pending"


def test_a_clean_step_still_closes_its_phase() -> None:
    """The other half: `written` holding a phase open must not become a phase
    that never closes. The inner loop promotes a clean file to `done` and that
    is what closes it."""
    loop = _mid_migration()
    loop.state.plan = tuple(
        PlanStep(s.file, s.action, s.accepts, phase=s.phase, status="done")
        for s in loop.state.plan
    )

    assert loop._close_phase() == "handlers"


def test_a_step_that_cannot_be_cleaned_still_has_an_exit() -> None:
    """`blocked` is settled here, and it is the move the model does not find on
    its own -- so a phase whose lint cannot be satisfied reports the blocker
    rather than hanging. Without this, "written holds the phase open" would be
    the permanently-unsatisfiable condition it replaced."""
    loop = _mid_migration()
    loop.state.plan = tuple(
        PlanStep(s.file, s.action, s.accepts, phase=s.phase, status="blocked", note="600-line cap")
        for s in loop.state.plan
    )

    assert loop._close_phase() == "handlers"


def test_the_completion_guard_and_the_phase_now_agree() -> None:
    """The contradiction itself, as one assertion: the run may not finish while
    a step is written and unclean, so its phase may not close either. Either
    rule alone is defensible; the pair is what had no legal move."""
    loop = _mid_migration()

    assert "not clean" in loop._why_not_done(), "the run is not done"
    assert loop._close_phase() == "", "so the phase is not over"


# ── 2. the phase that closed on somebody else's work ────────────────────────


def test_a_tagged_plan_does_not_close_a_phase_it_is_not_for() -> None:
    """Turn 80 reported "phase 3 of 7 — branch — is complete" about a branch cut
    fifty-eight turns earlier, because the plan's own phase was already closed,
    `working` fell back to the roadmap's first pending phase, and the untagged
    fallback closed it on the strength of handler work."""
    loop = _loop()
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(ROADMAP)
    loop.state.plan = (
        PlanStep("handler/objection.go", "convert", "go_build", phase="handlers", status="done"),
    )
    loop.state.migration.close("handlers")

    assert loop._close_phase() == "", "handler work is not evidence about the branch"
    assert loop.state.migration.phase_named("branch").status == "pending"


def test_a_plan_to_clean_the_unclean_work_is_now_adoptable() -> None:
    """The other jaw. Every plan naming those two files was rejected -- "these
    steps belong to handlers, which has already closed" -- while the loop's own
    `finish` refusal, seven messages up the same transcript, demanded exactly
    that work. With the phase open the objection does not fire, and the two
    messages stop contradicting each other."""
    loop = _mid_migration()
    steps = [
        PlanStep(
            "handler/objection.go", "split it under the 600-line cap", "go_build", phase="handlers"
        )
    ]

    assert plan_objection(loop.state.migration, list(ROADMAP), steps) == ""


def test_an_untagged_plan_still_reaches_its_phase() -> None:
    """The fallback the narrowing must not break: a roadmap under steps that
    carry no phase at all is a migration that could otherwise never reach its
    last phase, and therefore never run the gate."""
    loop = _loop()
    loop.state.migration = MigrationState(active=True)
    loop.state.migration.adopt(ROADMAP)
    loop.state.plan = (PlanStep("go.mod", "swap", "tidy", status="done"),)

    assert loop._close_phase() == "branch"


# ── 3. the state block that gave two answers ────────────────────────────────


def test_the_cursor_never_points_into_a_closed_phase() -> None:
    """The recency slot said, four lines apart: "Migration: phase 4 of 7 —
    grpc-handlers" and "Now: step 2 of 3 of phase handlers". The cursor had been
    frozen on that step for 27 turns because nothing could promote it and
    nothing would move past it."""
    loop = _mid_migration()
    loop.state.migration.close("handlers")  # as `evidenced` may still do

    assert loop.active_step is None, "a closed phase's step is history, not a cursor"

    block = loop._state_block()
    assert "of phase handlers" not in block
    assert "grpc-handlers" in block


def test_the_cursor_line_asks_for_an_edit_not_a_wait() -> None:
    """The self-cancelling pair, four lines apart in one block: "What is
    outstanding is its verification, not more of the work" above a migration
    line saying "the gate is deferred until the last phase closes". The recency
    slot named a job that could not be done, for 27 turns."""
    loop = _mid_migration()
    loop.router.touched.append("handler/objectionfile.go")
    loop.state.cursor = ("handler/objectionfile.go", 1)
    loop.context._turn = 9

    line = loop._cursor_age(loop.state.plan[0])

    assert "rules_lint is not clean" in line, "the finding is named"
    assert "an edit, not a wait" in line
    assert "revise_plan" in line, "and so is the exit"
    assert "outstanding is its verification" not in line


def test_the_cursor_sits_on_the_open_phase_when_there_is_one() -> None:
    """And the ordinary case is unchanged: an open phase's written step is still
    what the cursor names, because that is the work."""
    loop = _mid_migration()

    active = loop.active_step
    assert active is not None
    assert active[1].file == "handler/objectionfile.go"
    assert "of phase handlers" in loop._state_block()


# ── 4. the follow-up that re-planned instead of working ─────────────────────


def test_a_written_step_is_work_in_flight() -> None:
    """Three developer messages (turns 81-87, 91-97, 106-112) opened in PLANNER,
    which holds no write tool and whose only forward move is to submit the plan
    it is already holding. Nineteen turns at ~140,000 prompt tokens each, to
    reach an acting phase one `patch_file` from done."""
    loop = _mid_migration()

    assert loop._work_in_flight(), "written is not finished"
    assert loop._opening_mode(Intent.AGENT, continued=True) is Mode.AGENT


def test_a_settled_plan_still_re_plans() -> None:
    """The narrowness this must keep: a developer asking for more work on a
    finished plan is asking for a new one."""
    loop = _mid_migration()
    loop.state.plan = tuple(
        PlanStep(s.file, s.action, s.accepts, phase=s.phase, status="done")
        for s in loop.state.plan
    )

    assert not loop._work_in_flight()
    assert loop._opening_mode(Intent.AGENT, continued=True) is Mode.PLANNER


# ── 5. the forced turn that lost its write tools ────────────────────────────


def test_unclean_work_is_outstanding_work() -> None:
    """`_open_targets` is empty here -- both files exist -- so the stall escape
    took the branch that cuts the tool list to the terminals and says "give the
    developer what you have established now". `_why_not_done` then refused the
    `finish` that branch had just demanded."""
    loop = _mid_migration()

    assert loop._open_targets() == [], "nothing is unwritten"
    assert loop._outstanding() == ["handler/objectionfile.go", "handler/objection.go"]
    assert "not clean yet" in loop._outstanding_ask()
    assert "revise_plan" in loop._outstanding_ask(), "the exit is always named"


def test_the_ask_does_not_tell_it_to_write_a_file_it_has_written() -> None:
    """A model told to write a file it has already written writes it again,
    which is how the churn ledger came to be needed."""
    loop = _mid_migration()

    assert "Write it now" not in loop._outstanding_ask()
    assert "patch the part the finding names" in loop._outstanding_ask()


def test_a_planner_stall_is_not_asked_to_write() -> None:
    """AGENT only. Naming files to write at a mode holding no write tool is an
    instruction it cannot obey, and PLANNER's way out of a stall is
    `submit_plan`."""
    loop = _mid_migration()
    loop.state.mode = Mode.PLANNER

    assert loop._outstanding() == []


# ── 6. the reply nothing was counting ───────────────────────────────────────


#: The field reply, in shape: prose that narrates the work and one read that
#: has already been answered. Both halves matter -- the prose is what 25 copies
#: of the context were spent on, and the call is what the ledgers could see.
FIELD_REPLY_PROSE = (
    "I need to fix the remaining lint issues in handler/objection.go. "
    "Let me first check the current state:"
)


def _same_reply():
    reply = calls(("read_file", json.dumps({"path": "handler/objection.go", "start": 390})))
    reply.content = FIELD_REPLY_PROSE
    return reply


def test_the_same_reply_stops_being_appended(planning_router: Router) -> None:
    """25 copies of one 110-token message reached the final prompt -- 7% of a
    153,000-token context, spent teaching the model to send a 26th. The calls
    still travel; only the duplicated prose is replaced."""
    reply = _same_reply()
    loop, _client = build(
        planning_router,
        [reply, reply, reply, reply, say("done")],
        kind="question",
        max_turns=8,
    )
    list(loop.run("read it", intent=Intent.ASK))

    prose = [
        m.content
        for m in loop.context.build()
        if m.role == "assistant" and (m.content or "").strip()
    ]
    infull = [p for p in prose if FIELD_REPLY_PROSE in p]
    stubbed = [p for p in prose if "the same reply and the same call" in p]

    assert len(infull) <= REPLY_PROSE_REPEATS + 1, infull
    assert stubbed, "a repeated reply keeps being appended in full"


def test_the_same_reply_reaches_a_terminal_in_a_handful_of_turns(
    planning_router: Router,
) -> None:
    """What the fix is worth, end to end. The field run took 41 turns and about
    5.9 million prompt tokens to reach the `no_progress` it could have reached
    here; a loop that honours `tool_choice` now reaches a terminal instead."""
    reply = _same_reply()
    loop, _client = build(planning_router, [reply] * 12, kind="question", max_turns=20)
    list(loop.run("read it", intent=Intent.ASK))

    assert loop.result.turns <= REPLY_REPEATS_BEFORE_ANSWER + 2, loop.result.turns
    ended = [
        m for m in loop.context.build() if any(c.name == "finish" for c in m.tool_calls)
    ]
    assert ended, "the escape never reached a terminal"


def test_the_same_reply_ends_the_run_when_the_escape_is_ignored() -> None:
    """The backstop under the escape, for the server that drops `tool_choice`.

    `MAX_STALLED_TURNS` was the only thing here and it is per message, so the
    field session reached it five separate times and carried on. This bound is
    tighter, and `carry_from` makes it the session's.
    """

    class Defiant(ScriptedClient):
        """A server that accepts `tool_choice` and does not honour it."""

        def chat(self, messages, *, tool_choice=None, **kwargs):
            return super().chat(messages, **kwargs)

    reply = _same_reply()
    client = Defiant([reply] * 20, kind="question")
    loop = AgentLoop(
        ContextManager(mode=Mode.ASK, system_prompt="s"),
        client,
        Router(Workspace(Path.cwd())),
        max_turns=30,
    )
    list(loop.run("read it", intent=Intent.ASK))

    assert loop.result.outcome == Outcome.NO_PROGRESS
    assert "same reply" in loop.result.summary
    assert loop.state.reply_repeats >= MAX_REPLY_REPEATS
    assert loop.result.turns <= MAX_REPLY_REPEATS + 3, loop.result.turns


def test_a_run_that_is_working_never_trips_it(planning_router: Router) -> None:
    """Two different reads are two pieces of work, however alike they look."""
    first = calls(("read_file", json.dumps({"path": "handler/user.go", "start": 1})))
    second = calls(("read_file", json.dumps({"path": "handler/user.go", "start": 200})))
    loop, _client = build(
        planning_router, [first, second, say("done")], kind="question", max_turns=8
    )
    list(loop.run("read it", intent=Intent.ASK))

    assert loop.state.reply_repeats == 0
    assert loop.result.outcome != Outcome.NO_PROGRESS


def test_the_forced_turn_names_the_repetition(planning_router: Router) -> None:
    """The one thing the model cannot see from a transcript of its own confident
    narration. The opener it is added to is unchanged and measured."""
    reply = _same_reply()
    loop, _client = build(
        planning_router, [reply, reply, reply, say("done")], kind="question", max_turns=8
    )
    list(loop.run("read it", intent=Intent.ASK))

    pushed = [
        m.content for m in loop.context.build() if (m.content or "").startswith("Stop searching.")
    ]
    assert pushed, "the stall never spoke"
    assert any("same reply" in p for p in pushed)


# ── 7. the bounds that restarted at every developer message ─────────────────


def test_the_reply_ledger_survives_a_developer_message(planning_router: Router) -> None:
    """Five runs, six developer messages, `stalled_turns` reaching its bound of
    six five separate times. A reply that is still identical *after* the
    developer has typed something is not a new attempt at the work."""
    reply = _same_reply()
    first, _ = build(planning_router, [reply, reply, reply], kind="question", max_turns=3)
    list(first.run("read it", intent=Intent.ASK))
    assert first.state.reply_repeats >= 1, "the run under test never repeated"

    second, _ = build(planning_router, [say("done")], kind="question")
    second.carry_from(first)

    assert second.state.reply_repeats == first.state.reply_repeats
    assert second.state.reply_key == first.state.reply_key


def test_the_refusal_budgets_survive_a_developer_message(planning_router: Router) -> None:
    """One refused `finish` was refused three more times across four messages,
    for a reason that never changed. `plan_forced` was carried for exactly this
    and only solved it for one of the four counters."""
    first, _ = build(planning_router, [say("done")], kind="question")
    list(first.run("hello", intent=Intent.ASK))
    first.state.finish_refused = 1
    first.state.plan_objections = 2
    first.state.degenerate_refused = 1
    first.state.reasks = 1

    second, _ = build(planning_router, [say("done")], kind="question")
    second.carry_from(first)

    assert second.state.finish_refused == 1
    assert second.state.plan_objections == 2
    assert second.state.degenerate_refused == 1
    assert second.state.reasks == 1


def test_the_refusal_budgets_carry_leniently() -> None:
    """They carry in the direction that stops asking, not the one that stops the
    run: a `finish` already sent back once is believed the next time."""
    loop = _mid_migration()
    loop.state.finish_refused = 1

    assert loop._why_not_done() == "", "it has had its say"
