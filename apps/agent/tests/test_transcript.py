"""The canonical transcript, and the projection over it.

Every test here is an assertion about the one property the split exists for:
**what actually happened and what the model saw are separate, and the second is
always derived from the first.** A regression in any of them puts the loop back
in the position where it can assert something about a context that has since
changed under it, which is the failure two field transcripts died of.
"""

from __future__ import annotations

import json

import pytest
from dakcoder_shared.llm import ToolCall

from dakcoder_agent.compaction import CompactionState, Recap, basic_recap
from dakcoder_agent.context import ContextManager
from dakcoder_agent.journal import Journal
from dakcoder_agent.modes import Mode
from dakcoder_agent.rehydrate import restore_canonical
from dakcoder_agent.transcript import Transcript, Visibility


def _recap(_messages) -> Recap:
    return Recap(goal="carry on", turns=(1, 2))


def _context(**kw) -> ContextManager:
    context = ContextManager(mode=Mode.AGENT, system_prompt="sys", **kw)
    context.set_task("migrate the pension handler")
    return context


def _fill(context: ContextManager, turns: int, *, size: int = 4_000) -> None:
    """Enough conversation that a retention cut has something to bite on."""
    for n in range(turns):
        context.begin_turn()
        call = ToolCall(id=f"c{n}", name="read_file", arguments=json.dumps({"path": f"f{n}.go"}))
        context.append_assistant(f"reading f{n}.go", tool_calls=(call,))
        context.append_tool_result(
            "read_file",
            f"f{n}.go\n" + "\n".join(f"line {i}" for i in range(size)),
            tool_call_id=f"c{n}",
            path=f"f{n}.go",
            line_range=(1, size),
        )


# ── the transcript is append-only ───────────────────────────────────────────


def test_compaction_does_not_change_the_transcript() -> None:
    """The whole point, stated once.

    Before the split, ``compact()`` was ``self._working = retained``: the record
    of what happened stopped existing the moment a run compacted, and nine
    ledgers in the loop had to be cleared to stop them describing it.
    """
    context = _context()
    _fill(context, 12)

    before = [r.seq for r in context.transcript]
    before_bytes = [r.content for r in context.transcript]

    context.compact(_recap, retain_pct=0.10)

    assert [r.seq for r in context.transcript] == before, "records were removed or renumbered"
    assert [r.content for r in context.transcript] == before_bytes, "records were rewritten"
    assert context.compaction is not None, "a compaction produced no sidecar"


def test_compaction_changes_only_the_view() -> None:
    context = _context()
    _fill(context, 12)
    wide = len(context.build())

    context.compact(_recap, retain_pct=0.10)

    narrow = len(context.build())
    assert narrow < wide, "the projection did not shrink"
    assert len(context.transcript) > 0
    assert any("Recap" in m.content for m in context.build()), "no recap was projected"


def test_a_hidden_record_is_still_in_the_record() -> None:
    context = _context()
    context.begin_turn()
    message = context.append_user("a thing the developer said")
    assert context.discard(message) == 1

    assert all("a thing the developer said" not in m.content for m in context.build())
    assert any(r.content == "a thing the developer said" for r in context.transcript)


# ── caps happen at projection, and the whole result is kept ─────────────────


def test_the_whole_tool_result_survives_its_own_cap() -> None:
    """Item 6: what the model sees and what the tool returned are different things.

    A 400KB build log used to be cut to 12k on the way in, and the other 388KB
    existed nowhere -- not on disk, not in the recap, not for a developer asking
    what actually failed.
    """
    context = _context()
    context.begin_turn()
    log = "\n".join(f"pkg/x/y_{i}.go:{i}:1: undefined: Thing{i}" for i in range(20_000))
    shown = context.append_tool_result("go_build", log, tool_call_id="1")

    assert len(shown.content) < len(log), "the cap did not fire"
    seq = context.transcript.records[-1].seq
    assert context.canonical(seq) == log, "the uncapped result was not kept"


def test_coverage_reports_what_survived_the_cap_not_what_the_tool_returned() -> None:
    """BUG L-8, now structurally impossible: one computation, two outputs."""
    context = _context()
    context.begin_turn()
    body = "big.go\n" + "\n".join(f"line {i}" for i in range(200_000))
    shown = context.append_tool_result(
        "read_file", body, tool_call_id="1", path="big.go", line_range=(1, 200_000)
    )

    assert shown.line_range is not None
    assert shown.line_range[1] < 200_000, "the cap kept everything; widen the fixture"
    spans = context.coverage()["big.go"]
    assert spans == [shown.line_range], "coverage disagreed with the message it describes"
    assert not context.holds("big.go", 199_000, 200_000), "claimed lines the cap removed"
    assert context.holds("big.go", 1, shown.line_range[1])


# ── the loop and the view cannot disagree ───────────────────────────────────


def test_a_compacted_result_is_no_longer_claimable() -> None:
    """The intercept's honesty check, at the level of the context.

    ``visible_results`` is what the cached-result intercept consults before
    telling the model "you already have this". A result the compaction has
    projected a recap over must drop out of it on the same turn.
    """
    context = _context()
    context.begin_turn()
    context.append_tool_result(
        "search_repo", "hits\n" * 100, tool_call_id="1", fingerprint="search_repo:abc"
    )
    assert "search_repo:abc" in context.visible_results

    _fill(context, 12)
    context.compact(_recap, retain_pct=0.10)

    assert "search_repo:abc" not in context.visible_results


def test_a_compacted_read_becomes_askable_again() -> None:
    """The other half of BUG L-10: the recap says re-read, so the ledger must allow it."""
    context = _context()
    context.begin_turn()
    context.append_tool_result(
        "read_file", "a.go\nx\ny\nz", tool_call_id="1", path="a.go", line_range=(1, 3)
    )
    assert context.holds("a.go", 1, 3)

    _fill(context, 12)
    context.compact(_recap, retain_pct=0.10)

    assert not context.holds("a.go", 1, 3), "still claimed content the model cannot read"


def test_a_visible_body_stops_being_visible_when_it_is_compacted() -> None:
    context = _context()
    context.begin_turn()
    context.append_tool_result("go_vet", "clean", tool_call_id="1", body="digest-1")
    assert "digest-1" in context.visible_bodies

    _fill(context, 12)
    context.compact(_recap, retain_pct=0.10)

    assert "digest-1" not in context.visible_bodies


# ── repeated answers collapse without history being rewritten ──────────────


def test_repeated_answers_collapse_to_one() -> None:
    """Measured live: one (repeat -> "answered from the earlier result") pair and
    the model moves on 5/5; two and it repeats the call forever 5/5."""
    context = _context()
    for n in range(4):
        context.begin_turn()
        context.append_assistant(
            "",
            tool_calls=(ToolCall(id=f"c{n}", name="git_ops", arguments="{}"),),
        )
        context.append_tool_result(
            "git_ops",
            "git_ops returned: nothing to commit -- that is the current answer.",
            tool_call_id=f"c{n}",
            echo="git_ops:commit",
        )

    live = [m for m in context.build() if "that is the current answer" in m.content]
    stubs = [m for m in context.build() if "was asked again with these arguments" in m.content]
    assert len(live) == 1, "the intercept pattern accumulated in the projection"
    assert len(stubs) == 3
    # And nothing was rewritten to achieve it.
    assert sum(1 for r in context.transcript if "current answer" in r.content) == 4
    # The wire stays well-formed: a stub keeps its call id.
    wire = context.wire()
    declared = {c["id"] for m in wire for c in m.get("tool_calls") or ()}
    assert all(m["tool_call_id"] in declared for m in wire if m["role"] == "tool")


# ── the sidecar ─────────────────────────────────────────────────────────────


def test_a_sidecar_that_no_longer_describes_the_transcript_is_refused() -> None:
    """The safety catch. A recap of a conversation that did not happen is the one
    outcome the whole design exists to make impossible."""
    context = _context()
    _fill(context, 12)
    context.compact(_recap, retain_pct=0.10)
    sidecar = context.compaction
    assert sidecar is not None

    other = _context()
    other.begin_turn()
    other.append_user("a completely different conversation")

    assert other.adopt_compaction(sidecar) is False
    assert other.compaction is None


def test_a_sidecar_survives_a_round_trip_through_disk(tmp_path) -> None:
    context = _context()
    _fill(context, 12)
    context.compact(_recap, retain_pct=0.10)
    sidecar = context.compaction
    assert sidecar is not None

    revived = CompactionState.from_dict(json.loads(json.dumps(sidecar.as_dict())))
    assert revived is not None
    assert revived.matches(context.transcript)
    assert revived.recap.goal == sidecar.recap.goal


def test_restoring_reproduces_the_compacted_view(tmp_path) -> None:
    """Item 1's pay-off: a restart resumes the conversation the model was having.

    ``rehydrate`` rebuilds an approximation from the event stream -- "the newest
    whole turns that fit 55% of the budget" -- which is a different context from
    the compacted one the run was using. This one is the same context.
    """
    journal = Journal(tmp_path, "s1")
    context = _context()
    context.attach_journal(journal)
    _fill(context, 12)
    context.compact(_recap, retain_pct=0.10)
    context.persist()

    before = [(m.role, m.content) for m in context.build()]

    fresh = ContextManager(mode=Mode.AGENT, system_prompt="sys")
    restored = restore_canonical(
        Journal(tmp_path, "s1"), context=fresh, task="migrate the pension handler"
    )

    assert restored is not None
    assert fresh.compaction is not None, "the sidecar was not adopted"
    assert [(m.role, m.content) for m in fresh.build()] == before


def test_restoring_keeps_the_uncapped_results(tmp_path) -> None:
    journal = Journal(tmp_path, "s2")
    context = _context()
    context.attach_journal(journal)
    context.begin_turn()
    log = "\n".join(f"pkg/a_{i}.go:{i}:1: undefined: X{i}" for i in range(20_000))
    context.append_tool_result("go_build", log, tool_call_id="1")
    context.persist()

    fresh = ContextManager(mode=Mode.AGENT, system_prompt="sys")
    assert restore_canonical(Journal(tmp_path, "s2"), context=fresh) is not None
    assert fresh.canonical(fresh.transcript.records[-1].seq) == log


def test_persisting_twice_appends_only_what_is_new(tmp_path) -> None:
    journal = Journal(tmp_path, "s3")
    context = _context()
    context.attach_journal(journal)
    context.begin_turn()
    context.append_user("one")
    context.persist()
    context.append_user("two")
    context.persist()
    context.persist()

    assert journal.records_on_disk() == 2


# ── the deterministic tier ──────────────────────────────────────────────────


def test_the_basic_strategy_needs_no_model() -> None:
    """The tier that must never fail: what overflow recovery and a manual
    ``/compact`` use, and what the summariser falls back to."""
    context = _context()
    _fill(context, 12)

    def explode(_messages):  # pragma: no cover - must not be called
        raise AssertionError("the basic strategy called the summariser")

    recap = context.compact(explode, retain_pct=0.10, strategy="basic")

    assert recap.files_read, "a deterministic recap that names no files is not a recap"
    assert context.compaction is not None
    assert context.compaction.strategy == "basic"


def test_the_basic_recap_states_only_facts() -> None:
    transcript = Transcript()
    transcript.append("assistant", "I will patch the handler", turn=1)
    transcript.append(
        "tool", "ok", tool="write_file", path="handler/x.go", turn=1, meta={"mutation": True}
    )
    transcript.append("tool", "boom\nmore", tool="go_build", turn=1, meta={"ok": False})

    recap = basic_recap(transcript.records, turns=(1, 1))

    assert "handler/x.go" in recap.files_modified
    assert any("go_build" in item for item in recap.do_not_retry)
    assert recap.goal == "I will patch the handler"


# ── visibility ──────────────────────────────────────────────────────────────


def test_a_model_only_message_reaches_the_wire_but_not_the_display() -> None:
    context = _context()
    context.begin_turn()
    context.append_user("a synthetic reminder", visibility=Visibility.MODEL)

    assert any("a synthetic reminder" in m["content"] for m in context.wire())
    record = context.transcript.records[-1]
    assert record.visibility is Visibility.MODEL


# ── the projection is not the record ────────────────────────────────────────


def test_build_is_not_the_wire_repair() -> None:
    """A repair is a report of a defect; it must not become what everything else
    believes the context to be."""
    context = _context()
    context.transcript.append("tool", "an orphan", tool="read_file", tool_call_id="gone")

    assert all(m.content != "an orphan" or m.role == "tool" for m in context.build())
    assert context.wire_repairs, "the orphan was not reported"
    assert all(m["role"] != "tool" for m in context.wire())


@pytest.mark.parametrize("retain", [0.10, 0.35, 0.60])
def test_every_compaction_leaves_a_well_formed_request(retain: float) -> None:
    context = _context()
    _fill(context, 16)
    context.compact(_recap, retain_pct=retain)

    wire = context.wire()
    declared = {c["id"] for m in wire for c in m.get("tool_calls") or ()}
    answered = {m["tool_call_id"] for m in wire if m.get("tool_call_id")}
    assert answered <= declared, "a result whose call nothing declares"
    assert declared <= answered, "a declared call with no result"
