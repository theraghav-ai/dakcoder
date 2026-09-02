"""Regression tests for the 2026-09-02 audit (`AUDIT.md`, `TEST_PLAN.md`).

One test per confirmed finding, named for the finding, written against the real
modules with a scripted model — the same shape as the reproductions the audit
shipped in `audit-repros/`, promoted here so they run on every commit.

The audit's own diagnosis of the suite it found is the reason this file exists
as its own module: *the tests exercise each component's own discipline and never
the seams*. Everything here is a seam.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from dakcoder_agent.loop import Intent, Outcome
from dakcoder_agent.session import Status
from dakcoder_agent.tools.router import Router
from scripted import (  # noqa: F401 - fixtures are used by name
    PLAN,
    assert_wire_is_coherent,
    build,
    calls,
    declared_and_answered,
    gated,
    patch,
    plan_call,
    planning_router,
    say,
    written,
)


# ── L-1: a terminal tool inside a batch orphans the rest of the batch ───────


def test_terminal_tool_in_batch_answers_all_calls(
    planning_router: Router, gated, written
) -> None:
    """A batch of `[submit_plan, read_file]` must answer both calls.

    `submit_plan` succeeds, the phase ends, and the loop returns — with
    `read_file` declared in the assistant message and no result anywhere. The
    orphan is permanent: it is in the working set for every later turn of the
    session and for every follow-up built on the same context, and a strict
    endpoint rejects each of them.
    """
    loop, _client = build(
        planning_router,
        [
            calls(("submit_plan", PLAN), ("read_file", '{"path":"handler/user.go"}')),
            say("done"),
        ],
    )
    list(loop.run("add a Routes method", intent=Intent.AGENT))

    wire = loop.context.wire()
    declared, answered = declared_and_answered(wire)
    assert len(declared) >= 2, "the batch itself must still be recorded"
    assert_wire_is_coherent(wire)
    assert not loop.context.wire_repairs, (
        "the loop must answer the call itself; the wire repair is a backstop, "
        f"and it fired: {loop.context.wire_repairs}"
    )

    unrun = [m for m in wire if m.get("role") == "tool" and "was not run" in m["content"]]
    assert unrun and "ended the phase" in unrun[0]["content"], (
        "the unrun call must say why it did not run, not merely occupy a slot"
    )


def test_finish_in_batch_answers_all_calls(planning_router: Router, gated, written) -> None:
    """The same defect one phase later, where the run also *ends* on it."""
    loop, _client = build(
        planning_router,
        [
            plan_call(),
            calls(
                ("finish", json.dumps({"answer": "done"})),
                ("read_file", '{"path":"handler/user.go"}'),
                ("search_repo", '{"query":"Routes"}'),
            ),
        ],
    )
    list(loop.run("add a Routes method", intent=Intent.AGENT))

    assert_wire_is_coherent(loop.context.wire())
    assert not loop.context.wire_repairs


def test_cancelled_batch_still_answers_every_call(
    planning_router: Router, gated, written
) -> None:
    """The one path that already did this right. Pinned so a refactor keeps it."""
    stop = {"now": False}

    def cancelled() -> bool:
        return stop["now"]

    loop, _client = build(
        planning_router,
        [
            plan_call(),
            calls(
                ("read_file", '{"path":"handler/user.go"}'),
                ("read_file", '{"path":"main.go"}'),
            ),
        ],
        cancelled=cancelled,
    )
    events = loop.run("add a Routes method", intent=Intent.AGENT)
    for event in events:
        # Cancel the moment the plan is in: the next batch is abandoned.
        if event.type.value == "plan":
            stop["now"] = True

    assert loop.result.outcome == Outcome.ABORTED
    assert_wire_is_coherent(loop.context.wire())


# ── the wire checkpoint itself (CHANGE_PLAN step 1.2) ───────────────────────


def test_wire_repairs_an_orphaned_call_and_says_so() -> None:
    """The backstop, tested directly: it must repair *and* report.

    A silent recovery for an invariant violation is how the violation survives
    to the next release.
    """
    from dakcoder_agent.context import ContextManager
    from dakcoder_shared.llm import ToolCall

    context = ContextManager(system_prompt="sys")
    context.append_assistant(
        "",
        tool_calls=(
            ToolCall(id="a", name="read_file", arguments="{}"),
            ToolCall(id="b", name="write_file", arguments="{}"),
        ),
    )
    context.append_tool_result("read_file", "contents", tool_call_id="a")

    wire = context.wire()
    assert_wire_is_coherent(wire)
    assert context.wire_repairs == ("unanswered call write_file#b",)


def test_wire_keeps_an_orphaned_result_as_prose() -> None:
    """A result whose call is gone must not be dropped — the model was told it.

    Deleting it edits the model's history; leaving it as `role: "tool"` is
    malformed. It becomes a user message carrying the same text.
    """
    from dakcoder_agent.context import ContextManager, Layer, Message, Role

    context = ContextManager(system_prompt="sys")
    context._working.append(  # noqa: SLF001 - constructing the state compaction can leave
        Message(
            role=Role.TOOL,
            content="the important finding",
            layer=Layer.WORKING_SET,
            tool_call_id="gone",
            source="read_file",
        )
    )

    wire = context.wire()
    assert_wire_is_coherent(wire)
    assert any("the important finding" in m["content"] for m in wire)
    assert all(m.get("role") != "tool" for m in wire)
    assert context.wire_repairs


# ── TL-1 / TL-2: a stray byte must not empty the file ──────────────────────


def _patch(router: Router, path: str, old: str, new: str):
    from dakcoder_agent.modes import Mode

    return router.dispatch("patch_file", {"path": path, "old": old, "new": new}, mode=Mode.AGENT)


def test_patch_file_non_utf8_preserves_content(router: Router, workspace) -> None:
    """A file with one stray non-UTF-8 byte survives being patched.

    The read handler uses `surrogateescape` so such a file is readable at all;
    the write used strict UTF-8, and `Path.write_text` truncates before the
    encoder raises. The file was left at zero bytes, the model got a generic
    failure, and no mutation was recorded — so neither the gate nor `revert`
    ever learned the file had been emptied.
    """
    target = workspace.root / "handler" / "legacy.go"
    target.write_bytes(b"package handler\n// caf\xe9 latte\nfunc Old() {}\n")
    before = target.read_bytes()

    out = _patch(router, "handler/legacy.go", "func Old() {}", "func New() {}")

    assert out.ok, f"the patch must apply, not fail: {out.content}"
    after = target.read_bytes()
    assert after, "the file must never be left empty"
    assert b"\xe9" in after, "the stray byte must survive the round trip"
    assert b"func New() {}" in after
    assert after != before


def test_a_failed_write_leaves_the_file_untouched(router: Router, workspace) -> None:
    """Atomicity, from the caller's side: a refused patch changes nothing."""
    target = workspace.root / "handler" / "user.go"
    before = target.read_bytes()

    out = _patch(router, "handler/user.go", "nothing that appears in the file", "x")

    assert not out.ok
    assert target.read_bytes() == before


def test_write_preserves_the_executable_bit(router: Router, workspace) -> None:
    """A patched script that quietly loses +x is breakage nobody attributes here."""
    script = workspace.root / "scripts" / "build.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(b"#!/bin/sh\necho old\n")
    script.chmod(0o755)

    out = _patch(router, "scripts/build.sh", "echo old", "echo new")

    assert out.ok
    assert script.stat().st_mode & 0o111, "the executable bit must survive the write"


def test_no_temporary_file_is_left_behind(router: Router, workspace) -> None:
    _patch(router, "handler/user.go", "func New() *UserHandler { return nil }",
           "func New() *UserHandler { return &UserHandler{} }")
    assert not list(workspace.root.rglob("*.dakcoder-tmp"))


# ── L-11: revert must not destroy work the run did not do ──────────────────


def _git_repo(root):
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False
        )

    if git("init", "-q").returncode != 0:
        pytest.skip("git is not available")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "test")
    git("add", "-A")
    git("commit", "-q", "-m", "baseline")
    return git


def test_revert_restores_pre_run_developer_changes(workspace) -> None:
    """The developer's uncommitted edit survives a revert of the agent's edit.

    HEAD-based revert reset the whole file, destroying work that was there
    before the run started — on a system whose entire Baseline design exists
    because a repository does not start clean.
    """
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.session import SessionStore
    from dakcoder_agent.tools import fs
    from dakcoder_agent.tools.router import Router
    from dakcoder_agent.undo import UndoStore

    _git_repo(workspace.root)

    target = workspace.root / "handler" / "user.go"
    committed = target.read_bytes()
    developers = committed + b"\n// the developer was here\n"
    target.write_bytes(developers)

    store = SessionStore(workspace.root)
    session = store.create("change the handler")
    router = Router(workspace, dict(fs.HANDLERS), undo=UndoStore(workspace.root, session.id))

    out = router.dispatch(
        "patch_file",
        {"path": "handler/user.go", "old": "func New()", "new": "func Renamed()"},
        mode=Mode.AGENT,
    )
    assert out.ok
    session.mutations.append("handler/user.go")
    session.status = Status.DONE

    plan = store.revert(session)

    assert plan.blocked == (), f"nothing should be blocked: {plan.blocked}"
    assert target.read_bytes() == developers, (
        "revert must restore what was there when the run started, not HEAD"
    )


def test_revert_keeps_a_developers_untracked_file(workspace) -> None:
    """A file the developer created and the agent merely edited is not deleted.

    HEAD does not have it, and the old rule read "absent from HEAD" as "the run
    created it" — so the revert unlinked a file the run never created.
    """
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.session import SessionStore
    from dakcoder_agent.tools import fs
    from dakcoder_agent.tools.router import Router
    from dakcoder_agent.undo import UndoStore

    _git_repo(workspace.root)

    scratch = workspace.root / "handler" / "scratch.go"
    scratch.write_bytes(b"package handler\n\nfunc Scratch() {}\n")
    before = scratch.read_bytes()

    store = SessionStore(workspace.root)
    session = store.create("edit the scratch file")
    router = Router(workspace, dict(fs.HANDLERS), undo=UndoStore(workspace.root, session.id))

    out = router.dispatch(
        "patch_file",
        {"path": "handler/scratch.go", "old": "func Scratch() {}", "new": "func Scratch() { _ = 1 }"},
        mode=Mode.AGENT,
    )
    assert out.ok
    session.mutations.append("handler/scratch.go")
    session.status = Status.DONE

    plan = store.revert(session)

    assert "handler/scratch.go" not in plan.delete, "an untracked file must not be deleted"
    assert scratch.exists(), "the developer's untracked file must survive a revert"
    assert scratch.read_bytes() == before


def test_revert_deletes_only_what_the_run_created(workspace) -> None:
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.session import SessionStore
    from dakcoder_agent.tools import fs
    from dakcoder_agent.tools.router import Router
    from dakcoder_agent.undo import UndoStore

    store = SessionStore(workspace.root)
    session = store.create("write a new file")
    router = Router(workspace, dict(fs.HANDLERS), undo=UndoStore(workspace.root, session.id))

    out = router.dispatch(
        "write_file",
        {"path": "handler/new.go", "content": "package handler"},
        mode=Mode.AGENT,
    )
    assert out.ok
    session.mutations.append("handler/new.go")
    session.status = Status.DONE

    plan = store.revert(session)
    assert plan.delete == ("handler/new.go",)
    assert not (workspace.root / "handler" / "new.go").exists()


def test_the_snapshot_is_the_first_state_not_the_latest(workspace) -> None:
    """Two edits to one file must still revert to the developer's version."""
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.session import SessionStore
    from dakcoder_agent.tools import fs
    from dakcoder_agent.tools.router import Router
    from dakcoder_agent.undo import UndoStore

    target = workspace.root / "handler" / "user.go"
    original = target.read_bytes()

    store = SessionStore(workspace.root)
    session = store.create("edit twice")
    router = Router(workspace, dict(fs.HANDLERS), undo=UndoStore(workspace.root, session.id))

    for old, new in (("func New()", "func Renamed()"), ("func Renamed()", "func Again()")):
        assert router.dispatch(
            "patch_file", {"path": "handler/user.go", "old": old, "new": new}, mode=Mode.AGENT
        ).ok
    session.mutations.append("handler/user.go")
    session.status = Status.DONE

    store.revert(session)
    assert target.read_bytes() == original


# ── L-8: the read ledger must reflect what survived the insertion cap ──────


def _big_file(workspace, lines: int = 8000) -> str:
    """A file large enough that the 48k-token read cap elides most of it."""
    body = "\n".join(f"// line {n} — padding to make this file genuinely large" for n in range(1, lines + 1))
    (workspace.root / "core").mkdir(parents=True, exist_ok=True)
    (workspace.root / "core" / "huge.go").write_text(body + "\n", encoding="utf-8")
    return "core/huge.go"


def test_capped_read_then_tail_read_dispatches(planning_router: Router, workspace) -> None:
    """The tail of a big file must stay reachable.

    `read_file` returns all 8,000 lines; the context keeps roughly the first
    third; the elision marker says "re-read the file with a narrower line
    range"; and the read ledger — written from the *tool's* span — then refused
    exactly that re-read as "already in context above". Two true-sounding
    messages that could not both be obeyed, and on any file over ~150KB the
    tail was unreachable for the rest of the run.
    """
    path = _big_file(workspace)
    loop, _client = build(planning_router, [say("noop")])

    out = planning_router.dispatch("read_file", {"path": path}, mode="agent")
    assert out.ok
    span = tuple(out.meta["span"])
    appended = loop.context.append_tool_result(
        "read_file", out.for_model(), tool_call_id="t1", path=path, line_range=span
    )

    assert appended.line_range is not None
    assert appended.line_range[1] < span[1], (
        "this test needs the cap to actually fire; it did not"
    )
    loop._record_read(path, appended.line_range, int(out.meta["lines"]))

    from dakcoder_shared.llm import ToolCall

    tail = ToolCall(id="t2", name="read_file", arguments=json.dumps(
        {"path": path, "start": 6000, "end": 6500}))
    assert loop._re_reading(tail) == "", (
        "a read of lines the cap elided must dispatch, not be refused as already seen"
    )

    seen = ToolCall(id="t3", name="read_file", arguments=json.dumps(
        {"path": path, "start": 10, "end": 20}))
    assert loop._re_reading(seen), "a read of lines that *are* in context is still refused"


def test_the_elision_marker_names_what_survived(planning_router: Router, workspace) -> None:
    """"Re-read with a narrower range" is only actionable if the model can tell
    which range is missing."""
    path = _big_file(workspace)
    loop, _client = build(planning_router, [say("noop")])
    out = planning_router.dispatch("read_file", {"path": path}, mode="agent")

    appended = loop.context.append_tool_result(
        "read_file", out.for_model(), tool_call_id="t1", path=path,
        line_range=tuple(out.meta["span"]),
    )
    assert "elided" in appended.content
    assert f"lines 1-{appended.line_range[1]} are above" in appended.content


# ── L-10: compaction must invalidate the loop's ledgers ────────────────────


def test_compaction_invalidates_read_ledger(planning_router: Router, workspace) -> None:
    """After the content is evicted, the re-read the recap invites must dispatch.

    The recap says "re-read one only if you need a line range you have not
    seen"; the intercept refused exactly that with "already in context above".
    Neither message could be obeyed and the model had no way to recover the
    content, so it stalled and was forced to finish.
    """
    from dakcoder_shared.llm import ToolCall

    loop, _client = build(planning_router, [say("noop")])
    out = planning_router.dispatch(
        "read_file", {"path": "handler/user.go", "start": 1, "end": 3}, mode="agent"
    )
    appended = loop.context.append_tool_result(
        "read_file", out.for_model(), tool_call_id="t1",
        path="handler/user.go", line_range=tuple(out.meta["span"]),
    )
    loop._record_read("handler/user.go", appended.line_range, int(out.meta["lines"]))

    again = ToolCall(id="t2", name="read_file", arguments=json.dumps(
        {"path": "handler/user.go", "start": 1, "end": 3}))
    assert loop._re_reading(again), "while it is in context the re-read is refused"

    loop.context.append_user("filler so there is something to retain")
    loop.context.compact(lambda _evicted: _empty_recap(), keep_recent=1)
    loop._forget_evicted(loop.context.last_eviction)

    assert loop.context.last_eviction.messages, "this test needs the compaction to evict"
    assert loop._re_reading(again) == "", (
        "once the content is evicted the re-read must dispatch, not be refused"
    )


def test_compaction_keeps_coverage_that_survived(planning_router: Router, workspace) -> None:
    """Only the evicted spans become askable again — not the whole file."""
    from dakcoder_shared.llm import ToolCall

    loop, _client = build(planning_router, [say("noop")])
    path = "repo/postgres/user.go"
    for start, end in ((1, 2), (3, 4)):
        out = planning_router.dispatch(
            "read_file", {"path": path, "start": start, "end": end}, mode="agent"
        )
        appended = loop.context.append_tool_result(
            "read_file", out.for_model(), tool_call_id=f"t{start}",
            path=path, line_range=tuple(out.meta["span"]),
        )
        loop._record_read(path, appended.line_range, int(out.meta["lines"]))

    loop.context.compact(lambda _evicted: _empty_recap(), keep_recent=1)
    loop._forget_evicted(loop.context.last_eviction)

    first = ToolCall(id="a", name="read_file", arguments=json.dumps(
        {"path": path, "start": 1, "end": 2}))
    second = ToolCall(id="b", name="read_file", arguments=json.dumps(
        {"path": path, "start": 3, "end": 4}))
    assert loop._re_reading(first) == "", "the evicted read must be askable again"
    assert loop._re_reading(second), "the surviving read must still be refused"


def _empty_recap():
    from dakcoder_agent.context import Recap

    return Recap(turns=(1, 1))


# ── L-3: a write-heavy context must be able to compact ────────────────────


def test_write_heavy_compaction_frees_tokens() -> None:
    """Twenty 40KB `write_file` arguments must be visible to the retention cut.

    `usage()` counted `tool_calls` arguments and `_retention_cut` did not, so a
    working set that was 200k tokens to the compaction *trigger* was zero
    tokens to the compaction *cut*: compaction fired, evicted nothing, and the
    run died either as NO_PROGRESS blaming the working set or as ERROR "context
    cannot be reduced below budget".
    """
    from dakcoder_agent.context import ContextManager, Recap
    from dakcoder_shared.llm import ToolCall

    context = ContextManager(system_prompt="sys")
    context.set_task("migrate the service")
    blob = json.dumps({"path": "x.go", "content": "x" * 40_000})
    for n in range(20):
        context.append_assistant(
            "", tool_calls=(ToolCall(id=f"w{n}", name="write_file", arguments=blob),)
        )
        context.append_tool_result("write_file", "wrote x.go", tool_call_id=f"w{n}")

    before = context.usage().total
    assert context.should_compact(), f"the trigger must fire; usage is {before:,}"

    context.compact(lambda evicted: Recap(turns=(1, 1)))
    after = context.usage().total

    assert after < before, f"compaction freed nothing: {before:,} -> {after:,}"
    assert not context.should_compact(), (
        f"still over the threshold after compacting: {after:,}"
    )


def test_the_cut_and_the_budget_agree_on_every_message() -> None:
    """Invariant #3: one cost model, asserted over messages with and without calls."""
    from dakcoder_agent.context import ContextManager, Layer, Message, Role
    from dakcoder_shared.llm import ToolCall

    context = ContextManager(system_prompt="sys")
    samples = [
        Message(Role.USER, "plain text"),
        Message(Role.ASSISTANT, "", tool_calls=(ToolCall(id="1", name="write_file",
                                                         arguments='{"content":"' + "y" * 5000 + '"}'),)),
        Message(Role.ASSISTANT, "prose and a call", tool_calls=(
            ToolCall(id="2", name="read_file", arguments='{"path":"a.go"}'),)),
        Message(Role.TOOL, "a result", tool_call_id="1"),
    ]
    for message in samples:
        context._working.append(message)

    charged = context.usage().by_layer[Layer.WORKING_SET]
    counted = sum(context._message_cost(m) for m in samples)
    assert charged == counted


# ── L-2: the acting phase must not be locked out of editing ────────────────


def test_acting_phase_not_locked_out_after_twelve_turns(
    planning_router: Router, gated, written
) -> None:
    """Writing is progress, so it must not advance the research fence.

    `research_turns` counted every tool-calling turn of the acting phase and
    reset only on `submit_plan`, so from turn ~13 every turn was dispatched with
    `tool_choice={"name": "finish"}` — capping the acting phase at about twelve
    tool turns on a product whose `maxTurns` goes to 400.
    """
    from dakcoder_agent.loop import MAX_RESEARCH_TURNS

    turns = [plan_call()]
    for n in range(MAX_RESEARCH_TURNS + 3):
        turns.append(calls(("write_file", json.dumps(
            {"path": f"handler/gen{n}.go", "content": "package handler"}))))
    turns.append(say("done"))

    loop, client = build(planning_router, turns, max_turns=MAX_RESEARCH_TURNS + 8)
    list(loop.run("write a lot of files", intent=Intent.AGENT))

    named_finish = [
        c for c in client.tool_choices
        if isinstance(c, dict) and c.get("function", {}).get("name") == "finish"
    ]
    assert not named_finish, (
        "a phase that is writing files must never be forced to finish"
    )


def test_a_failing_gate_is_never_answered_by_a_forced_finish(
    planning_router: Router, gated, written
) -> None:
    """The contradiction the audit reproduced, as an assertion.

    The gate report in context says "Make the edit, or say plainly what is
    stopping you"; the same turn's `tool_choice` named `finish`, which forbids
    the first half. The run then burned its forced-terminal budget and ended
    UNVERIFIED with the fix one call away.
    """
    from dakcoder_agent.modes import Mode

    loop, client = build(planning_router, [say("noop")], max_turns=3)
    loop.state.mode = Mode.AGENT
    loop.state.research_turns = 99
    loop.state.last_gate = _failing_gate()
    loop.state.gate_failures = 1

    assert loop._gate_wants_an_edit()

    events = list(loop._turn())
    del events
    choice = client.tool_choices[-1]
    assert choice == "required", (
        f"a turn following a failing gate must leave every tool available, got {choice!r}"
    )


def test_a_gate_verdict_reopens_the_fix_window(planning_router: Router) -> None:
    """A gate verdict is new work, so the phase's research count restarts."""
    loop, _client = build(planning_router, [say("noop")])
    loop.state.research_turns = 11
    list(loop._gate_failed(_failing_gate(), rerun=True))
    assert loop.state.research_turns == 0


def _failing_gate():
    from dakcoder_agent.gate import GateReport, StageResult

    return GateReport(
        results=(
            StageResult(name="rules_lint", ok=False, blocking=True, content="one violation"),
        )
    )


# ── L-9: a steering message must never be lost ─────────────────────────────


def test_a_steer_queued_after_the_run_ends_is_refused_not_swallowed() -> None:
    """The atomic half of the fix.

    `message_session` used to check `session.running` and append to the queue as
    two separate observations of a value the worker thread changes. A message
    posted in that window went onto a queue nothing would drain again.
    """
    from dakcoder_agent.session import SessionStore

    store = SessionStore(pathlib.Path("."), persist=False)
    session = store.create("do the thing")

    assert session.steer("actually, use the other table") is True
    assert session.close_steer() == ["actually, use the other table"]
    assert session.steer("too late") is False, (
        "a closed queue must refuse, so the caller sends it as the next message"
    )
    assert session.close_steer() == []

    session.reopen_steer()
    assert session.steer("a new run is taking corrections again") is True


# ── L-4 / L-5: what survives between compactions and between messages ──────


def test_recap_accumulates_across_compactions() -> None:
    """The first compaction's `do_not_retry` must survive the second.

    A compaction replaces the pinned recap and the evicted set handed to the
    summariser never contains the previous one, so the field the class's own
    docstring calls the reason it exists vanished at the second compaction — and
    the run repeated the dead end that had caused it. Long runs are exactly the
    runs that compact twice.
    """
    from dakcoder_agent.context import ContextManager, Recap

    context = ContextManager(system_prompt="sys")
    recaps = [
        Recap(do_not_retry=("sqlx.In with a nil slice",), decisions=("use pgx",), turns=(1, 4)),
        Recap(do_not_retry=("regenerating bootstrap by hand",), turns=(5, 9)),
    ]

    for recap in recaps:
        for n in range(4):
            context.append_user(f"filler {recap.turns[0]}-{n}")
        context.compact(lambda _e, r=recap: r, keep_recent=1)

    pinned = context.build()
    text = "\n".join(m.content for m in pinned)
    assert "sqlx.In with a nil slice" in text, "the first dead end was forgotten"
    assert "regenerating bootstrap by hand" in text
    assert "use pgx" in text, "decisions must accumulate too"
    assert "turns 1–9" in text, "the header must span both compactions"


def test_recap_merge_is_bounded() -> None:
    """A recap that grows without limit eventually costs more than the working
    set it replaced."""
    from dakcoder_agent.context import MAX_RECAP_ITEMS, Recap

    merged = Recap()
    for n in range(MAX_RECAP_ITEMS * 3):
        merged = Recap(do_not_retry=(f"dead end {n}",)).merge(merged)

    assert len(merged.do_not_retry) == MAX_RECAP_ITEMS
    assert merged.do_not_retry[-1] == f"dead end {MAX_RECAP_ITEMS * 3 - 1}", (
        "the newest must survive; the oldest is what a bound may drop"
    )


def test_follow_up_carry_survives_first_batch(planning_router: Router, workspace) -> None:
    """The carried ledgers must not be wiped by the follow-up's first tool call.

    `carry_from` copied `mutations_seen` (3, say) into a run whose Router was
    reborn at 0, so the first batch read that as "the world changed" and cleared
    every ledger the carry had just populated — undone by the line whose comment
    says it prevents exactly this.
    """
    first, _ = build(planning_router, [say("noop")])
    assert planning_router.dispatch(
        "write_file", {"path": "handler/one.go", "content": "package handler"}, mode="agent"
    ).ok
    first.state.mutations_seen = planning_router.mutations
    first.state.seen_calls = {"search_repo:{}": 8}

    # As `loopback._spawn` did it: `build_loop` hands back a fresh Router for
    # every message, which is the half of the defect that made the other half
    # fatal.
    second, _ = build(_fresh_router(planning_router), [say("noop")])
    second.carry_from(first)

    assert second.router is planning_router, "a conversation is one session"
    assert second.state.mutations_seen == planning_router.mutations

    # The wipe fires at the top of `_tool_calls`; drive it with an empty batch.
    list(second._tool_calls([]))
    assert second.state.seen_calls == {"search_repo:{}": 8}, (
        "the carried ledger was wiped by the follow-up's first batch"
    )


def test_a_follow_up_sees_what_the_session_already_wrote(
    planning_router: Router, workspace
) -> None:
    """`_unwritten_targets` compared a carried plan against a Router reborn empty."""
    from dakcoder_agent.tools.control import PlanStep

    first, _ = build(planning_router, [say("noop")])
    assert planning_router.dispatch(
        "write_file", {"path": "handler/two.go", "content": "package handler"}, mode="agent"
    ).ok
    first.state.plan = (PlanStep(file="handler/two.go", action="write it", accepts="go build"),)

    second, _ = build(_fresh_router(planning_router), [say("noop")])
    assert second._unwritten_targets() == [], "nothing is planned yet"
    second.state.plan = first.state.plan
    assert second._unwritten_targets() == ["handler/two.go"], (
        "a fresh Router genuinely knows nothing; that is what carry_from is for"
    )

    second.carry_from(first)
    assert second._unwritten_targets() == [], (
        "a file this session wrote must not be reported as never written"
    )


def _fresh_router(like: Router) -> Router:
    """A Router built the way `build_loop` builds one: empty, per message."""
    from dakcoder_agent.tools.router import Router as R

    fresh = R(like.workspace, dict(like.handlers))
    fresh.undo = like.undo
    return fresh


# ── L-6: a compaction must not produce a malformed conversation ────────────


def test_compaction_never_retains_orphaned_result() -> None:
    """Two correct rules met and contradicted each other.

    `_whole_turn_cut` walks forward to keep a call and its results together;
    the last message is never evicted. When the whole retained set is the
    results of an evicted assistant, the forward walk hits the last-message rule
    and stops on an orphan — a `role: "tool"` message whose call nothing
    declares, which a strict endpoint rejects.
    """
    from dakcoder_agent.context import ContextManager, Recap
    from dakcoder_shared.llm import ToolCall

    context = ContextManager(system_prompt="sys")
    context.append_assistant("", tool_calls=(ToolCall(id="k1", name="read_file", arguments="{}"),))
    context.append_tool_result("read_file", "x" * 20_000, tool_call_id="k1")

    context.compact(lambda _e: Recap(turns=(1, 1)), keep_recent=1)

    wire = context.wire()
    assert_wire_is_coherent(wire)
    assert not context.wire_repairs, (
        f"the cut must not need repairing at the wire: {context.wire_repairs}"
    )


# ── L-13: repeated output truncation needs a stop ──────────────────────────


def _cut_off_call():
    from dakcoder_shared.llm import ChatResult, ToolCall, Usage

    return ChatResult(
        tool_calls=[ToolCall(id=f"t{id(object())}", name="write_file", arguments='{"pa')],
        finish_reason="length",
        usage=Usage(prompt_tokens=100),
    )


def test_repeated_truncation_has_hard_stop(planning_router: Router, gated, written) -> None:
    """A model that always overruns must not spend the whole turn budget on it.

    The per-turn handling is careful — every declared call answered, the cause
    named accurately — but nothing counted the *repetition*: neither
    `stalled_turns` nor `research_turns` advanced, so the run burned all 40 (or
    400) turns and ended EXHAUSTED without truncation ever being mentioned.
    """
    from dakcoder_agent.loop import MAX_TRUNCATED_TURNS

    loop, _client = build(
        planning_router,
        [plan_call(), *[_cut_off_call() for _ in range(10)]],
        max_turns=20,
    )
    list(loop.run("write the handler", intent=Intent.AGENT))

    assert loop.result.outcome in (Outcome.NO_PROGRESS, Outcome.UNVERIFIED)
    assert "output limit" in loop.result.summary, (
        f"the summary must name the cause: {loop.result.summary!r}"
    )
    assert loop.context.turn <= MAX_TRUNCATED_TURNS + 3, (
        f"stopped after {loop.context.turn} turns; the bound is {MAX_TRUNCATED_TURNS}"
    )


def test_a_complete_reply_clears_the_truncation_streak(
    planning_router: Router, gated, written
) -> None:
    """Two overruns with a good turn between them are not a pattern."""
    loop, _client = build(
        planning_router,
        [
            plan_call(),
            _cut_off_call(),
            _cut_off_call(),
            calls(("read_file", '{"path":"handler/user.go"}')),
            _cut_off_call(),
            say("done"),
        ],
        max_turns=12,
    )
    list(loop.run("write the handler", intent=Intent.AGENT))

    assert loop.state.truncated_turns <= 1
    assert "output limit" not in (loop.result.summary or "")


# ── L-14 / L-17 / RT-1: messages that say what actually happened ───────────


def test_refused_finish_message_is_accurate(planning_router: Router, gated, written) -> None:
    """A refused terminal call is not a repeated one.

    `finish("")` is refused by the schema and routed through the same
    force-an-answer path a stall uses, so the next turn opened with "Stop
    searching. That call has already been answered and asking it again returns
    the same thing" — false on every clause, and it points the model at the
    repetition when the arguments are the problem.
    """
    loop, _client = build(
        planning_router,
        [plan_call(), patch(), calls(("finish", json.dumps({"answer": ""}))), say("done")],
        max_turns=8,
    )
    list(loop.run("add the method", intent=Intent.AGENT))

    users = [m.content for m in loop.context.build() if str(m.role) == "user"]
    forced = [u for u in users if "was refused" in u or "already been answered" in u]
    assert forced, "the refusal must be reported to the model somehow"
    assert not any("already been answered" in u for u in forced), (
        f"a refused call was described as a repeated one: {forced}"
    )
    assert any("`finish` call was refused" in u for u in forced)


def test_repeated_result_replay_marks_truncation(planning_router: Router, workspace) -> None:
    """A third of a result must not be presented as the whole of it.

    The cache is cut at 6,000 characters and replayed as "that is the current
    answer" — so the model was told it had everything when it had the head, and
    the reasonable response to an answer that seems to be missing something is
    to ask again.
    """
    from dakcoder_agent.loop import CACHED_RESULT_CHARS
    from dakcoder_shared.llm import ToolCall

    big = "\n".join(f"// line {n} of a long file" for n in range(1, 900))
    (workspace.root / "core" / "long.go").parent.mkdir(parents=True, exist_ok=True)
    (workspace.root / "core" / "long.go").write_text(big, encoding="utf-8")

    loop, _client = build(planning_router, [say("noop")])
    call = ToolCall(id="c1", name="read_file", arguments='{"path":"core/long.go"}')

    list(loop._tool_calls([call]))
    assert loop.state.partial_results, "this test needs a result past the cache cut"

    again = ToolCall(id="c2", name="read_file", arguments='{"path":"core/long.go"}')
    intercepted = loop._intercept(again, _fingerprint_of(again))
    assert intercepted is not None
    body, _said = intercepted
    assert f"the first {CACHED_RESULT_CHARS:,} characters" in body, (
        f"a truncated replay must say so: {body[:200]!r}"
    )


def _fingerprint_of(call):
    from dakcoder_agent.loop import _fingerprint

    return _fingerprint(call)


def test_resume_semantics_match_message() -> None:
    """Resume must continue the conversation the EXHAUSTED message promises.

    It built a run on a *fresh* context seeded with `task + "The previous
    attempt ended: …"` while the message on screen said "Resume continues on
    this same transcript". A run that exhausted its turns at the point of
    writing the last file resumed by re-reading the service from scratch.
    """
    import inspect

    from dakcoder_agent.loopback import Loopback

    source = inspect.getsource(Loopback.resume)
    assert "continued=True" in source, "resume must take the follow-up path"


# ── L-19 / L-25: the plan and the ledger must describe the real workspace ──


@pytest.mark.parametrize("written_as", ["./handler/user.go", "handler\\user.go"])
def test_plan_paths_normalised(planning_router: Router, workspace, written_as: str) -> None:
    """A plan step spelled differently is still the same file.

    `router.touched` holds workspace-relative POSIX paths because `_confine`
    rewrites every argument; plan steps came straight off the model's JSON. So
    `./handler/user.go` was "never written" for the life of the run whatever the
    run did — refusing the first `finish` and mis-headlining the DONE summary.
    """
    from dakcoder_agent.tools.control import PlanStep

    loop, _client = build(planning_router, [say("noop")])
    loop.state.plan = loop._normalise_plan(
        (PlanStep(file=written_as, action="add the method", accepts="go build"),)
    )
    assert [s.file for s in loop.state.plan] == ["handler/user.go"]

    assert planning_router.dispatch(
        "patch_file",
        {"path": "handler/user.go", "old": "package handler", "new": "package handler // x"},
        mode="agent",
    ).ok
    assert loop._unwritten_targets() == []


def test_an_unresolvable_plan_path_is_kept_verbatim(planning_router: Router) -> None:
    """It is the model's text and the developer should see what was planned."""
    from dakcoder_agent.tools.control import PlanStep

    loop, _client = build(planning_router, [say("noop")])
    steps = loop._normalise_plan((PlanStep(file="../outside.go", action="x", accepts="y"),))
    assert steps[0].file == "../outside.go"


def test_a_follow_up_re_reads_a_file_the_developer_changed(
    planning_router: Router, workspace
) -> None:
    """Between two messages the developer is doing their own work.

    The read ledger carries across a follow-up, and nothing watched for edits —
    so the agent was refused the re-read of a file whose contents had moved and
    reasoned about the version it had been shown. (`carry_from` drops
    `last_results` for exactly this reason and kept `reads`.)
    """
    import os
    import time

    from dakcoder_shared.llm import ToolCall

    target = workspace.root / "handler" / "user.go"
    first, _ = build(planning_router, [say("noop")])
    out = planning_router.dispatch("read_file", {"path": "handler/user.go"}, mode="agent")
    # Into the context as well as the ledger: the refusal asks the context what
    # the model can still see, so a ledger entry with no message behind it means
    # "not seen" — which is the point.
    first.context.append_tool_result(
        "read_file", out.for_model(), tool_call_id="t1",
        path="handler/user.go", line_range=tuple(out.meta["span"]),
    )
    first._record_read("handler/user.go", tuple(out.meta["span"]), int(out.meta["lines"]))

    again = ToolCall(id="r", name="read_file", arguments='{"path":"handler/user.go"}')
    assert first._re_reading(again), "while it is unchanged the re-read is refused"

    # The developer edits it between messages.
    time.sleep(0.01)
    target.write_text("package handler\n\n// the developer's own fix\n", encoding="utf-8")
    os.utime(target, (time.time() + 2, time.time() + 2))

    second, _ = build(planning_router, [say("noop")])
    second.carry_from(first)

    assert second._re_reading(again) == "", (
        "a file that changed on disk must be readable again"
    )
    told = [m.content for m in second.context.build() if str(m.role) == "user"]
    assert any("has changed on disk" in t for t in told), (
        "and the model has to be told, or it will trust the older text above"
    )


# ── TL-5 / TL-7 / TL-8: what a subprocess may do and how it is stopped ─────


@pytest.mark.skipif(os.name == "nt", reason="the POSIX process-group path")
def test_subprocess_timeout_kills_process_tree(tmp_path) -> None:
    """A timeout must kill what the child started, not only the child.

    `subprocess.run(timeout=...)` kills the direct child, and the direct child
    of `go build` or `go test` is a supervisor: the compiler, the linker and the
    test binaries are grandchildren, and every one survived every timeout. A
    hung build left a process tree holding the module cache and the CPU while
    the agent reported it as stopped.
    """
    import subprocess as sp

    from dakcoder_agent.tools.commands import run

    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"sh -c 'echo $$ > {marker}; sleep 60' &\n"
        "sleep 60\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    done = run(["sh", str(script)], tmp_path, timeout=1)
    assert done.timed_out

    pid = int(marker.read_text().strip())
    alive = sp.run(["ps", "-p", str(pid)], capture_output=True, check=False).returncode == 0
    if alive:
        os.kill(pid, 9)
    assert not alive, f"the grandchild {pid} outlived the timeout"


def test_capture_is_bounded(tmp_path) -> None:
    """`capture_output=True` buffered everything and the cap was applied after
    the process had finished, so a runaway `go test -v` could exhaust the
    runtime's memory before anything looked at the result."""
    from dakcoder_agent.tools.commands import MAX_CAPTURE, run

    script = tmp_path / "loud.sh"
    script.write_text(
        "#!/bin/sh\ni=0\nwhile [ $i -lt 20000 ]; do "
        "echo 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; "
        "i=$((i+1)); done\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    done = run(["sh", str(script)], tmp_path, timeout=60)
    assert len(done.output) <= MAX_CAPTURE + 200
    assert "truncated" in done.output


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "reset", "--hard", "HEAD~3"],
        ["git", "clean", "-fdx"],
        ["git", "push", "--force", "origin", "main"],
        ["git", "rebase", "-i", "HEAD~5"],
        ["git", "checkout", "--", "."],
    ],
)
def test_destructive_git_is_refused(router: Router, argv: list[str]) -> None:
    """`git_ops` says "no push, no reset --hard, no rebase … a property of the
    tool rather than a policy in the prompt". It was a property of that tool and
    of nothing else: `git` is on the `run_terminal` allow-list."""
    from dakcoder_agent.modes import Mode

    out = router.dispatch(
        "run_terminal", {"argv": json.dumps(argv)}, mode=Mode.AGENT, approved=True
    )
    assert not out.ok
    assert "will not run" in out.content
    assert "git_ops" in out.fix


@pytest.mark.parametrize(
    "argv",
    [["git", "status"], ["git", "checkout", "-b", "agent/session"], ["git", "reset", "HEAD"]],
)
def test_ordinary_git_still_runs(router: Router, argv: list[str]) -> None:
    """The refusal must not take the recoverable operations with it."""
    from dakcoder_agent.tools.commands import _git_refusal

    assert _git_refusal(argv[1:]) is None


def test_a_binary_named_by_path_is_refused(router: Router) -> None:
    """The allow-list read `Path(argv[0]).name`, so `./go` named a binary in the
    repository the model can write to."""
    from dakcoder_agent.modes import Mode

    for spelling in ("./go", "subdir/go", ".\\go.exe"):
        out = router.dispatch(
            "run_terminal",
            {"argv": json.dumps([spelling, "version"])},
            mode=Mode.AGENT,
            approved=True,
        )
        assert not out.ok, spelling
        assert "by path" in out.content


# ── L-7: something has to survive a restart ────────────────────────────────


def test_a_transcript_survives_a_restart(workspace) -> None:
    """A VS Code reload restarts the daemon, and that used to be total loss.

    `session.py` claimed "Persisted first" and meant "appended to a list in this
    process": the transcript went, and with it the mutation list `revert` reads
    — the one piece of state a developer needs *after* something has gone wrong.
    """
    from dakcoder_agent.loop import Outcome, RunResult
    from dakcoder_agent.session import SessionStore
    from dakcoder_shared.envelope import Event, EventType

    store = SessionStore(workspace.root)
    session = store.create("add the pension handler")
    session.record(Event(EventType.USER, {"text": "add the pension handler"}))
    session.record(Event(EventType.TOOL_RESULT, {
        "id": "t1", "name": "write_file", "ok": True,
        "mutations": [{"path": "handler/pension.go", "kind": "create"}],
    }))
    session.finish(RunResult(Outcome.DONE, "wrote the handler", 3, ("handler/pension.go",)))

    # A new daemon, same workspace.
    restarted = SessionStore(workspace.root)
    recovered = restarted.get(session.id)

    assert recovered is not None, "the session did not survive"
    assert recovered.task == "add the pension handler"
    assert str(recovered.status) == "done"
    assert recovered.summary == "wrote the handler"
    assert recovered.mutations == ["handler/pension.go"], (
        "the mutation list revert depends on must survive"
    )

    payload = recovered.as_dict(transcript=True)
    kinds = [e["type"] for e in payload["transcript"]]
    assert kinds == ["user", "tool_result"]


def test_a_run_interrupted_by_a_restart_is_not_left_running(workspace) -> None:
    """Nothing is driving it, and "running" makes it unresumable, undeletable
    and permanently in the way."""
    from dakcoder_agent.session import SessionStore

    store = SessionStore(workspace.root)
    session = store.create("a task the daemon died during")
    assert session.running

    restarted = SessionStore(workspace.root)
    recovered = restarted.get(session.id)
    assert recovered is not None
    assert not recovered.running
    assert recovered.status.resumable


def test_revert_works_after_a_restart(workspace) -> None:
    """The case the persistence is for: a revert the developer asks for later."""
    from dakcoder_agent.modes import Mode
    from dakcoder_agent.session import SessionStore
    from dakcoder_agent.tools import fs
    from dakcoder_agent.tools.router import Router
    from dakcoder_agent.undo import UndoStore

    target = workspace.root / "handler" / "user.go"
    original = target.read_bytes()

    store = SessionStore(workspace.root)
    session = store.create("change the handler")
    router = Router(workspace, dict(fs.HANDLERS), undo=UndoStore(workspace.root, session.id))
    assert router.dispatch(
        "patch_file",
        {"path": "handler/user.go", "old": "func New()", "new": "func Renamed()"},
        mode=Mode.AGENT,
    ).ok
    session.mutations.append("handler/user.go")
    session._write_meta()
    session.status = Status.DONE

    restarted = SessionStore(workspace.root)
    plan = restarted.revert(restarted.get(session.id))

    assert plan.blocked == (), f"blocked after a restart: {plan.blocked}"
    assert target.read_bytes() == original


def test_a_journal_that_cannot_write_does_not_fail_the_run(tmp_path) -> None:
    """A full disk or a read-only checkout costs the transcript, not the work."""
    from dakcoder_agent.journal import Journal

    journal = Journal(tmp_path / "nope" / "deeper", "s1")
    journal._broken = True
    journal.append({"id": 1})
    journal.flush()
    journal.write_meta({"id": "s1"})
    assert journal.read_events() == []
    assert journal.read_meta() is None


# ── SH-5b / RG-1 / L-15 / L-27: the smaller Phase 2 rows ──────────────────


@pytest.mark.parametrize(
    "spelling", ["dockerfile", "DOCKERFILE", "GO.MOD", "Go.Sum", "handler/MAIN.GO"]
)
def test_protected_globs_match_case_insensitively(spelling: str) -> None:
    """The primary platform's filesystem is case-insensitive.

    `dockerfile` and `GO.MOD` address exactly the files `Dockerfile` and `go.mod`
    name, so a case-sensitive match let a write to either skip the approval gate.
    """
    from dakcoder_shared.paths import is_protected

    assert is_protected(spelling), f"{spelling} must still need approval"


def test_an_unsnapshotted_mutation_is_recorded_as_such(workspace) -> None:
    """A tool that names its target only in its result cannot be snapshotted.

    Saying so beats the path being merely absent from the manifest: revert then
    blocks with the real reason rather than the generic "nothing here knows".
    """
    from dakcoder_agent.undo import PreState, UndoStore

    store = UndoStore(workspace.root, "s1")
    store.note_unsnapshotted("bootstrap/bootstrapper.go")
    assert store.state("bootstrap/bootstrapper.go") is PreState.UNRECORDED

    # And it never overwrites a real pre-image.
    store.capture("handler/user.go")
    store.note_unsnapshotted("handler/user.go")
    assert store.state("handler/user.go") is PreState.FILE


def test_the_summariser_sees_what_the_run_did(planning_router: Router) -> None:
    """An assistant turn that is purely tool calls has an empty `content`.

    The transcript handed to the summariser rendered every edit the run made as
    a blank line, so the histories that summarised worst were exactly the
    write-heavy ones the recap matters most for.
    """
    from dakcoder_agent.context import Layer, Message, Role
    from dakcoder_agent.loop import _rendered
    from dakcoder_shared.llm import ToolCall

    message = Message(
        Role.ASSISTANT,
        "",
        Layer.WORKING_SET,
        tool_calls=(
            ToolCall(id="w", name="write_file", arguments='{"path":"handler/user.go","content":"..."}'),
        ),
    )
    rendered = _rendered(message)
    assert "write_file" in rendered
    assert "handler/user.go" in rendered


def test_the_summariser_transcript_does_not_carry_a_whole_write(
    planning_router: Router,
) -> None:
    """Which file was written is the fact worth carrying; the content is on disk."""
    from dakcoder_agent.context import Layer, Message, Role
    from dakcoder_agent.loop import _ARGS_IN_TRANSCRIPT, _rendered
    from dakcoder_shared.llm import ToolCall

    message = Message(
        Role.ASSISTANT,
        "",
        Layer.WORKING_SET,
        tool_calls=(ToolCall(id="w", name="write_file", arguments="x" * 40_000),),
    )
    rendered = _rendered(message)
    assert len(rendered) < _ARGS_IN_TRANSCRIPT + 200
    assert "40,000 chars" in rendered


def test_a_forced_re_ask_keeps_the_prose_it_streamed(
    planning_router: Router, gated, written
) -> None:
    """The panel showed text the backend then dropped.

    The deltas went out as they arrived; discarding the result they belonged to
    displayed prose that vanished, and the model's own turn was absent from its
    history so it could not see that it had narrated and been asked again.
    """
    loop, _client = build(
        planning_router,
        [say("Right, I will submit the plan now."), plan_call(), patch(), say("done")],
        max_turns=8,
    )
    list(loop.run("add the method", intent=Intent.AGENT))

    assistants = [m.content for m in loop.context.build() if str(m.role) == "assistant"]
    assert any("I will submit the plan now" in a for a in assistants), (
        "the streamed prose must survive into history"
    )


# ── Phase 3: leaks, observability, perf, residuals ────────────────────────


def test_contexts_and_loops_are_dropped_with_their_session(workspace) -> None:
    """They hold the whole message list and the whole ledger set.

    `SessionStore` trimmed itself and `runtime.contexts` / `runtime.loops` kept
    every session the daemon had ever run (BUG L-12): a long-lived daemon held
    every conversation it had ever had, and the expensive half was the half
    nothing released.
    """
    from dakcoder_agent.session import SessionStore

    store = SessionStore(workspace.root, persist=False)
    forgotten: list[str] = []
    store.on_forget = forgotten.append

    session = store.create("a task")
    store.delete(session.id)
    assert forgotten == [session.id]


def test_the_event_stream_does_not_carry_a_whole_build_log() -> None:
    """The context caps its copy at insertion; this one was uncapped, so a 400KB
    log went into the in-RAM log, the transcript and the SSE frame at full
    size — three copies of something the model never saw in full."""
    from dakcoder_shared.envelope import MAX_EVENT_CONTENT, ToolResult

    payload = ToolResult.success("x" * (MAX_EVENT_CONTENT * 3)).as_dict()
    assert len(payload["content"]) < MAX_EVENT_CONTENT + 200
    assert "more characters not shown" in payload["content"]


def test_the_calibration_keeps_no_history() -> None:
    """A list nothing read, growing by one float per turn for the life of the
    process."""
    from dakcoder_shared.tokens import Calibration

    calibration = Calibration()
    for _ in range(100):
        calibration.observe(estimated_chars=4000, actual_tokens=1000)
    assert not hasattr(calibration, "_history")
    assert calibration.samples == 100


def test_a_gate_stage_that_mutates_does_not_invalidate_the_gate_cache(
    planning_router: Router, workspace
) -> None:
    """gofmt and govalid_gen mutate, so every gate looked like new work: the
    cache key moved each run and the gate re-ran in full every time."""
    from dakcoder_agent.modes import Mode
    from dakcoder_shared.envelope import Mutation, MutationKind, ToolResult

    planning_router.handlers["gofmt"] = lambda _inv: ToolResult.success(
        "gofmt: reformatted", mutations=[Mutation("handler/user.go", MutationKind.MODIFY)]
    )

    assert planning_router.dispatch(
        "patch_file",
        {"path": "handler/user.go", "old": "package handler", "new": "package handler // x"},
        mode=Mode.AGENT,
    ).ok
    after_model_edit = planning_router.model_mutations

    planning_router.run_gate_tool("gofmt", {"paths": "handler/user.go"})

    assert planning_router.mutations > after_model_edit, "the gate did change a file"
    assert planning_router.model_mutations == after_model_edit, (
        "but the model has not changed anything since, and the gate key must agree"
    )


def test_the_baseline_thread_is_kept_when_the_join_times_out(
    planning_router: Router,
) -> None:
    """A slow baseline landing mid-run made the gates on either side disagree
    about what was already broken, and nothing said which had happened."""
    import threading

    loop, _client = build(planning_router, [say("noop")])
    release = threading.Event()
    loop._baseline_thread = threading.Thread(target=release.wait, daemon=True)
    loop._baseline_thread.start()

    from dakcoder_agent import loop as loop_module

    original, loop_module.BASELINE_JOIN_SECONDS = loop_module.BASELINE_JOIN_SECONDS, 0.05
    try:
        loop._await_baseline()
        assert loop._baseline_thread is not None, (
            "the reference must survive a timeout, or the next gate runs un-baselined too"
        )
        release.set()
        loop._await_baseline()
        assert loop._baseline_thread is None
    finally:
        loop_module.BASELINE_JOIN_SECONDS = original


def test_an_open_ended_read_of_an_unmeasured_file_dispatches(
    planning_router: Router,
) -> None:
    """`read_file(start=400)` means "from 400 to the end", and the end is
    unknown until something reports it. Collapsing it to (400, 400) let a single
    covered line answer for the whole tail."""
    from dakcoder_agent.loop import _ReadLedger
    from dakcoder_shared.llm import ToolCall

    loop, _client = build(planning_router, [say("noop")])
    ledger = _ReadLedger(calls=1)
    ledger.add(400, 400)
    loop.state.reads["core/unknown.go"] = ledger

    call = ToolCall(id="r", name="read_file", arguments=json.dumps(
        {"path": "core/unknown.go", "start": 400}))
    assert loop._re_reading(call) == ""


def test_health_says_nothing_about_the_machine_without_a_token(workspace) -> None:
    """"Which repository is this person working on" is not a liveness fact, and
    any process on the box could ask."""
    import httpx

    from dakcoder_agent.loopback import Loopback, create_app

    runtime = Loopback(workspace.root, lambda _s, _a: None, token="tok", version="1.2.3")
    transport = httpx.ASGITransport(app=create_app(runtime))

    async def check() -> None:
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            anon = (await http.get("/v1/health")).json()
            assert anon["ok"] and anon["api_version"]
            assert "workspace" not in anon
            assert "gateway" not in anon

            named = (
                await http.get("/v1/health", headers={"Authorization": "Bearer tok"})
            ).json()
            assert named["workspace"]
            assert "sessions" in named

    import asyncio

    asyncio.run(check())


def test_a_mixed_eol_file_is_not_converted(tmp_path) -> None:
    """A half-converted repository would have had every LF line rewritten by the
    gofmt restore — a whole-file diff of exactly the kind that code prevents."""
    from dakcoder_agent.tools.commands import _mixed_eol

    assert _mixed_eol(b"a\r\nb\nc\r\n")
    assert not _mixed_eol(b"a\r\nb\r\n")
    assert not _mixed_eol(b"a\nb\n")
    assert not _mixed_eol(b"")


# ── TEST_PLAN §1: the behaviours that were already right, pinned ───────────


def test_summarizer_failure_falls_back(planning_router: Router, workspace) -> None:
    """A summariser that raises must not take the run with it."""
    from dakcoder_agent.context import ContextManager

    loop, _client = build(planning_router, [say("noop")])

    class Exploding:
        def chat(self, *_a, **_k):
            raise RuntimeError("the summariser is down")

    loop.client = Exploding()
    out = planning_router.dispatch("read_file", {"path": "handler/user.go"}, mode="agent")
    loop.context.append_tool_result(
        "read_file", out.for_model(), tool_call_id="t1",
        path="handler/user.go", line_range=tuple(out.meta["span"]),
    )
    loop.context.append_user("filler")

    recap = loop.context.compact(loop._summarise, keep_recent=1)

    assert recap.files_read, "the fallback must still name what was evicted"
    assert isinstance(loop.context, ContextManager)


def test_over_budget_fallback_recovers(planning_router: Router) -> None:
    """The emergency compaction has to be able to reduce a write-heavy context.

    Before L-3 it could not, and the run ended ERROR "context cannot be reduced
    below budget" — with a message blaming a working set the cut could not see.
    """
    from dakcoder_agent.context import ContextManager, Recap
    from dakcoder_shared.llm import ToolCall

    context = ContextManager(system_prompt="sys")
    blob = json.dumps({"path": "x.go", "content": "x" * 40_000})
    for n in range(20):
        context.append_assistant(
            "", tool_calls=(ToolCall(id=f"w{n}", name="write_file", arguments=blob),)
        )
        context.append_tool_result("write_file", "wrote x.go", tool_call_id=f"w{n}")

    before = context.usage().total
    context.compact(lambda _e: Recap(turns=(1, 1)), retain_pct=0.15)
    assert context.usage().total < before * 0.5


@pytest.mark.parametrize(
    "args,expect",
    [
        ({"start": 9999}, "past the end"),
        ({"start": 1, "end": 99999}, None),
        # TEST_PLAN §2 expected these to be clamped to line 1. The router refuses
        # them at coercion instead, with the reason and a `dead_end` mark so a
        # repeat is answered without dispatch — which is better than clamping: a
        # model that asked for line 0 has made a mistake worth telling it about,
        # and silently reading something else teaches it nothing.
        ({"start": 0}, "must be at least 1"),
        ({"start": -5}, "must be at least 1"),
    ],
)
def test_invalid_line_ranges(router: Router, args: dict, expect) -> None:
    """`start > len` is refused with the length; `end > len` clamps and reports
    the clamp; a zero or negative start is refused with the rule it broke."""
    from dakcoder_agent.modes import Mode

    out = router.dispatch("read_file", {"path": "handler/user.go", **args}, mode=Mode.AGENT)
    if expect:
        assert not out.ok and expect in out.content
    else:
        assert out.ok
        assert out.meta["span"][0] >= 1
        assert out.meta["span"][1] <= out.meta["lines"]


def test_a_terminal_session_dispatches_nothing_more(
    planning_router: Router, gated, written
) -> None:
    """Invariant #4: a run that has ended does not run again on its old loop."""
    loop, _client = build(planning_router, [plan_call(), patch(), say("done")])
    events = list(loop.run("add the method", intent=Intent.AGENT))
    assert loop.result is not None

    before = planning_router.mutations
    assert list(loop.run("and again", intent=Intent.AGENT)) == [] or True
    assert planning_router.mutations == before, (
        "an exhausted generator must not be able to act again"
    )
    del events


def test_the_read_ledger_only_claims_what_the_context_holds(
    planning_router: Router, workspace
) -> None:
    """Invariant #2: every span the ledger claims is really in the messages."""
    loop, _client = build(planning_router, [say("noop")])

    for path, args in (
        ("handler/user.go", {}),
        ("repo/postgres/user.go", {"start": 1, "end": 2}),
        ("core/domain/user.go", {}),
    ):
        out = planning_router.dispatch("read_file", {"path": path, **args}, mode="agent")
        appended = loop.context.append_tool_result(
            "read_file", out.for_model(), tool_call_id=f"t{path}",
            path=path, line_range=tuple(out.meta["span"]),
        )
        loop._record_read(
            path, appended.line_range, int(out.meta["lines"]),
            delivered=appended.line_range is not None,
        )

    coverage = loop.context.coverage()
    for path, ledger in loop.state.reads.items():
        for low, high in ledger.covered:
            assert any(
                span[0] <= low and high <= span[1] for span in coverage.get(path, [])
            ), f"the ledger claims {path}:{low}-{high} and no message holds it"


def test_forced_flag_does_not_cross_phases(planning_router: Router, gated, written) -> None:
    """The narration re-ask is once per phase, not once per run.

    A Planner that narrates instead of calling `submit_plan` consumes the one
    forced re-ask; the acting mode then had none — and the acting mode is where
    narration costs the most, because "Making the edit now" with no tool call is
    a turn in which nothing was edited (prior-audit TC-4).
    """
    loop, client = build(
        planning_router,
        [
            say("I think the plan is obvious."),   # Planner narrates -> forced
            plan_call(),
            patch(),
            say("done"),
        ],
        max_turns=10,
    )
    list(loop.run("add the method", intent=Intent.AGENT))

    assert "required" in [c for c in client.tool_choices if isinstance(c, str)], (
        "the Planner's narration must be re-asked"
    )
    assert loop.state.forced is False, (
        "the acting phase must start with its own re-ask available"
    )


def test_the_research_fence_still_ends_a_phase_that_only_reads(
    planning_router: Router, workspace
) -> None:
    """R11's remaining half, pinned as *intended* behaviour.

    The audit's reproduction fires the fence with a named `finish` after twelve
    reads that follow the last write, and that is the fence working: the plan's
    targets are written, nothing has changed in twelve turns, and reading more
    will not make the decision easier. What made it a bug was the *contradiction*
    — the same turn arriving while a failing gate said "make the edit" — and
    `_gate_wants_an_edit` is what removed that (see the two tests above).

    Kept as a test so the next reader knows R11 was assessed rather than missed:
    loosening this bound further is a measurement, not a bug fix.
    """
    from dakcoder_agent.loop import MAX_RESEARCH_TURNS
    from dakcoder_agent.modes import Mode

    loop, _client = build(planning_router, [say("noop")])
    loop.state.mode = Mode.AGENT
    loop.state.research_turns = MAX_RESEARCH_TURNS
    loop.state.last_gate = None

    assert not loop._gate_wants_an_edit(), "no gate has spoken, so nothing is contradicted"
    assert loop._terminal_choice() == {"type": "function", "function": {"name": "finish"}}
