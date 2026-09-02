"""Rebuild a conversation from what a session left on disk.

`journal.py` made a restart keep the *record* of a conversation — the transcript
a panel replays, the mutation list `revert` reads. It did not make a restart keep
the conversation. `loopback.follow_up` says so in its own comment:

    `continued=True` only has a context to reuse while the daemon still holds
    one. After a restart there is none, and the resume falls back to re-seeding
    the original task -- degraded, but not silently.

Degraded means: a developer reloads the VS Code window at turn 40 of a migration,
types "carry on with the repo layer", and the agent starts the task again from
the beginning, having forgotten every file it read and every decision it made.
The record of all of it is sitting in `events.jsonl`.

This reads it back. The events are replayed through the ContextManager's own
append methods rather than deserialised into messages, so the rebuilt context
goes through the same insertion caps, the same read-slice ledger and the same
supersession rules as a live one. There is one assembler (§6.4) and this is not
a second one.

**What comes back, and what does not.** The message list comes back. What does
not is anything that was never on the wire: the loop's `_State` ledgers — which
searches were exhausted, which reads were refused, how many times a gate failed.
Those live in `AgentLoop.carry_from`, and a restored session starts them empty.
The consequence is bounded and one-directional: the agent may repeat a search it
had already exhausted. It will not skip work it has not done.

**Bounded by the budget, deterministically.** A 400-turn session does not fit in
a prompt, and restoring one must not call a model to summarise — that is a
billed request the developer did not ask for, at the moment they are waiting for
a window to finish reloading. So the replay keeps the most recent whole turns
that fit and states, in a message the model reads, how many it dropped and where
to look for them.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from dakcoder_shared.envelope import EventType
from dakcoder_shared.llm import ToolCall
from dakcoder_shared.tokens import estimate_tokens

from .context import ContextManager

__all__ = ["Restored", "rehydrate", "restorable"]

#: Fraction of the prompt budget a restored conversation may occupy.
#:
#: Not the whole budget: the run that follows has to fit its own turns in what
#: is left, and a context restored to the compaction threshold would compact on
#: its first turn — turning "continue where you left off" into "summarise where
#: you left off", which is the thing this exists to avoid.
RESTORE_FRACTION = 0.55


class Restored:
    """What a rehydration produced, and what it had to leave out."""

    __slots__ = ("context", "turns", "dropped_turns", "events")

    def __init__(
        self, context: ContextManager, turns: int, dropped_turns: int, events: int
    ) -> None:
        self.context = context
        #: Turns replayed into the context.
        self.turns = turns
        #: Turns that did not fit, oldest first.
        self.dropped_turns = dropped_turns
        #: Stored events read.
        self.events = events

    @property
    def complete(self) -> bool:
        return self.dropped_turns == 0

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"Restored(turns={self.turns}, dropped={self.dropped_turns}, "
            f"events={self.events})"
        )


def restorable(events: Sequence[dict[str, Any]]) -> bool:
    """Whether these events describe a conversation worth rebuilding.

    One `user` event and nothing else is a session that never got a reply; there
    is nothing to continue and re-seeding the task is the right answer.
    """
    return any(
        str(e.get("type")) in (EventType.ASSISTANT, EventType.TOOL_RESULT) for e in events
    )


def rehydrate(
    events: Iterable[dict[str, Any]],
    *,
    context: ContextManager,
    task: str = "",
    acceptance: Sequence[str] = (),
) -> Restored:
    """Replay stored events into ``context``.

    The context is expected fresh: this is the restore path, not a merge. The
    task and acceptance criteria come from the session summary rather than from
    the event stream, because the summary is where they are authoritative — the
    opening `user` event carries the text the developer typed, which for a
    resumed or re-worded session is not the same thing.
    """
    stored = [e for e in events if isinstance(e, dict)]
    turns = _turns(stored)

    if task:
        context.set_task(task, acceptance=tuple(acceptance))

    kept, dropped = _fit(turns, context)
    if dropped:
        context.append_user(
            f"[Restored from disk. The {dropped} earliest turn(s) of this "
            f"conversation did not fit the prompt budget and are not below; the "
            f"full transcript is in .dakcoder/sessions/. What follows is the "
            f"most recent {len(kept)} turn(s), verbatim.]"
        )

    for turn in kept:
        _replay(turn, context)

    return Restored(context, len(kept), dropped, len(stored))


# ── shaping the stream into turns ───────────────────────────────────────────


class _Turn:
    """One turn's events, in the order they were stored."""

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []


def _turns(stored: Sequence[dict[str, Any]]) -> list[_Turn]:
    """Group into turns on `turn_start`.

    Events before the first `turn_start` — the opening `user` message — go into
    a turn of their own so nothing is lost when the head is dropped for budget:
    the task is re-supplied from the summary either way, and a `user` message
    the developer typed mid-run belongs with the turn it interrupted.
    """
    turns: list[_Turn] = [_Turn()]
    for event in stored:
        kind = str(event.get("type"))
        if kind == EventType.TURN_START and turns[-1].events:
            turns.append(_Turn())
        if kind in _REPLAYED:
            turns[-1].events.append(event)
    return [t for t in turns if t.events]


#: Event types that carry conversation. Everything else — `usage`, `gate`,
#: `heartbeat`, `tool_pending`, `finish` — is telemetry, UI furniture, or a
#: statement about the run rather than a message in it.
_REPLAYED = frozenset(
    {
        EventType.TURN_START,
        EventType.USER,
        EventType.ASSISTANT,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.PLAN,
        EventType.STEER,
    }
)


def _fit(turns: list[_Turn], context: ContextManager) -> tuple[list[_Turn], int]:
    """Keep the newest whole turns that fit, and say how many were dropped.

    Whole turns, never part of one: a turn cut in half is an assistant message
    whose tool calls have no results, or results whose calls nothing declares —
    the exact wire defect `wire()` exists to repair, manufactured on purpose at
    restore time.
    """
    allowance = int(context.budget * RESTORE_FRACTION)
    used = 0
    kept: list[_Turn] = []
    for turn in reversed(turns):
        cost = sum(_cost(event, context) for event in turn.events)
        if kept and used + cost > allowance:
            break
        used += cost
        kept.append(turn)
    kept.reverse()
    return kept, len(turns) - len(kept)


def _cost(event: dict[str, Any], context: ContextManager) -> int:
    """What this event will cost once it is a message.

    An estimate over the raw event rather than the assembled message, because
    the assembled message does not exist until it is appended and the fitting
    decision has to be made before that. It over-counts a capped tool result —
    the insertion cap will shrink it — which errs towards restoring less than
    would fit rather than more.
    """
    data = event.get("data") or {}
    text = "".join(
        str(data.get(field, "")) for field in ("text", "content", "arguments", "fix")
    )
    return estimate_tokens(text)


# ── replaying one turn ──────────────────────────────────────────────────────


def _replay(turn: _Turn, context: ContextManager) -> None:
    """Replay one turn's events through the context's own append methods.

    An assistant message and its tool calls arrive as separate events — the
    prose first, then one `tool_call` each — and the API needs them as one
    message with a `tool_calls` array. So the prose is held until the turn's
    calls are known, which is what `_flush` does. Holding rather than patching
    afterwards keeps every message the context sees immutable, which is what
    `supersede`'s docstring calls the narrow exception it is allowed to be.
    """
    prose = ""
    calls: list[ToolCall] = []
    started = False
    #: call id -> the arguments it was dispatched with. A `tool_result` carries
    #: the content and the id but not the arguments — only the *intercepted*
    #: path adds them — and the arguments are where `path` and the line range
    #: live. Without them a restored read claims no coverage, and the re-read
    #: intercept lets the agent re-read every file it already has.
    arguments: dict[str, dict[str, Any]] = {}

    def flush() -> None:
        nonlocal prose, calls
        if prose or calls:
            context.append_assistant(prose, tool_calls=tuple(calls))
        prose, calls = "", []

    for event in turn.events:
        kind = str(event.get("type"))
        data = event.get("data") or {}

        if kind == EventType.TURN_START:
            if not started:
                context.begin_turn()
                started = True
            continue

        if kind == EventType.USER:
            flush()
            text = str(data.get("text", ""))
            if text:
                context.append_user(text)
            continue

        if kind == EventType.STEER:
            # Pinned as well as appended, exactly as the live path does: a
            # correction the developer made at turn 12 must survive the restore
            # that a window reload causes, or reloading undoes the steer.
            text = str(data.get("text", ""))
            if text:
                context.pin_directive(text)
            continue

        if kind == EventType.ASSISTANT:
            flush()
            prose = str(data.get("text", ""))
            continue

        if kind == EventType.PLAN:
            # `plan` repeats the prose of the `assistant` that carried it, which
            # is why the panel de-duplicates them. Here the prose is already
            # held, so only the pin is applied.
            text = str(data.get("text", ""))
            if text:
                context.set_plan(text)
            continue

        if kind == EventType.TOOL_CALL:
            call_id = str(data.get("id", ""))
            calls.append(
                ToolCall(
                    id=call_id,
                    name=str(data.get("name", "")),
                    arguments=_arguments(data.get("arguments")),
                )
            )
            arguments[call_id] = _parsed(data.get("arguments"))
            continue

        if kind == EventType.TOOL_RESULT:
            # The assistant that declared this call has to be in the list before
            # its result is, or the wire is malformed for the length of the
            # conversation.
            flush()
            _replay_result(data, context, arguments)

    flush()


def _replay_result(
    data: dict[str, Any], context: ContextManager, dispatched: dict[str, dict[str, Any]]
) -> None:
    name = str(data.get("name", "")) or "tool"
    content = str(data.get("content", ""))
    fix = str(data.get("fix", ""))
    if fix and fix not in content:
        # `ToolResult.for_model` appends the fix; the event keeps them apart.
        # The model saw them joined, so the restore joins them.
        content = f"{content}\n\n{fix}"

    call_id = str(data.get("id", ""))
    # The call's arguments first, the result's own only as a fallback: the
    # intercepted path is the only one that repeats them on the result.
    arguments = dispatched.get(call_id) or _parsed(data.get("arguments"))
    path = arguments.get("path") if isinstance(arguments.get("path"), str) else None
    context.append_tool_result(
        name,
        content,
        tool_call_id=call_id,
        path=path,
        line_range=_line_range(arguments),
    )


def _arguments(raw: Any) -> str:
    import json

    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw, separators=(",", ":"))
    return "{}"


def _parsed(raw: Any) -> dict[str, Any]:
    import json

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _line_range(arguments: dict[str, Any]) -> tuple[int, int] | None:
    """The line range a read asked for, if it asked for one.

    Recovered so the restored context's read-slice ledger knows what the model
    has seen. Without it every restored read claims no coverage, and the
    re-read intercept — which asks the context, not the loop, since RC-1 — lets
    the agent re-read every file it already has.
    """
    start, end = arguments.get("start"), arguments.get("end")
    if isinstance(start, int) and isinstance(end, int) and 0 < start <= end:
        return (start, end)
    return None
