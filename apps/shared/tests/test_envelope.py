"""Tests for the event envelope, and for the coalescer streaming depends on.

``DeltaCoalescer`` had no tests for as long as it had no callers. It has both
now: it is the thing standing between a model that emits a token at a time and a
panel that would otherwise render once per token, which is the shape the frontend
agent shipped and measurably fell behind on (fix S11).
"""

from __future__ import annotations

import pytest

from dakcoder_shared.envelope import TRANSIENT, DeltaCoalescer, Event, EventType


class Clock:
    """A clock a test can move, so the interval behaviour needs no sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def texts(events):
    return [e.data["text"] for e in events]


# -- flushing ----------------------------------------------------------------


def test_fragments_are_held_until_there_is_enough_to_be_worth_a_frame():
    """One frame per token is an SSE frame, an IPC message and a repaint each."""
    clock = Clock()
    coalescer = DeltaCoalescer(min_chars=10, max_interval=99.0, clock=clock)

    assert coalescer.feed("abc") is None
    assert coalescer.feed("def") is None
    event = coalescer.feed("ghij")

    assert event is not None
    assert event.type is EventType.ASSISTANT_DELTA
    assert event.data["text"] == "abcdefghij"


def test_a_pause_flushes_what_is_held_rather_than_sitting_on_it():
    """The trigger that matters. Without it a model that pauses mid-sentence
    leaves the last few characters buffered, which reads as a hang."""
    clock = Clock()
    coalescer = DeltaCoalescer(min_chars=1_000, max_interval=0.08, clock=clock)

    assert coalescer.feed("thinking") is None
    clock.advance(0.09)
    event = coalescer.feed("...")

    assert event is not None
    assert event.data["text"] == "thinking..."


def test_the_clock_restarts_on_every_flush():
    """Otherwise the first flush leaves the interval permanently expired and
    every fragment after it goes out on its own — coalescing in name only."""
    clock = Clock()
    coalescer = DeltaCoalescer(min_chars=4, max_interval=0.08, clock=clock)

    assert coalescer.feed("abcd") is not None
    assert coalescer.feed("e") is None, "the interval was not reset by the flush"


def test_an_empty_fragment_is_not_a_flush():
    coalescer = DeltaCoalescer(min_chars=1)
    assert coalescer.feed("") is None


def test_flushing_an_empty_buffer_says_nothing_twice():
    coalescer = DeltaCoalescer(min_chars=1)
    coalescer.feed("x")
    assert coalescer.flush() is None
    assert coalescer.flush() is None


def test_min_chars_must_be_positive():
    """Zero would flush on every fragment, which is the behaviour this class
    exists to prevent — and it would do it silently."""
    with pytest.raises(ValueError):
        DeltaCoalescer(min_chars=0)


# -- draining a whole stream -------------------------------------------------


def test_drain_flushes_the_tail():
    """The single most common bug in code shaped like this: everything works
    except that the last sentence never arrives."""
    clock = Clock()
    coalescer = DeltaCoalescer(min_chars=6, max_interval=99.0, clock=clock)

    events = list(coalescer.drain(["abc", "def", "ghi"]))

    assert texts(events) == ["abcdef", "ghi"]


def test_drain_loses_nothing():
    clock = Clock()
    coalescer = DeltaCoalescer(min_chars=7, max_interval=99.0, clock=clock)
    fragments = ["the ", "pension ", "handler ", "lives ", "in ", "handler/"]

    joined = "".join(texts(coalescer.drain(fragments)))

    assert joined == "".join(fragments)


# -- the transient contract --------------------------------------------------


def test_a_delta_is_transient_and_says_so():
    """`Session.record` reads this to decide whether to store the event, and the
    SSE relay reads it to decide whether the event may advance a cursor."""
    coalescer = DeltaCoalescer(min_chars=1)
    event = coalescer.feed("x")

    assert event is not None
    assert event.transient
    assert event.type in TRANSIENT


def test_the_types_that_carry_the_transcript_are_not_transient():
    for kind in (EventType.ASSISTANT, EventType.TOOL_RESULT, EventType.FINISH):
        assert not Event(kind, {}).transient, kind
