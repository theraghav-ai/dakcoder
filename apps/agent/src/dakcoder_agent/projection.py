"""What the model sees, derived from what actually happened.

The one rule
------------
``project()`` is a pure function of the canonical transcript, the compaction
sidecar and the pinned head. It writes nothing back. Every question the loop
used to answer from a ledger it maintained by hand -- has the model seen these
lines, has it seen this result body, was this call answered -- is answered here,
from the same pass that builds the request. That is the whole fix for the class
of failure where the agent asserts something about a context that has since
changed under it:

    the intercept said "lines 3777-3840 are already in context above"
    the compaction had evicted them
    the recap said "re-read one only if you need a line range you have not seen"
    the model asked, was refused, and asked again

Both statements were true when they were written and neither was true when it
was read. There is no version of that bug here, because nothing is written down
to go stale -- ``View.coverage`` is computed from the messages that are about to
be sent, on the turn they are sent.

The stages, in order
--------------------
1. **Compaction.** If the sidecar still describes the transcript, the records it
   replaces become one RECAP message. If it does not (a restored session, an
   edited journal), it is ignored and the caller compacts again.
2. **Visibility.** Display-only records never reach the wire.
3. **Caps.** Every tool result is capped *here*, not at insertion. The canonical
   record keeps the whole 400KB build log; the projection sends 12k of it,
   chosen by the tool's strategy, and reports which source lines survived.
4. **Echo collapse.** N identical (repeated call -> "answered from the earlier
   result") pairs in history are a few-shot demonstration of the behaviour the
   answer is asking the model to stop -- measured on the live endpoint, one pair
   and it moves on 5/5, two and it repeats forever 5/5. All but the newest
   collapse to a stub. Previously this was ``ContextManager.supersede``, which
   rewrote history to achieve it.
5. **Slice supersession.** A read whose every line is inside a later read of the
   same file becomes a stub pointing at the newer one. Containment, not path
   identity: a Planner that read one 6,571-line handler at 40-150, 153-205 and
   3777-3840 had the first two stubbed over lines that then existed nowhere.
   This is Cline's outdated-read rewrite, arrived at independently and applied
   at the same place -- projection -- so the full read stays in the record.
6. **Coherence.** One tool message per declared call, no orphans. A strict
   OpenAI-compatible endpoint rejects the whole conversation over one orphan.

Doing 3-5 at projection time rather than at insertion is what makes ``BUGS
L-8`` structurally impossible: the cap and the coverage ledger are two outputs
of one computation, so they cannot disagree about which lines the model can
read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from dakcoder_shared.llm import ToolCall
from dakcoder_shared.tokens import Calibration

from .compaction import CompactionState
from .messages import Layer, Message, Role, contains
from .transcript import Record, Transcript, Visibility

__all__ = [
    "DEFAULT_TOOL_CAP",
    "TOOL_CAPS",
    "Projector",
    "ToolCap",
    "View",
    "cap_for",
]


@dataclass(frozen=True, slots=True)
class ToolCap:
    """The cap applied to one tool's results at projection time (Part A §6.2)."""

    max_tokens: int
    #: How to elide. ``head`` keeps the beginning, ``tail`` the end, and
    #: ``errors`` keeps every line that looks like a compiler diagnostic.
    strategy: str = "tail"
    #: Rendered into the elision marker, telling the model how to get the rest.
    recover: str = ""


#: Per-tool caps. The shapes and strategies are Part A §6.2's; the numbers
#: were re-based when the prompt budget moved from 32,768 to the model window.
#:
#: At 32,768 a 6,000-token ``read_file`` cap was 18% of the budget and the
#: elision marker's advice — "re-read the file with a narrower line range" —
#: was survival. At 245,760 the same cap is 2.4%, and that advice *instructed*
#: the sliced re-reading loop two field transcripts died of. The caps below
#: still exist (an unbounded tool result is how one call eats a context), but
#: they are sized so an ordinary artefact — a whole Go file, a whole build log,
#: a whole search — lands intact.
#:
#: `go_build` and friends get the special strategy for a reason worth stating:
#: their error lines are the single most useful thing in the whole context, and
#: a naive head-or-tail truncation of a long build log throws away exactly the
#: `file:line:col` messages the agent needs while keeping the package list it
#: does not.
TOOL_CAPS: dict[str, ToolCap] = {
    "read_file": ToolCap(48_000, "head", "re-read the file with a narrower line range"),
    "repo_map": ToolCap(16_000, "head", 'call repo_map(package="<dir>") for one package in full'),
    "search_repo": ToolCap(16_000, "head", "narrow the pattern or pass a glob"),
    "go_build": ToolCap(12_000, "errors", "fix the reported errors and re-run"),
    "go_vet": ToolCap(12_000, "errors", "fix the reported findings and re-run"),
    "go_test": ToolCap(12_000, "errors", "re-run with a package pattern to narrow the output"),
    "rules_lint": ToolCap(8_000, "head", "pass `paths` to scope the lint to what you changed"),
    "legacy_audit": ToolCap(8_000, "head", "pass `paths` to scope the audit"),
    "go_diagnostics": ToolCap(8_000, "head", "narrow to one file with `path`"),
    # The review audits. `head` rather than the default `tail` for all four:
    # they are rendered worst-first, so the head is the part worth keeping —
    # and the default of tail-truncating a ranked report keeps the least
    # important findings and drops the N+1.
    "db_roundtrip_audit": ToolCap(2_500, "head", "the worst methods are listed first"),
    "validation_audit": ToolCap(2_500, "head", "fields are grouped by struct"),
    "temporal_audit": ToolCap(2_000, "head", "candidates only; no action is implied"),
    "lib_version_check": ToolCap(1_500, "head", "report only — do not edit go.mod"),
}

#: Everything not named above. §6.2's "everything else".
DEFAULT_TOOL_CAP = ToolCap(8_000, "tail", "call the tool again with narrower arguments")

#: Lines that must survive an `errors`-strategy elision. A build log is mostly
#: noise around a handful of these.
_DIAGNOSTIC_MARKERS = (
    ".go:",
    "error:",
    "Error:",
    "FAIL",
    "--- FAIL",
    "panic:",
    "cannot use",
    "undefined:",
    "declared and not used",
    "missing dependencies",
    "could not build arguments",
)

#: The prefix a superseded read carries. One string, so "is this message a
#: stub" is a question with one answer everywhere.
STALE_PREFIX = "[stale read of "


def cap_for(tool: str) -> ToolCap:
    return TOOL_CAPS.get(tool, DEFAULT_TOOL_CAP)


# ── the capping pass ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Capped:
    """One record's content after its cap, and what survived of it."""

    content: str
    #: Which source lines the message still carries. ``None`` means "not
    #: expressible as a range": a scattered ``errors`` elision, or a head cut so
    #: tight that no content line survived at all. A caller must treat that as
    #: *no* coverage, never as whole-file coverage.
    line_range: tuple[int, int] | None
    elided: int = 0


def apply_cap(
    content: str,
    cap: ToolCap,
    calibration: Calibration,
    *,
    path: str | None = None,
    line_range: tuple[int, int] | None = None,
) -> Capped:
    """Cap the content, and say which of its lines actually survived.

    The second field is the fix for BUG L-8. The cap is where the context stops
    agreeing with the tool: ``read_file`` hands over lines 1-8000 of a large
    file, the 48k-token cap keeps roughly the first third, and the loop then
    recorded the *tool's* span in its read ledger. The model was told two things
    that were each true on their own — the elision marker said "re-read with a
    narrower line range", the repeat intercept said "lines 6000-6500 are already
    in context above" — and could obey neither.

    Now the two are the same computation. ``View.coverage`` is built from these
    ranges, so there is no second place for the answer to differ.
    """
    tokens = calibration.estimate(content)
    if tokens <= cap.max_tokens:
        return Capped(content, line_range)

    lines = content.splitlines()
    if cap.strategy == "errors":
        kept, elided = _keep_diagnostics(lines, cap.max_tokens, calibration)
    elif cap.strategy == "head":
        kept, elided = _keep_edge(lines, cap.max_tokens, calibration, head=True)
    else:
        kept, elided = _keep_edge(lines, cap.max_tokens, calibration, head=False)

    survived = _surviving_range(
        line_range, total_lines=len(lines), kept=len(kept), strategy=cap.strategy
    )
    marker = _marker(elided, cap, path=path, line_range=line_range, survived=survived)
    if cap.strategy == "tail":
        return Capped(marker + "\n" + "\n".join(kept), survived, elided)
    return Capped("\n".join(kept) + "\n" + marker, survived, elided)


def _surviving_range(
    line_range: tuple[int, int] | None,
    *,
    total_lines: int,
    kept: int,
    strategy: str,
) -> tuple[int, int] | None:
    """Which source lines are still in the message after an elision.

    A read result is a header line followed by the file's lines, so the
    difference between the rendered line count and the span's width is the
    header. Deriving the offset rather than assuming one line keeps this honest
    if the renderer ever gains a second.
    """
    if line_range is None or strategy == "errors":
        return None
    low, high = line_range
    width = high - low + 1
    offset = total_lines - width
    if offset < 0:
        return None
    body = kept - offset if strategy == "head" else kept
    if body <= 0:
        return None
    if strategy == "head":
        return (low, min(high, low + body - 1))
    return (max(low, high - body + 1), high)


def _keep_edge(
    lines: list[str], budget: int, calibration: Calibration, *, head: bool
) -> tuple[list[str], int]:
    ordered = lines if head else list(reversed(lines))
    kept: list[str] = []
    used = 0
    for line in ordered:
        cost = calibration.estimate(line) + 1
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    if not head:
        kept.reverse()
    return kept, len(lines) - len(kept)


def _keep_diagnostics(
    lines: list[str], budget: int, calibration: Calibration
) -> tuple[list[str], int]:
    """Keep the diagnostic lines first, then fill with context around them.

    Diagnostics first: they are the agent's best fuel, and a build log that
    elided its own error messages is worse than no build log, because the agent
    will conclude the build passed.

    First, not *unconditionally*. Keeping every diagnostic whatever it cost meant
    a log made entirely of diagnostics ignored its cap completely -- 20,000
    ``undefined:`` lines from one broken import is roughly 230k tokens, which is
    a whole context window arriving through the one code path written to stop
    exactly that. Since the split, the record keeps the full log either way, so
    bounding what is *shown* costs nothing that cannot be recovered.

    Earliest first, because Go reports errors in source order and a cascade is
    explained by its first entry; the marker names how many were dropped, so the
    model is never left believing it has seen them all.
    """
    keep_flags = [any(m in line for m in _DIAGNOSTIC_MARKERS) for line in lines]

    kept_idx: list[int] = []
    used = 0
    for i, line in enumerate(lines):
        if not keep_flags[i]:
            continue
        cost = calibration.estimate(line) + 1
        if used + cost > budget and kept_idx:
            break
        kept_idx.append(i)
        used += cost

    for i, line in enumerate(lines):
        if keep_flags[i]:
            continue
        cost = calibration.estimate(line) + 1
        if used + cost > budget:
            continue
        kept_idx.append(i)
        used += cost

    kept_idx.sort()
    return [lines[i] for i in kept_idx], len(lines) - len(kept_idx)


def _marker(
    elided: int,
    cap: ToolCap,
    *,
    path: str | None,
    line_range: tuple[int, int] | None,
    survived: tuple[int, int] | None = None,
) -> str:
    """Render the elision marker.

    Always machine-readable and always actionable. An elision the model cannot
    see is one it treats as absence — it concludes the symbol it was looking for
    does not exist, and plans around a repository that has more in it than it
    was shown.
    """
    where = ""
    if path and line_range:
        where = f" of {path}:{line_range[0]}-{line_range[1]}"
    elif path:
        where = f" of {path}"
    kept = f"; lines {survived[0]}-{survived[1]} are above" if survived else ""
    recover = f" — {cap.recover}" if cap.recover else ""
    return f"[... {elided} line(s) elided{where}{kept}{recover} ...]"


# ── the view ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class View:
    """One projection: the request, and every derived fact about it.

    The derived facts are the point. They are computed in the same pass that
    builds ``messages``, from the same data, so a caller asking "can the model
    see this" and the model's actual request cannot disagree. Anything the loop
    needs to know about the state of the context is a field here, not a ledger
    somewhere else.
    """

    #: The layered list, as ``ContextManager.build`` returns it: the pinned
    #: head, the recap, the projected working set, the volatile block. This is
    #: what the budget is measured against and what an inspector renders.
    messages: tuple[Message, ...] = ()
    #: The same list with the tool-call invariant repaired, which is what
    #: actually goes on the wire. Separate from ``messages`` because a repair is
    #: a report of a defect: it must not silently become part of what everything
    #: else believes the context to be.
    wire_messages: tuple[Message, ...] = ()
    #: What the coherence pass had to repair. Empty is the only healthy value:
    #: the request is valid either way, but something upstream produced an
    #: invalid one and the loop reports that as an error event.
    repairs: tuple[str, ...] = ()
    #: Which lines of which files the model can read *in this request*.
    coverage: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    #: Digests of the tool-result bodies the model can read. Set by the caller
    #: at append time as ``meta["body"]``; the view reports which are visible.
    bodies: frozenset[str] = frozenset()
    #: ``tool_call_id`` of every result the model can read.
    answered: frozenset[str] = frozenset()
    #: Fingerprints whose result the model can still read: not stubbed, not
    #: hidden, not behind a compaction recap. What an intercept may honestly
    #: claim is "you already have this". Elision does not disqualify a result --
    #: the caller tracks separately whether its cached copy is partial -- but a
    #: result the model can no longer see at all does.
    intact: frozenset[str] = frozenset()
    #: How many records the compaction sidecar stands in for, 0 if none applies.
    compacted: int = 0
    #: Whether a sidecar existed but no longer describes the transcript. The
    #: caller's cue to compact again rather than to project a stale summary.
    compaction_stale: bool = False
    #: Reads collapsed to stubs, and repeated answers collapsed to stubs.
    stale_slices: int = 0
    collapsed_echoes: int = 0
    #: Records the caps elided lines from, and how many lines in total.
    elided_records: int = 0
    elided_lines: int = 0

    def holds(self, path: str, low: int, high: int) -> bool:
        """Whether lines ``low..high`` of ``path`` are readable in this request.

        The question the re-read intercept asks. It used to be asked of a ledger
        the loop maintained; asking it here is what makes the answer true.
        """
        for span_low, span_high in self.coverage.get(path, ()):  # type: ignore[union-attr]
            if span_low <= low and high <= span_high:
                return True
        return False

    def wire(self) -> list[dict[str, Any]]:
        return [m.wire() for m in self.wire_messages]


class Projector:
    """Builds views, and caches the one it just built.

    The cache is sound because the only thing that can change the answer is an
    append (``Transcript.version``), a new sidecar, a changed pinned head, or a
    moved calibration ratio -- and all four are in the key. It matters because
    ``build``, ``wire``, ``usage`` and ``should_compact`` all want the same view
    on the same turn, and the capping pass is the expensive part of a turn that
    is not a model call.
    """

    __slots__ = ("_key", "_view", "_caps")

    def __init__(self) -> None:
        self._key: tuple[Any, ...] | None = None
        self._view: View | None = None
        #: (seq, cap, ratio) -> Capped. Records are immutable, so a cap once
        #: computed is valid until the calibration ratio moves.
        self._caps: dict[tuple[int, int, str, int], Capped] = {}

    def project(
        self,
        transcript: Transcript,
        *,
        calibration: Calibration,
        compaction: CompactionState | None = None,
        head: Sequence[Message] = (),
        tail: Sequence[Message] = (),
        supersede_slices: bool = True,
        overrides: Mapping[int, str | None] | None = None,
        turn: int = 0,
    ) -> View:
        overrides = overrides or {}
        key = (
            transcript.version,
            len(transcript),
            id(compaction),
            getattr(compaction, "source_prefix_hash", ""),
            tuple((m.role, m.content) for m in head),
            tuple((m.role, m.content) for m in tail),
            supersede_slices,
            tuple(sorted(overrides.items())),
            round(calibration.ratio, 3),
            turn,
        )
        if self._key == key and self._view is not None:
            return self._view
        view = self._build(
            transcript,
            calibration=calibration,
            compaction=compaction,
            head=head,
            tail=tail,
            supersede_slices=supersede_slices,
            overrides=overrides,
            turn=turn,
        )
        self._key, self._view = key, view
        return view

    def invalidate(self) -> None:
        self._key, self._view = None, None

    # -- the pass -----------------------------------------------------------

    def _capped(self, record: Record, calibration: Calibration) -> Capped:
        cap = cap_for(record.tool)
        key = (record.seq, cap.max_tokens, cap.strategy, int(calibration.ratio * 100))
        hit = self._caps.get(key)
        if hit is None:
            hit = apply_cap(
                record.content,
                cap,
                calibration,
                path=record.path,
                line_range=record.line_range,
            )
            if len(self._caps) > 4_000:  # pragma: no cover - housekeeping
                self._caps.clear()
            self._caps[key] = hit
        return hit

    def _build(
        self,
        transcript: Transcript,
        *,
        calibration: Calibration,
        compaction: CompactionState | None,
        head: Sequence[Message],
        tail: Sequence[Message],
        supersede_slices: bool,
        overrides: Mapping[int, str | None],
        turn: int,
    ) -> View:
        records = transcript.records

        # 1. compaction ----------------------------------------------------
        cut = 0
        stale_sidecar = False
        recap_message: Message | None = None
        if compaction is not None:
            if compaction.matches(transcript):
                cut = compaction.cut(transcript)
                recap_message = Message(
                    Role.USER,
                    compaction.recap.markdown(),
                    Layer.RECAP,
                    source="recap",
                    turn=turn or compaction.turn,
                )
            else:
                stale_sidecar = True

        # 2. visibility ----------------------------------------------------
        working = [
            r
            for r in records[cut:]
            if r.visibility is not Visibility.DISPLAY and overrides.get(r.seq, "") is not None
        ]

        # 3. caps ----------------------------------------------------------
        capped: list[Capped | None] = []
        for record in working:
            capped.append(self._capped(record, calibration) if record.role == "tool" else None)

        # 4. echo collapse -------------------------------------------------
        # Keep only the newest answer to each repeated question. The older ones
        # are what teach the model to keep asking.
        stubbed: dict[int, str] = {}
        # A caller's explicit override wins over every automatic rule below: it
        # is the one that was asked for by name.
        for index, record in enumerate(working):
            replacement = overrides.get(record.seq)
            if replacement is not None and record.seq in overrides:
                stubbed[index] = replacement
        newest_echo: dict[str, int] = {}
        for index, record in enumerate(working):
            fingerprint = record.meta.get("echo")
            if fingerprint:
                newest_echo[str(fingerprint)] = index
        collapsed = 0
        for index, record in enumerate(working):
            fingerprint = record.meta.get("echo")
            if not fingerprint or newest_echo.get(str(fingerprint)) == index:
                continue
            stubbed[index] = (
                f"[{record.tool or 'that call'} was asked again with these arguments "
                "and answered from the earlier result; the newest answer is below]"
            )
            collapsed += 1

        # 5. slice supersession --------------------------------------------
        # Containment, not path identity, and computed against the ranges that
        # survived the cap rather than the ones the tool returned.
        stale = 0
        if supersede_slices:
            for index, record in enumerate(working):
                if index in stubbed or record.role != "tool" or not record.path:
                    continue
                mine = capped[index].line_range if capped[index] else record.line_range
                for later_index in range(index + 1, len(working)):
                    later = working[later_index]
                    if later_index in stubbed or later.role != "tool":
                        continue
                    if later.path != record.path:
                        continue
                    theirs = (
                        capped[later_index].line_range
                        if capped[later_index]
                        else later.line_range
                    )
                    # A later read that itself delivered nothing supersedes
                    # nothing: its `None` means "the cap kept no body line",
                    # which would otherwise read as "the whole file".
                    if theirs is None and later.line_range is not None:
                        continue
                    if not contains(theirs, mine):
                        continue
                    where = f" lines {mine[0]}-{mine[1]}" if mine else ""
                    stubbed[index] = (
                        f"{STALE_PREFIX}{record.path}{where}: those lines are inside "
                        "the newer read of this file below]"
                    )
                    stale += 1
                    break

        # 6. emit ----------------------------------------------------------
        out: list[Message] = list(head)
        if recap_message is not None:
            out.append(recap_message)

        coverage: dict[str, list[tuple[int, int]]] = {}
        bodies: set[str] = set()
        answered: set[str] = set()
        intact: set[str] = set()
        elided_records = 0
        elided_lines = 0

        for index, record in enumerate(working):
            stub = stubbed.get(index)
            cut_result = capped[index]
            if stub is not None:
                content, span = stub, None
            elif cut_result is not None:
                content, span = cut_result.content, cut_result.line_range
                if cut_result.elided:
                    elided_records += 1
                    elided_lines += cut_result.elided
            else:
                content, span = record.content, record.line_range

            try:
                role = Role(record.role)
            except ValueError:  # pragma: no cover - defensive
                role = Role.USER
            try:
                layer = Layer(record.layer)
            except ValueError:  # pragma: no cover - defensive
                layer = Layer.WORKING_SET

            out.append(
                Message(
                    role,
                    content,
                    layer,
                    source=record.source,
                    path=record.path,
                    line_range=span,
                    tool_call_id=record.tool_call_id,
                    tool_calls=record.tool_calls,
                    turn=record.turn,
                    seq=record.seq,
                )
            )

            if role is not Role.TOOL:
                continue
            if record.tool_call_id:
                answered.add(record.tool_call_id)
            if stub is not None:
                continue
            if body := record.meta.get("body"):
                bodies.add(str(body))
            if record.path and span is not None:
                coverage.setdefault(record.path, []).append(span)
            if fingerprint := record.meta.get("fingerprint"):
                intact.add(str(fingerprint))

        out.extend(tail)

        repaired, repairs = _coherent(out)
        return View(
            messages=tuple(out),
            wire_messages=tuple(repaired),
            repairs=repairs,
            coverage={p: tuple(spans) for p, spans in coverage.items()},
            bodies=frozenset(bodies),
            answered=frozenset(answered),
            intact=frozenset(intact),
            compacted=cut,
            compaction_stale=stale_sidecar,
            stale_slices=stale,
            collapsed_echoes=collapsed,
            elided_records=elided_records,
            elided_lines=elided_lines,
        )


def _coherent(messages: Sequence[Message]) -> tuple[list[Message], tuple[str, ...]]:
    """Return the list with every declared call answered and no orphaned result.

    One ``role: "tool"`` message per declared ``tool_call_id``, no tool message
    whose call nothing declares: that is not a convention, it is the condition
    for the request being accepted at all. A strict OpenAI-compatible endpoint
    rejects the whole conversation over a single orphan.

    Two repairs, both information-preserving:

    * A declared call with no result gets a synthesised one saying it did not
      run. It is placed at the end of its assistant's block — the next assistant
      message, or the end of the list — because a batch's results are not always
      contiguous (a retrieval-overlap note is a ``role: user`` message appended
      between two results of the same batch).
    * A result whose call no assistant declares becomes a ``role: user`` message
      carrying the same text. Dropping it would delete something the model was
      told; leaving it would be malformed.

    This is a backstop, not the fix. When it fires, something upstream is wrong
    and should be repaired there — hence ``View.repairs``, which the loop turns
    into an ERROR event rather than a silent recovery.
    """
    declared: set[str] = set()
    answered: set[str] = set()
    for message in messages:
        for call in message.tool_calls:
            declared.add(call.id)
        if message.tool_call_id:
            answered.add(message.tool_call_id)

    if declared == answered:
        return list(messages), ()

    repairs: list[str] = []
    out: list[Message] = []
    pending: list[ToolCall] = []

    def flush() -> None:
        for call in pending:
            repairs.append(f"unanswered call {call.name}#{call.id}")
            out.append(
                Message(
                    role=Role.TOOL,
                    content=f"{call.name} was not run: the run moved on before "
                    "this call was dispatched.",
                    layer=Layer.WORKING_SET,
                    source="wire-repair",
                    tool_call_id=call.id,
                )
            )
        pending.clear()

    for message in messages:
        if message.role is Role.ASSISTANT:
            flush()
            out.append(message)
            pending.extend(call for call in message.tool_calls if call.id not in answered)
            continue
        if message.tool_call_id and message.tool_call_id not in declared:
            repairs.append(f"orphaned result {message.source or message.role}")
            out.append(
                Message(
                    role=Role.USER,
                    content=f"[a tool result whose call is no longer in context]\n"
                    f"{message.content}",
                    layer=message.layer,
                    source=message.source,
                    turn=message.turn,
                    seq=message.seq,
                )
            )
            continue
        out.append(message)
    flush()
    return out, tuple(repairs)
