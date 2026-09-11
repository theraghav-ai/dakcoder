"""The canonical transcript: what actually happened, as opposed to what the model saw.

Why this file exists
--------------------
Before it, ``ContextManager`` owned one list and that list was three things at
once: the record of the conversation, the working set the budget is enforced
against, and the request that goes on the wire. Compaction then *rewrote* it --
``self._working = retained`` -- so the moment a run compacted, the record of
what happened stopped existing anywhere in the process. Three consequences, all
of which were being paid for in the field:

**The agent and its own history disagreed.** The loop's ledgers were written at
insertion time and describe what was appended; after a compaction they described
messages that were no longer there. ``_forget_evicted`` existed to paper over
exactly this, by re-deriving four ledgers from ``coverage()`` and clearing five
more outright. Every one of those clears is a fact the run had earned and threw
away, and every gap between "what the ledger claims" and "what the model can
read" is a turn spent arguing with a transcript that has already changed under
it. That is the loop the failure reports describe: the intercept says "those
lines are already in context above", the lines are not, the model asks again.

**A restart could not restore the conversation the model was actually having.**
``rehydrate`` replays ``events.jsonl`` and keeps the newest whole turns that fit
55% of the budget -- a different context from the compacted one the run was
using, rebuilt by a different rule.

**Nothing could be audited.** "What did the model see on turn 30" had no answer
after the first compaction, because the answer had been overwritten.

The split
---------
``Transcript`` is append-only for the life of a conversation. Nothing removes
from it, nothing edits it, and tool results go in **whole** -- the 400KB build
log is here in full. It is the ground truth, and it is what ``events.jsonl``
already almost was.

What the model sees is a *projection* of it (``projection.py``): caps applied,
superseded reads stubbed, the compaction sidecar folded in. The projection is
recomputed from the canonical list every time it is needed, so it cannot drift
from it -- there is no second copy to fall out of sync.

That is the same split Cline reached (``AgentRuntime.state.messages`` plus
``createCompactionStateAwarePrepareTurn``), and for the same reason. It is not
copied wholesale: their projection is a truncating pass with a flat token
estimate, and ours keeps the layered budget, the calibrated estimate and the
slice ledger this codebase measured into existence.

Identity
--------
Every record carries a ``seq``, assigned once and never reused. That is what
makes a sidecar possible -- "the recap replaces records 0..47" is a statement
that survives a restart, where "replaces the first 47 elements of a list that
has since been rewritten" is not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dakcoder_shared.llm import ToolCall

__all__ = [
    "Record",
    "Transcript",
    "Visibility",
    "digest_of",
]


class Visibility(StrEnum):
    """Who a record is for.

    Cline reaches the same distinction through ``metadata.userRunSpan: 0`` and
    ``displayRole: "system"``; this is the same idea with a name on it. The
    thing it prevents is a synthetic message -- a stall reminder, a hook's
    context block, a compaction notice -- appearing in the developer's
    transcript as though the agent had said it, or a display-only note being
    charged to the prompt budget.
    """

    #: The model reads it and the developer sees it. Ordinary conversation.
    BOTH = "both"
    #: On the wire, not in the UI. Reminders, intercepts, hook context.
    MODEL = "model"
    #: In the UI, never on the wire. Reserved for host annotations.
    DISPLAY = "display"


def digest_of(text: str) -> str:
    """A short stable digest. Used for prefix hashing and body identity."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class Record:
    """One thing that happened, recorded whole.

    Frozen for the reason ``Message`` was frozen: append-only is easy to state
    and easy to violate by accident three refactors later, and immutability
    makes the accident impossible rather than merely detectable. The difference
    from ``Message`` is that this one is never elided, never stubbed and never
    evicted -- those are things the *projection* does to a copy.
    """

    #: Monotonic within one transcript, assigned at append, never reused.
    seq: int
    role: str
    #: What the tool actually returned, or what the model actually said. Whole.
    content: str
    #: Which layer the projection should file it under. Kept here rather than
    #: derived so that a restored transcript reproduces the same projection.
    layer: str = "working_set"
    source: str = ""
    #: The tool that produced it, for cap selection at projection time. The cap
    #: used to be chosen at insertion and then forgotten; the projection needs
    #: to choose it afresh every time, so the name has to survive.
    tool: str = ""
    path: str | None = None
    #: The lines the tool actually delivered -- not the lines that survived a
    #: cap. The cap is a projection concern now, and conflating the two is what
    #: BUG L-8 was.
    line_range: tuple[int, int] | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    turn: int = 0
    visibility: Visibility = Visibility.BOTH
    #: Projection hints and provenance. ``echo`` carries a call fingerprint so
    #: the projection can collapse all but the newest answer to one repeated
    #: question; ``kind`` names synthetic records. Never sent to the model.
    meta: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return digest_of(self.content)

    def as_dict(self) -> dict[str, Any]:
        """The persisted form. Round-trips through ``Transcript.from_records``."""
        out: dict[str, Any] = {
            "seq": self.seq,
            "role": str(self.role),
            "content": self.content,
            "layer": str(self.layer),
            "turn": self.turn,
            "visibility": str(self.visibility),
        }
        if self.source:
            out["source"] = self.source
        if self.tool:
            out["tool"] = self.tool
        if self.path:
            out["path"] = self.path
        if self.line_range:
            out["line_range"] = list(self.line_range)
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [
                {"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls
            ]
        if self.meta:
            out["meta"] = dict(self.meta)
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Record":
        span = raw.get("line_range")
        calls = tuple(
            ToolCall(
                id=str(c.get("id") or ""),
                name=str(c.get("name") or ""),
                arguments=str(c.get("arguments") or ""),
            )
            for c in raw.get("tool_calls") or ()
            if isinstance(c, Mapping)
        )
        try:
            visibility = Visibility(str(raw.get("visibility") or Visibility.BOTH))
        except ValueError:
            visibility = Visibility.BOTH
        return cls(
            seq=int(raw.get("seq") or 0),
            role=str(raw.get("role") or "user"),
            content=str(raw.get("content") or ""),
            layer=str(raw.get("layer") or "working_set"),
            source=str(raw.get("source") or ""),
            tool=str(raw.get("tool") or ""),
            path=(str(raw["path"]) if raw.get("path") else None),
            line_range=(
                (int(span[0]), int(span[1]))
                if isinstance(span, (list, tuple)) and len(span) == 2
                else None
            ),
            tool_call_id=(str(raw["tool_call_id"]) if raw.get("tool_call_id") else None),
            tool_calls=calls,
            turn=int(raw.get("turn") or 0),
            visibility=visibility,
            meta=dict(raw.get("meta") or {}),
        )


class Transcript:
    """Append-only record of one conversation.

    The API is deliberately small and deliberately missing things. There is no
    ``remove``, no ``replace``, no ``__setitem__``. A caller that wants an
    earlier record to read differently to the model changes the *projection*,
    which is a pure function of this list plus the compaction sidecar, and is
    therefore always consistent with it by construction.
    """

    __slots__ = ("_records", "_next_seq", "_version")

    def __init__(self, records: Sequence[Record] = ()) -> None:
        self._records: list[Record] = list(records)
        self._next_seq = (self._records[-1].seq + 1) if self._records else 0
        #: Bumped on every append. The projector caches against it, which is
        #: sound precisely because nothing but an append can change this list.
        self._version = 0

    # -- writing ------------------------------------------------------------

    def append(
        self,
        role: str,
        content: str,
        *,
        layer: str = "working_set",
        source: str = "",
        tool: str = "",
        path: str | None = None,
        line_range: tuple[int, int] | None = None,
        tool_call_id: str | None = None,
        tool_calls: Sequence[ToolCall] = (),
        turn: int = 0,
        visibility: Visibility = Visibility.BOTH,
        meta: Mapping[str, Any] | None = None,
    ) -> Record:
        record = Record(
            seq=self._next_seq,
            role=role,
            content=content,
            layer=layer,
            source=source,
            tool=tool,
            path=path,
            line_range=line_range,
            tool_call_id=tool_call_id,
            tool_calls=tuple(tool_calls),
            turn=turn,
            visibility=visibility,
            meta=dict(meta or {}),
        )
        self._records.append(record)
        self._next_seq += 1
        self._version += 1
        return record

    # -- reading ------------------------------------------------------------

    @property
    def records(self) -> tuple[Record, ...]:
        return tuple(self._records)

    @property
    def version(self) -> int:
        """Changes on every append. The projector's cache key."""
        return self._version

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[Record]:
        return iter(self._records)

    def __getitem__(self, index: int) -> Record:
        return self._records[index]

    def by_seq(self, seq: int) -> Record | None:
        for record in self._records:
            if record.seq == seq:
                return record
        return None

    def index_of_seq(self, seq: int) -> int:
        """Position of ``seq``, or ``len`` if every record precedes it.

        The sidecar stores a seq rather than an index precisely so that this
        translation happens once, here, against the list as it is now.
        """
        for position, record in enumerate(self._records):
            if record.seq >= seq:
                return position
        return len(self._records)

    def prefix_hash(self, count: int) -> str:
        """A hash over the first ``count`` records.

        This is what makes a persisted compaction safe to reuse. A sidecar says
        "these ``count`` records were replaced by this recap"; if the records
        under it are not the ones that were summarised -- a different session,
        an edited journal, a restore to an earlier point -- the hash differs and
        the sidecar is discarded rather than silently projected over the wrong
        conversation.

        Cheap by construction: content digests, not content.
        """
        hasher = hashlib.sha256()
        for record in self._records[: max(0, count)]:
            hasher.update(str(record.seq).encode())
            hasher.update(b"\x00")
            hasher.update(str(record.role).encode())
            hasher.update(b"\x00")
            hasher.update(record.digest.encode())
            for call in record.tool_calls:
                hasher.update(b"\x01")
                hasher.update(f"{call.name}{call.arguments or ''}".encode("utf-8", "replace"))
            hasher.update(b"\n")
        return hasher.hexdigest()[:32]

    # -- persistence --------------------------------------------------------

    def as_list(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self._records]

    @classmethod
    def from_records(cls, raw: Sequence[Mapping[str, Any]]) -> "Transcript":
        return cls([Record.from_dict(r) for r in raw if isinstance(r, Mapping)])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Transcript(records={len(self._records)}, next_seq={self._next_seq})"
