"""Compaction as a *view* of history, not an edit to it.

The change this file makes
--------------------------
Compaction used to be destructive: ``self._working = retained``. The evicted
messages stopped existing in the process, the recap took their place, and every
ledger in the loop that described them became a claim about content that was no
longer there. Nine ledgers were cleared or rebuilt after each compaction
(``_forget_evicted``) to stop the agent asserting things about a transcript that
had changed under it, and the clearing itself threw away facts the run had paid
model calls to establish.

Here compaction produces a ``CompactionState`` -- a *sidecar* -- and changes
nothing. The canonical transcript is untouched. The sidecar says:

    "records ``[0, source_seq)`` project as this recap"

and the projection applies it on the way to the model. Three things follow that
were expensive before:

* **The agent and the model can be kept in step.** "Can the model see line 300
  of this file" is answered by looking at the projection, which is derived from
  the transcript and the sidecar together, so the answer cannot be stale. There
  is no ledger to invalidate because there is no second copy of the fact.
* **A restart restores the conversation the model was actually having**, by
  loading the sidecar beside the transcript rather than re-deriving a different
  context from raw turns.
* **"Undo this compaction" is deleting a file.** So is "show me what the model
  saw at turn 30".

``source_prefix_hash`` is the safety catch. A sidecar is only applied when the
records it claims to replace hash to what they hashed to when it was written; a
transcript that has been restored, edited or truncated underneath it gets a
fresh compaction instead of a recap describing a conversation that did not
happen.

Two strategies
--------------
``agentic`` is the summariser call this codebase already had -- a
schema-constrained recap whose ``do_not_retry`` list is the field that earns the
compaction. ``basic`` is deterministic: no model call, built from what the
evicted records *are*. It was already present as ``_digest``, buried as a
fallback inside the summariser; promoting it to a named strategy is what makes
it selectable when the summariser is down, when the run is in overflow recovery,
or when a developer asks for a compaction and should not be billed for one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .transcript import Record, Transcript

__all__ = [
    "CompactionState",
    "MAX_RECAP_ITEMS",
    "RECAP_BUDGET_TOKENS",
    "Recap",
    "basic_recap",
]

#: How many entries of each recap list survive a merge. A recap that grows
#: without limit eventually costs more than the working set it replaced; twelve
#: is roughly two screens of ``do_not_retry`` and well inside the recap budget.
MAX_RECAP_ITEMS = 12

#: The recap's allocation from §6.1. Reserved when deciding how much of the
#: working set to retain, because the recap grows as history is evicted and
#: budgeting against its current size would leave no room for the one about to
#: replace it.
RECAP_BUDGET_TOKENS = 2_000


@dataclass(frozen=True, slots=True)
class Recap:
    """A structured compaction recap (Part A §6.5).

    Structured, not prose, because of one field: ``do_not_retry`` records dead
    ends, and that is what stops the post-compaction agent cheerfully repeating
    them. ``merge`` folds each recap into the one before it, so a second
    compaction does not throw the first one's dead ends away (BUG L-4).

    ``markdown()`` is what the projection renders into the RECAP layer. It is
    now also persisted, inside a ``CompactionState``, which is what lets a
    restart rebuild the compacted view rather than a different one.
    """

    goal: str = ""
    plan_step: str = ""
    files_created: tuple[str, ...] = ()
    files_modified: tuple[str, ...] = ()
    #: Files whose contents were evicted by this compaction.
    #:
    #: The field the recap did not have, and the omission that made compaction
    #: self-defeating: what a compaction throws away is mostly file reads, and a
    #: recap that does not mention them leaves re-reading as the only rational
    #: next move — which puts the context straight back over the threshold that
    #: fired the compaction. One session went round that circuit twenty-five
    #: times without ever producing a plan.
    files_read: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    #: What the run established about the code, as facts a reader could act on.
    #: The field an *answering* run needs and none of the others gave it
    #: (BUG L-30).
    findings: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    do_not_retry: tuple[str, ...] = ()
    turns: tuple[int, int] = (0, 0)

    def merge(self, previous: "Recap | None") -> "Recap":
        """Fold an earlier recap into this one.

        A compaction *replaces* the projected recap, and the evicted set handed
        to the summariser never contains the previous one — so the first
        compaction's ``do_not_retry``, the field this class's own docstring
        calls the reason it exists, vanished at the second compaction and the
        run cheerfully repeated the dead end that had caused it (BUG L-4). Long
        runs are exactly the runs that compact twice.

        Older entries come first: they are the earlier history, and a reader —
        model or human — should meet them in the order they happened. Bounded
        per field, oldest dropped first, because a recap that grows without
        limit eventually costs more than the working set it replaced.
        """
        if previous is None:
            return self

        def fold(older: tuple[str, ...], newer: tuple[str, ...]) -> tuple[str, ...]:
            seen: dict[str, None] = {}
            for item in (*older, *newer):
                if item:
                    seen[item] = None
            return tuple(seen)[-MAX_RECAP_ITEMS:]

        lo = min(previous.turns[0] or self.turns[0], self.turns[0] or previous.turns[0])
        return Recap(
            goal=self.goal or previous.goal,
            plan_step=self.plan_step or previous.plan_step,
            files_created=fold(previous.files_created, self.files_created),
            files_modified=fold(previous.files_modified, self.files_modified),
            files_read=fold(previous.files_read, self.files_read),
            decisions=fold(previous.decisions, self.decisions),
            findings=fold(previous.findings, self.findings),
            verified=fold(previous.verified, self.verified),
            open_items=fold(previous.open_items, self.open_items),
            do_not_retry=fold(previous.do_not_retry, self.do_not_retry),
            turns=(lo, max(previous.turns[1], self.turns[1])),
        )

    def markdown(self) -> str:
        lo, hi = self.turns

        def block(label: str, items: Iterable[str]) -> str:
            items = [i for i in items if i]
            if not items:
                return ""
            head = f"{label + ':':17}{items[0]}\n"
            return head + "".join(f"{'':17}{i}\n" for i in items[1:])

        out = [f"## Recap (turns {lo}–{hi}, compacted)\n"]
        if self.goal:
            out.append(f"{'Goal:':17}{self.goal}\n")
        if self.plan_step:
            out.append(f"{'Plan step:':17}{self.plan_step}\n")
        out.append(block("Files created", self.files_created))
        out.append(block("Files modified", self.files_modified))
        if self.files_read:
            out.append(block("Already read", self.files_read))
            out.append(
                f"{'':17}(their contents were compacted away. Re-read one only if you\n"
                f"{'':17}need a line range you have not seen — re-reading all of them\n"
                f"{'':17}is what caused this compaction.)\n"
            )
        out.append(block("Decisions", self.decisions))
        out.append(block("Findings", self.findings))
        out.append(block("Verified", self.verified))
        out.append(block("Open", self.open_items))
        out.append(block("Do not retry", self.do_not_retry))
        return "".join(out)

    # -- persistence --------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "plan_step": self.plan_step,
            "files_created": list(self.files_created),
            "files_modified": list(self.files_modified),
            "files_read": list(self.files_read),
            "decisions": list(self.decisions),
            "findings": list(self.findings),
            "verified": list(self.verified),
            "open_items": list(self.open_items),
            "do_not_retry": list(self.do_not_retry),
            "turns": list(self.turns),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Recap":
        def items(key: str) -> tuple[str, ...]:
            value = raw.get(key)
            if not isinstance(value, (list, tuple)):
                return ()
            return tuple(str(v) for v in value if v)

        turns = raw.get("turns")
        span = (
            (int(turns[0]), int(turns[1]))
            if isinstance(turns, (list, tuple)) and len(turns) == 2
            else (0, 0)
        )
        return cls(
            goal=str(raw.get("goal") or ""),
            plan_step=str(raw.get("plan_step") or ""),
            files_created=items("files_created"),
            files_modified=items("files_modified"),
            files_read=items("files_read"),
            decisions=items("decisions"),
            findings=items("findings"),
            verified=items("verified"),
            open_items=items("open_items"),
            do_not_retry=items("do_not_retry"),
            turns=span,
        )


@dataclass(frozen=True, slots=True)
class CompactionState:
    """The sidecar: which canonical records a recap stands in for.

    Everything here is a statement about the transcript rather than a copy of
    it, which is what makes it safe to persist and cheap to keep. ``matches``
    is the whole contract: a sidecar that cannot prove it describes the records
    under it is ignored, and the caller compacts again.
    """

    recap: Recap
    #: Records with ``seq < source_seq`` are the ones the recap replaces. A seq
    #: rather than a count, so it survives a transcript that gains a prefix.
    source_seq: int
    #: ``Transcript.prefix_hash`` over exactly those records, at write time.
    source_prefix_hash: str
    #: How many records were replaced, and what they cost. Reporting only.
    source_count: int = 0
    source_tokens: int = 0
    #: Which paths the replaced records had read. The projection does not need
    #: it; the loop reports it and the recap names it.
    source_paths: tuple[str, ...] = ()
    #: "agentic" or "basic".
    strategy: str = "agentic"
    #: The turn it was written on, for the thrash detector and the UI.
    turn: int = 0
    created_at: str = ""
    #: How many compactions have been folded into this recap. A sidecar
    #: replaces its predecessor rather than stacking, because ``Recap.merge``
    #: already folds the older one in.
    generation: int = 1

    def matches(self, transcript: Transcript) -> bool:
        """Whether this sidecar still describes the transcript it was written for.

        Cheap and total. A mismatch is not an error -- it is the ordinary
        outcome of restoring a session, importing a transcript, or rolling back
        to a checkpoint -- and the caller's answer is to compact again rather
        than to project a summary of a conversation that did not happen.
        """
        count = transcript.index_of_seq(self.source_seq)
        if count != self.source_count:
            return False
        return transcript.prefix_hash(count) == self.source_prefix_hash

    def cut(self, transcript: Transcript) -> int:
        """How many leading records the recap stands in for, in this transcript."""
        return transcript.index_of_seq(self.source_seq)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recap": self.recap.as_dict(),
            "source_seq": self.source_seq,
            "source_prefix_hash": self.source_prefix_hash,
            "source_count": self.source_count,
            "source_tokens": self.source_tokens,
            "source_paths": list(self.source_paths),
            "strategy": self.strategy,
            "turn": self.turn,
            "created_at": self.created_at,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CompactionState | None":
        recap = raw.get("recap")
        if not isinstance(recap, Mapping):
            return None
        try:
            return cls(
                recap=Recap.from_dict(recap),
                source_seq=int(raw.get("source_seq") or 0),
                source_prefix_hash=str(raw.get("source_prefix_hash") or ""),
                source_count=int(raw.get("source_count") or 0),
                source_tokens=int(raw.get("source_tokens") or 0),
                source_paths=tuple(str(p) for p in raw.get("source_paths") or () if p),
                strategy=str(raw.get("strategy") or "agentic"),
                turn=int(raw.get("turn") or 0),
                created_at=str(raw.get("created_at") or ""),
                generation=int(raw.get("generation") or 1),
            )
        except (TypeError, ValueError):
            return None

    @classmethod
    def build(
        cls,
        *,
        recap: Recap,
        transcript: Transcript,
        cut: int,
        tokens: int = 0,
        paths: Sequence[str] = (),
        strategy: str = "agentic",
        turn: int = 0,
        generation: int = 1,
    ) -> "CompactionState":
        """Write a sidecar for a cut taken at position ``cut`` of ``transcript``.

        ``source_seq`` is the seq of the first *retained* record, so the
        statement is "everything before this one is the recap" and it stays true
        however the list is later extended.
        """
        records = transcript.records
        source_seq = (
            records[cut].seq if cut < len(records) else (records[-1].seq + 1 if records else 0)
        )
        return cls(
            recap=recap,
            source_seq=source_seq,
            source_prefix_hash=transcript.prefix_hash(cut),
            source_count=cut,
            source_tokens=tokens,
            source_paths=tuple(dict.fromkeys(p for p in paths if p)),
            strategy=strategy,
            turn=turn,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            generation=generation,
        )


# ── the deterministic strategy ──────────────────────────────────────────────

#: How many recent assistant statements the deterministic recap keeps verbatim.
#: Cline keeps three, on the reasoning that the most recent statement of intent
#: is the one the next turn continues from; the two before it are what make it
#: legible as a direction rather than a sentence.
BASIC_KEEP_STATEMENTS = 3

#: Longest assistant line the deterministic recap will quote. A recap made of
#: three 4,000-character monologues is not a recap.
BASIC_STATEMENT_CHARS = 400


def basic_recap(records: Sequence[Record], *, turns: tuple[int, int] = (0, 0)) -> Recap:
    """Build a recap from what the records *are*, with no model call.

    The tier that must never fail. It is what a compaction falls back to when
    the summariser errors or returns something unparseable, what overflow
    recovery uses (a run that has just been refused for being too large should
    not answer by making another large request), and what a manual ``/compact``
    can use when nobody wants to pay for a summary.

    It is deliberately factual: which files were read, written and created,
    which tools ran, what the last few assistant statements were. No inference,
    so nothing in it can be wrong -- which is the property that matters when it
    is standing in for the summariser precisely because the summariser is not
    trustworthy right now.
    """
    read: dict[str, None] = {}
    wrote: dict[str, None] = {}
    tools: dict[str, int] = {}
    statements: list[str] = []
    failures: dict[str, None] = {}

    for record in records:
        if record.role == "tool":
            name = record.tool or (record.source.split(":", 1)[-1] if record.source else "")
            if name:
                tools[name] = tools.get(name, 0) + 1
            if record.path:
                if record.meta.get("mutation"):
                    wrote[record.path] = None
                else:
                    read[record.path] = None
            if record.meta.get("ok") is False:
                first = record.content.strip().splitlines()[:1]
                if first:
                    failures[f"{name or 'tool'}: {first[0][:160]}"] = None
        elif record.role == "assistant" and record.content.strip():
            statements.append(record.content.strip()[:BASIC_STATEMENT_CHARS])

    activity = tuple(
        f"{name} ran {count} time(s)" for name, count in sorted(tools.items(), key=lambda kv: -kv[1])
    )[:MAX_RECAP_ITEMS]

    return Recap(
        goal=(statements[-1] if statements else ""),
        files_modified=tuple(wrote)[:MAX_RECAP_ITEMS],
        files_read=tuple(read)[:MAX_RECAP_ITEMS],
        decisions=tuple(statements[-BASIC_KEEP_STATEMENTS:-1]),
        findings=activity,
        do_not_retry=tuple(failures)[:MAX_RECAP_ITEMS],
        turns=turns,
    )


# ── on-disk form ────────────────────────────────────────────────────────────


def dumps(state: CompactionState) -> str:
    return json.dumps(state.as_dict(), indent=1, sort_keys=True, default=str)


def loads(text: str) -> CompactionState | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return CompactionState.from_dict(parsed) if isinstance(parsed, Mapping) else None
