"""The two wire contracts: tool results (C1) and the SSE event stream (C2).

Both live in ``shared`` because both have three independent implementors — the
agent produces them, the gateway relays them, the extension renders them — and a
contract that lives in only one of those is a contract in name only.

**C1, tool results.** Every tool returns ``{ok, content, mutations[]}``. Uniform
so the loop never special-cases a tool to find out whether it worked, and so the
context manager can cap ``content`` without understanding it. ``mutations`` is
the load-bearing field: it is what lets the verification gate scope ``gofmt`` and
``rules_lint`` to touched files, which Part A section 9.3 requires because the
reference template does not pass an unscoped ``gofmt -l``.

**C2, the event stream.** ``event:`` plus a JSON ``data:`` line. Additive only,
and unknown types must be ignored rather than treated as errors — that is what
lets a newer agent talk to an older extension, which will happen the moment the
.vsix and the wheel version independently.

One event type is different in kind. ``assistant_delta`` is transient: never
persisted, never replayed, and **coalesced here rather than at the client**. That
is finding S11 from the frontend agent, where per-token events saturated the
extension host and the UI fell behind the model. Coalescing at the source is the
only place it can be fixed once for every consumer.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .paths import is_protected

__all__ = [
    "DeltaCoalescer",
    "Event",
    "EventType",
    "Mutation",
    "MutationKind",
    "ToolResult",
    "sse_frame",
]


# ── C1: tool results ────────────────────────────────────────────────────────


class MutationKind(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class Mutation:
    """One file a tool changed, workspace-relative.

    Deliberately not a diff. The extension already has the working tree and can
    render one; carrying the content here would put every edit through the token
    budget twice.
    """

    path: str
    kind: MutationKind = MutationKind.MODIFY

    def as_dict(self) -> dict[str, Any]:
        """Serialised with the protected flag computed here, in one place.

        The alternative was for the extension to reimplement ``PROTECTED_GLOBS``
        in TypeScript, which duplicates a security-relevant constant across the
        seam with no test binding the copies — and the matcher is custom, not
        ``fnmatch``, so a naive port disagrees at the edges. Computing it at
        serialisation time means every surface that shows a mutation shows the
        badge, and there is exactly one implementation.
        """
        return {
            "path": self.path,
            "kind": str(self.kind),
            "protected": is_protected(self.path),
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What every tool returns (C1).

    ``ok=False`` is a normal outcome, not an exception. A failed build is
    information the model needs, and raising would force the loop to translate
    the exception back into text anyway — losing the structure on the way.

    ``fix`` exists because of Part A section 7.1's best small idea: a refusal
    that names the working alternative saves a whole turn. It is rendered into
    ``content`` when the result reaches the model, so a tool author cannot set it
    and have it silently dropped.
    """

    ok: bool
    content: str
    mutations: tuple[Mutation, ...] = ()
    #: What to do instead. Set on every refusal.
    fix: str = ""
    #: True when ``content`` was cut to fit an insertion cap.
    truncated: bool = False
    #: Free-form, never shown to the model: timings, exit codes, counts.
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, content: str, *, fix: str = "", **kw: Any) -> ToolResult:
        return cls(ok=False, content=content, fix=fix, **kw)

    @classmethod
    def success(cls, content: str, *, mutations: Sequence[Mutation] = (), **kw: Any) -> ToolResult:
        return cls(ok=True, content=content, mutations=tuple(mutations), **kw)

    def for_model(self) -> str:
        """The string the model sees as the tool message.

        The fix is appended rather than kept in a field the model never reads.
        Structured error metadata that only the UI renders is how an agent ends
        up repeating a refused call three times.
        """
        if self.fix and self.fix not in self.content:
            return f"{self.content}\n\n{self.fix}"
        return self.content

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "content": self.content}
        if self.mutations:
            payload["mutations"] = [m.as_dict() for m in self.mutations]
        else:
            payload["mutations"] = []
        if self.fix:
            payload["fix"] = self.fix
        if self.truncated:
            payload["truncated"] = True
        # `meta` carried the elapsed milliseconds and was then dropped on the
        # floor here, so nothing downstream could time a tool. Client-side
        # timing is not a substitute: it measures the round trip including the
        # approval wait, which is the developer's time, not the tool's.
        if self.meta:
            if "ms" in self.meta:
                payload["ms"] = self.meta["ms"]
            extra = {k: v for k, v in self.meta.items() if k not in ("ms", "tool")}
            if extra:
                payload["meta"] = extra
        return payload

    @property
    def paths(self) -> tuple[str, ...]:
        """Just the touched paths — what the gate scopes itself to."""
        return tuple(m.path for m in self.mutations)


# ── C2: the event stream ────────────────────────────────────────────────────


class EventType(StrEnum):
    """Every event the agent can emit.

    Additive only. A client that meets an unknown type must ignore it; that rule
    is what lets the wheel and the .vsix version independently, which they will,
    because one ships through GitLab and the other through a marketplace.
    """

    TURN_START = "turn_start"
    ASSISTANT = "assistant"
    ASSISTANT_DELTA = "assistant_delta"
    TOOL_CALL = "tool_call"
    TOOL_PENDING = "tool_pending"
    TOOL_RESULT = "tool_result"
    PLAN = "plan"
    GATE = "gate"
    USAGE = "usage"
    QUOTA = "quota"
    FINISH = "finish"
    ERROR = "error"
    STEER = "steer"
    HEARTBEAT = "heartbeat"
    END = "end"


#: Types that must never be written to a transcript or replayed on reconnect.
TRANSIENT: frozenset[EventType] = frozenset({EventType.ASSISTANT_DELTA, EventType.HEARTBEAT})


@dataclass(frozen=True, slots=True)
class Event:
    """One SSE event."""

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def transient(self) -> bool:
        return self.type in TRANSIENT

    def encode(self) -> str:
        return sse_frame(str(self.type), self.data)


def sse_frame(event: str, data: Any) -> str:
    """Encode one SSE frame.

    Compact JSON on a single ``data:`` line. Multi-line data is legal SSE but
    needs every line prefixed, and getting that wrong produces a stream that
    parses fine until the first payload containing a newline — a failure that
    shows up in production and never in a test with short strings. Compact JSON
    escapes newlines, so the case cannot arise.

    ``ensure_ascii=False`` keeps Devanagari and Tamil identifiers readable rather
    than turning them into escape soup; the stream is declared UTF-8.
    """
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n"


class DeltaCoalescer:
    """Batches ``assistant_delta`` fragments before they reach the wire (fix S11).

    The model streams tokens; a naive relay turns each into an SSE frame, an
    IPC message and a React render. The frontend agent shipped that and the UI
    fell measurably behind the model on long responses — the text was already
    complete while the panel was still catching up.

    Flush on either trigger:

    * ``min_chars`` — enough text accumulated to be worth a frame.
    * ``max_interval`` — enough time passed that holding it would look like a
      stall. This is the one that matters: without it, a model that pauses
      mid-sentence leaves the last few characters buffered indefinitely, which
      reads as a hang rather than as latency.

    The clock is injected so the interval behaviour is testable without sleeping.
    A coalescer whose timing can only be verified by wall-clock is one whose
    timing is never verified.
    """

    def __init__(
        self,
        *,
        min_chars: int = 120,
        max_interval: float = 0.08,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min_chars < 1:
            raise ValueError("min_chars must be positive")
        self.min_chars = min_chars
        self.max_interval = max_interval
        self._clock = clock
        self._buffer: list[str] = []
        self._size = 0
        self._last = clock()

    def feed(self, fragment: str) -> Event | None:
        """Accept a fragment; return an event only when a flush is due."""
        if not fragment:
            return None
        self._buffer.append(fragment)
        self._size += len(fragment)

        if self._size >= self.min_chars or self._clock() - self._last >= self.max_interval:
            return self.flush()
        return None

    def flush(self) -> Event | None:
        """Emit whatever is buffered. Idempotent — returns None when empty."""
        if not self._buffer:
            return None
        text = "".join(self._buffer)
        self._buffer.clear()
        self._size = 0
        self._last = self._clock()
        return Event(EventType.ASSISTANT_DELTA, {"text": text})

    def drain(self, fragments: Iterable[str]) -> Iterator[Event]:
        """Coalesce a whole stream, flushing the tail.

        Forgetting the final flush is the single most common bug in code shaped
        like this: everything works except that the last sentence never arrives.
        """
        for fragment in fragments:
            event = self.feed(fragment)
            if event is not None:
                yield event
        tail = self.flush()
        if tail is not None:
            yield tail

    @property
    def pending(self) -> int:
        return self._size
