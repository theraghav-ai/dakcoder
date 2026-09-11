"""Full-fidelity turn recording.

The questions this exists to answer are the ones nothing else on disk could:
what were the exact bytes of the prompt on turn 14, what did the loop's own
state look like at that moment, and what came back. ``events.jsonl`` is a
display record and ``transcript.jsonl`` is the conversation, not the assembled
request.
"""

from __future__ import annotations

import json
from pathlib import Path

from dakcoder_agent.debug import (
    MAX_FIELD_CHARS,
    DebugLog,
    _render,
    enabled,
    prompts,
    read,
)
from dakcoder_agent.modes import Intent
from scripted import build, calls, patch, plan_call  # noqa: E402
from scripted import gated, planning_router, written  # noqa: F401,E402


def _log_for(tmp_path, monkeypatch, session: str = "s1") -> DebugLog:
    monkeypatch.setenv("DAKCODER_DEBUG", "1")
    log = DebugLog.for_session(tmp_path, session)
    assert log is not None
    return log


def _debug_path(tmp_path, session: str = "dbg") -> Path:
    return Path(tmp_path) / ".dakcoder" / "sessions" / session / "debug.jsonl"


def _run(planning_router, tmp_path, monkeypatch, answer: str = "Added it."):
    loop, _client = build(
        planning_router,
        [plan_call(), patch(), calls(("finish", json.dumps({"answer": answer})))],
        max_turns=8,
    )
    loop.session_id = "dbg"
    loop._debug = _log_for(tmp_path, monkeypatch, "dbg")
    list(loop.run("add the Routes method", intent=Intent.AGENT))
    return read(_debug_path(tmp_path))


# ── off by default ──────────────────────────────────────────────────────────


def test_recording_is_off_unless_it_is_asked_for(tmp_path, monkeypatch) -> None:
    """It has to be free when off, or nobody leaves it in the hot path."""
    monkeypatch.delenv("DAKCODER_DEBUG", raising=False)
    assert not enabled()
    assert DebugLog.for_session(tmp_path, "s1") is None


def test_the_usual_falsy_spellings_are_all_off(monkeypatch) -> None:
    for value in ("", "0", "false", "no", "off", "  OFF  "):
        monkeypatch.setenv("DAKCODER_DEBUG", value)
        assert not enabled(), value
    for value in ("1", "true", "yes", "on"):
        monkeypatch.setenv("DAKCODER_DEBUG", value)
        assert enabled(), value


def test_a_session_with_no_id_records_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DAKCODER_DEBUG", "1")
    assert DebugLog.for_session(tmp_path, "") is None


# ── the prefix compression ──────────────────────────────────────────────────


def _wire(system: str, *rest: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": system}] + [
        {"role": "user", "content": r} for r in rest
    ]


def test_the_first_request_is_stored_whole(tmp_path, monkeypatch) -> None:
    log = _log_for(tmp_path, monkeypatch)

    log.request(
        _wire("sys", "task", "state"),
        turn=1, role="planner", tools=None, tool_choice=None, estimated_tokens=10,
    )

    record = next(r for r in read(log.path) if r["kind"] == "request")
    assert record["shared_prefix"] == 0
    assert len(record["tail"]) == 3


def test_a_later_request_stores_only_what_changed(tmp_path, monkeypatch) -> None:
    """The prompt is prefix-stable by design -- that is what the layered build
    is for -- so a debug log can store the tail and nothing else."""
    log = _log_for(tmp_path, monkeypatch)

    log.request(_wire("sys", "task", "state 1"), turn=1, role="planner",
                tools=None, tool_choice=None, estimated_tokens=10)
    log.request(_wire("sys", "task", "state 2", "and a reply"), turn=2, role="agent",
                tools=None, tool_choice=None, estimated_tokens=20)

    records = [r for r in read(log.path) if r["kind"] == "request"]
    assert records[1]["shared_prefix"] == 2
    assert len(records[1]["tail"]) == 2


def test_replay_is_exact_not_approximate(tmp_path, monkeypatch) -> None:
    """A debug log you cannot trust byte for byte sends you looking for bugs
    that are not there."""
    log = _log_for(tmp_path, monkeypatch)
    sent = [
        _wire("sys", "task", "state 1"),
        _wire("sys", "task", "state 2", "reply"),
        _wire("sys", "task", "state 3", "reply", "another"),
        # A head that genuinely changes -- a mode switch -- must not be
        # reconstructed from the stale prefix.
        _wire("different sys", "task", "state 4"),
    ]
    for turn, wire in enumerate(sent, 1):
        log.request(wire, turn=turn, role="agent", tools=None, tool_choice=None,
                    estimated_tokens=turn)

    rebuilt = dict(prompts(read(log.path)))

    assert [rebuilt[i] for i in range(1, 5)] == sent


def test_a_changed_head_resets_the_prefix(tmp_path, monkeypatch) -> None:
    log = _log_for(tmp_path, monkeypatch)

    log.request(_wire("sys", "a"), turn=1, role="agent", tools=None,
                tool_choice=None, estimated_tokens=1)
    log.request(_wire("OTHER", "a"), turn=2, role="agent", tools=None,
                tool_choice=None, estimated_tokens=1)

    records = [r for r in read(log.path) if r["kind"] == "request"]
    assert records[1]["shared_prefix"] == 0


# ── bounds and safety ───────────────────────────────────────────────────────


def test_a_runaway_field_is_clipped_and_says_so(tmp_path, monkeypatch) -> None:
    log = _log_for(tmp_path, monkeypatch)
    huge = "a" * (MAX_FIELD_CHARS + 5_000)

    log.response(turn=1, content=huge, tool_calls=(), finish_reason="length",
                 usage={}, seconds=0.1)

    record = next(r for r in read(log.path) if r["kind"] == "response")
    assert len(record["content"]) < len(huge)
    assert "more characters" in record["content"]


def test_a_broken_log_never_fails_a_run(tmp_path, monkeypatch) -> None:
    """A debug recorder that can fail a run is one nobody dares leave on."""
    log = _log_for(tmp_path, monkeypatch)
    log._broken = True

    log.note("this goes nowhere")  # must not raise

    assert read(log.path) == []


def test_an_unreadable_log_reads_as_empty(tmp_path) -> None:
    assert read(Path(tmp_path) / "nope.jsonl") == []


def test_a_truncated_last_line_is_dropped_not_raised_on(tmp_path, monkeypatch) -> None:
    """The shape a hard kill leaves."""
    log = _log_for(tmp_path, monkeypatch)
    log.note("first")
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind":"note","tex')

    records = read(log.path)

    assert len(records) == 1
    assert records[0]["text"] == "first"


# ── the whole thing, over a real run ────────────────────────────────────────


def test_a_run_records_every_turn(planning_router, gated, written, tmp_path, monkeypatch):
    records = _run(planning_router, tmp_path, monkeypatch)

    kinds = {r["kind"] for r in records}
    assert {"turn_start", "request", "response", "event"} <= kinds
    # Three model turns: plan, patch, finish.
    assert len([r for r in records if r["kind"] == "request"]) == 3
    assert len([r for r in records if r["kind"] == "turn_start"]) == 3


def test_every_record_is_stamped_with_its_turn(
    planning_router, gated, written, tmp_path, monkeypatch
):
    """Events come off a tee that has no idea which turn it is on. Without the
    stamp they all land under turn 0, which is a flat list -- the thing this
    exists not to be."""
    records = _run(planning_router, tmp_path, monkeypatch)

    edits = [
        r for r in records
        if r["kind"] == "event" and (r.get("data") or {}).get("name") == "patch_file"
    ]

    assert edits, "the patch never reached the recorder"
    assert all(r["turn"] == 2 for r in edits), "the edit belongs to turn 2"


def test_the_state_of_the_loop_is_captured_per_turn(
    planning_router, gated, written, tmp_path, monkeypatch
):
    records = _run(planning_router, tmp_path, monkeypatch)
    starts = [r for r in records if r["kind"] == "turn_start"]

    # All five state groups, every turn.
    assert set(starts[0]["state"]) == {"task", "calls", "reading", "gate", "progress"}
    assert starts[0]["mode"] == "planner"
    assert starts[1]["mode"] == "agent"
    # The plan, carrying the status the change set gave it.
    assert starts[2]["plan"][0]["status"] in ("written", "done")
    # The context's own accounting, straight off `inspect()`.
    assert starts[1]["context"]["canonical_records"] > 0
    assert starts[1]["context"]["budget"] > 0


def test_the_request_records_what_the_turn_was_dispatched_with(
    planning_router, gated, written, tmp_path, monkeypatch
):
    records = _run(planning_router, tmp_path, monkeypatch)
    first = next(r for r in records if r["kind"] == "request")

    assert first["role"] == "planner"
    assert "submit_plan" in first["tool_names"]
    assert first["estimated_tokens"] > 0
    assert first["by_layer"], "the per-layer breakdown is the point of the budget"


def test_the_response_records_what_nothing_else_renders(
    planning_router, gated, written, tmp_path, monkeypatch
):
    records = _run(planning_router, tmp_path, monkeypatch)
    first = next(r for r in records if r["kind"] == "response")

    assert first["finish_reason"]
    assert "prompt_tokens" in first["usage"]
    assert "reasoning_tokens" in first["usage"]
    assert first["seconds"] >= 0


def test_the_report_renders_without_raising(
    planning_router, gated, written, tmp_path, monkeypatch
):
    records = _run(planning_router, tmp_path, monkeypatch)

    report = "\n".join(_render(records))

    assert "TURN 1" in report and "TURN 3" in report
    assert "submit_plan" in report
    assert "cached prefix" in report


# ── finding a session in the first place ────────────────────────────────────


def test_sessions_are_listed_newest_first(tmp_path, monkeypatch) -> None:
    """"How do I get the session id" is the first question anyone asks, and the
    tool should answer it rather than send them into a dot-directory."""
    from dakcoder_agent.debug import sessions

    for name, task in (("older", "first task"), ("newer", "second task")):
        log = _log_for(tmp_path, monkeypatch, name)
        log.note("something")
        (log.path.parent / "session.json").write_text(
            json.dumps({"id": name, "task": task, "status": "done"}), encoding="utf-8"
        )

    listed = sessions(tmp_path)

    assert [row["id"] for row in listed] == ["newer", "older"]
    assert listed[0]["task"] == "second task"
    assert listed[0]["bytes"] > 0


def test_a_session_with_no_recording_is_not_listed(tmp_path, monkeypatch) -> None:
    from dakcoder_agent.debug import sessions

    bare = Path(tmp_path) / ".dakcoder" / "sessions" / "no-recording"
    bare.mkdir(parents=True)
    (bare / "session.json").write_text("{}", encoding="utf-8")

    assert sessions(tmp_path) == []


def test_a_workspace_with_nothing_recorded_says_how_to_turn_it_on(
    tmp_path, capsys
) -> None:
    from dakcoder_agent.debug import main

    code = main(["-C", str(tmp_path)])

    printed = capsys.readouterr().out
    assert code == 1
    assert "no recordings under" in printed
    assert "dakcoder.debugRecording" in printed


def test_latest_resolves_to_the_newest_session(tmp_path, monkeypatch, capsys) -> None:
    from dakcoder_agent.debug import main

    log = _log_for(tmp_path, monkeypatch, "only-one")
    log.turn_start(turn=1, mode="agent", state={}, context={}, plan=[])

    assert main(["latest", "-C", str(tmp_path)]) == 0
    assert "TURN 1" in capsys.readouterr().out


def test_no_session_argument_lists_them(tmp_path, monkeypatch, capsys) -> None:
    from dakcoder_agent.debug import main

    _log_for(tmp_path, monkeypatch, "abc").note("x")

    assert main(["-C", str(tmp_path)]) == 0
    printed = capsys.readouterr().out
    assert "abc" in printed
    assert "SESSION" in printed


def test_an_unknown_session_names_the_ones_that_exist(
    tmp_path, monkeypatch, capsys
) -> None:
    from dakcoder_agent.debug import main

    _log_for(tmp_path, monkeypatch, "abc").note("x")

    assert main(["nope", "-C", str(tmp_path)]) == 1
    assert "abc" in capsys.readouterr().out
