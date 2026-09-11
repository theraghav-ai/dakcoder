"""The context manager: the only component allowed to build a message list.

Why this is a component and not a helper
----------------------------------------
The frontend agent has no context management inside a run. ``AgentRun.messages``
is append-only across up to forty turns; the only trimming anywhere in that
codebase is a forty-*message* cap that applies solely to resumed sessions. Tool
results enter history untruncated — one ``read_file`` can contribute 25k tokens
and stay there for the rest of the task. ``repo_map`` alone contributes 20-30k,
permanently, from turn one.

Worked out for a 25-turn brownfield task (Part A §5.2)::

    fixed overhead per turn            5,700 tok
    repo_map, resident from turn 1    25,000 tok
    average new content per turn       1,500 tok

    prompt at turn 25  ~ 5,700 + 25,000 + 25 x 1,500        ~    68,000 tok
    total prefill      ~ 25 x 30,700 + 1,500 x (25*26/2)    ~ 1,250,000 tok

Roughly 95% of that is recomputation of a prefix that never changed. None of it
is a criticism of a system that shipped and works — it is what happens when
context is nobody's component. Here it is a component, owned and budgeted, and
``tests/test_budget_regression.py`` is the CI gate that keeps it that way.

Canonical, and projected
------------------------
This class used to own one list that was three things at once: the record of the
conversation, the working set the budget was enforced against, and the request
that went on the wire. Compaction rewrote that list, so after the first
compaction there was no record of what had happened, the loop's ledgers
described messages that no longer existed, and nine of them had to be cleared or
rebuilt on every eviction to stop the agent asserting things about a transcript
that had changed under it.

It now owns two things that cannot disagree:

* a :class:`~dakcoder_agent.transcript.Transcript` — append-only, whole tool
  results, never rewritten, never evicted from. What actually happened.
* a :class:`~dakcoder_agent.compaction.CompactionState` — a sidecar saying
  which leading records a recap stands in for.

Everything the model sees is :func:`~dakcoder_agent.projection.Projector.project`
of those two plus the pinned head, recomputed on the turn it is sent. Caps,
superseded reads and collapsed repeats are all things the *projection* does; the
record keeps the full text. "Can the model read lines 300-400 of this file" is
answered by ``view.holds`` from the same pass that built the request, so it
cannot be stale — which is the whole of the fix for the loop-and-context
disagreement two field transcripts died of.

The four disciplines
--------------------
**Budget.** A hard prompt cap per mode, allocated across layers in eviction
order. The cap is a *quality* decision as much as a latency one: the
context-rot literature is consistent that accuracy degrades with input length
across every frontier model tested, so a large window is not free even when the
GPU allows it.

**Caps.** Every tool result is capped on the way to the model, and the cap
always leaves a machine-readable marker, so the model knows it can re-read
rather than concluding the content does not exist. The uncapped result stays in
the transcript, which is what makes "show me what the tool actually returned" a
question with an answer.

**The file-slice ledger.** An agent that reads a file, patches it, re-reads and
finds both copies in context will reason about the stale one. Superseded slices
collapse to a stub naming where the live lines are — at projection time, so
nothing is lost.

**One assembler.** Nothing else builds a message list. The moment two places can
append to history, the budget stops being enforceable and the prefix stops being
stable.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from dakcoder_shared.llm import ToolCall
from dakcoder_shared.tokens import Calibration

from .compaction import (
    MAX_RECAP_ITEMS,
    RECAP_BUDGET_TOKENS,
    CompactionState,
    Recap,
    basic_recap,
)
from .messages import PINNED_LAYERS, Layer, Message, Role, contains, parseable_arguments
from .modes import Mode, ModeConfig, config_for
from .projection import (
    DEFAULT_TOOL_CAP,
    TOOL_CAPS,
    Projector,
    ToolCap,
    View,
    cap_for,
)
from .transcript import Record, Transcript, Visibility, digest_of

__all__ = [
    "DEFAULT_TOOL_CAP",
    "MAX_RECAP_ITEMS",
    "PINNED_LAYERS",
    "RECAP_BUDGET_TOKENS",
    "TOOL_CAPS",
    "CompactionState",
    "ContextManager",
    "Eviction",
    "Layer",
    "Message",
    "OverBudgetError",
    "Recap",
    "Role",
    "ToolCap",
    "Transcript",
    "Usage",
    "View",
    "Visibility",
    "basic_recap",
    "cap_for",
    "contains",
    "parseable_arguments",
]

#: How many developer directives the pinned task block may carry at once.
#:
#: Bounded because the layer is pinned and a long conversation would otherwise
#: grow it without limit. Six is well past any run that is still going well; the
#: oldest is dropped first, and every one of them is also in the transcript.
MAX_DIRECTIVES = 6

#: How many mode instructions the pinned head may carry at once.
#:
#: **One.** The head carries the instruction that is in force and nothing else.
#:
#: It was six, with each new overlay opening "This replaces the mode
#: instructions above it", and that was a bound on a problem rather than a fix
#: for it. A run walking the escalation ladder switched mode on almost every
#: turn -- fourteen switches in fifteen turns in the field transcript -- so the
#: un-evictable head accumulated five Coder overlays and four Verifier ones,
#: each contradicting the one above. The Verifier, whose overlay opens "Report;
#: do not fix anything here", announced "My job is to make the edit" on four
#: separate turns: it was reading the Coder's instruction, still sitting two
#: messages up. A 27B model at temperature 0.1 picks whichever instruction it
#: saw most recently, and the preamble asking it not to is one more sentence in
#: the pile.
MAX_MODE_MESSAGES = 1


class OverBudgetError(RuntimeError):
    """Raised when a prompt cannot be brought inside its budget.

    Distinct from silently truncating: an assembled prompt that quietly dropped
    the task description would produce a confident answer to the wrong question.
    """


@dataclass(frozen=True, slots=True)
class Usage:
    """Per-layer token accounting for one assembled prompt."""

    by_layer: dict[Layer, int]
    total: int
    budget: int
    tools: int = 0

    @property
    def used_pct(self) -> float:
        return 0.0 if self.budget <= 0 else round(100.0 * self.total / self.budget, 1)

    @property
    def over_budget(self) -> bool:
        return self.total > self.budget


@dataclass(frozen=True, slots=True)
class Eviction:
    """What the last compaction stopped showing the model.

    Named "eviction" for continuity, but nothing is evicted any more: the
    records are still in the transcript and the sidecar simply projects a recap
    over them. It is returned as data because the loop reports it, and because
    the recap names the files whose contents are no longer visible.
    """

    paths: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    messages: int = 0
    tokens: int = 0


# A summariser turns the messages the compaction is about to hide into a Recap.
# Injected rather than imported so the context manager stays testable without a
# model: §6.5 runs compaction on the `fast` role, which today resolves to the
# same 27B model, so a real compaction is a real model call and its cost shows
# up in telemetry.
Summariser = Callable[[Sequence[Message]], Recap]


class ContextManager:
    """Owns the transcript and the projection for one run.

    Nothing else assembles messages. That is the whole point: the moment two
    places can append to history, the budget stops being enforceable and the
    prefix stops being stable.
    """

    def __init__(
        self,
        *,
        mode: Mode | str = Mode.ASK,
        system_prompt: str,
        tool_schema_tokens: int = 0,
        calibration: Calibration | None = None,
        compact_at: float = 0.70,
        transcript: Transcript | None = None,
        compaction: CompactionState | None = None,
    ) -> None:
        self._config: ModeConfig = config_for(mode)
        self._calibration = calibration or Calibration()
        self._compact_at = compact_at
        self._turn = 0
        self._compactions = 0

        # The pinned head. Byte-identical for every mode and every task in the
        # repository, which is what makes it a permanent prefix-cache hit —
        # roughly 2.4k tokens that never need prefilling again.
        self._system = Message(Role.SYSTEM, system_prompt, Layer.SYSTEM, source="system")
        self._tool_schema_tokens = tool_schema_tokens

        self._mode_messages: list[Message] = []
        #: The last raw mode instruction, for the dedupe in `switch_mode`.
        self._last_mode_instruction = ""
        self._task: Message | None = None
        #: The plan and the directives, rendered below the working set. Pinned,
        #: like the task, but never part of the cacheable head.
        self._directive_message: Message | None = None
        self._task_text = ""
        self._plan_text = ""
        self._acceptance: tuple[str, ...] = ()
        #: Follow-ups and corrections, pinned. See ``pin_directive``.
        self._directives: list[str] = []
        #: The loop's turn-scoped statement of where the task stands, rendered
        #: last in the volatile block. See ``set_state``.
        self._state_text = ""

        #: What actually happened. Append-only, whole tool results.
        self._transcript = transcript if transcript is not None else Transcript()
        #: Which leading records a recap stands in for, if any.
        self._compaction = compaction
        self._projector = Projector()
        #: Projection-time content overrides, keyed by record seq. The narrow
        #: escape hatch for a caller that wants an earlier message to *read*
        #: differently; ``None`` hides it. The record itself is never touched,
        #: so nothing here can put the canonical transcript out of step with
        #: what the model sees -- it is an input to the projection, not a patch
        #: applied after it.
        self._overrides: dict[int, str | None] = {}

        #: What the last compaction stopped showing. Read by the loop.
        self._last_eviction = Eviction()

        #: Where the canonical transcript and the sidecar are written, if
        #: anywhere. Optional so a test, a CLI run or an in-memory session costs
        #: no disk; attached by the runtime for a session that must survive a
        #: daemon restart.
        self._journal: Any | None = None
        #: How many records have already been written. The transcript only ever
        #: grows, so persisting it is an append of the tail rather than a
        #: rewrite -- which is what makes it affordable to do every turn.
        self._persisted = 0
        #: The sidecar as last written, so an unchanged one is not rewritten.
        self._persisted_compaction: str = ""

    # ── persistence ─────────────────────────────────────────────────────────

    def attach_journal(self, journal: Any, *, persisted: int = 0) -> None:
        """Write the canonical transcript and the sidecar through ``journal``.

        Best-effort by construction: ``Journal`` swallows its own IO errors, so
        a read-only checkout costs the ability to resume, never the run.
        """
        self._journal = journal
        self._persisted = persisted

    def persist(self) -> None:
        """Write whatever is new. Called at a turn boundary, where a run already waits.

        Two writes, and both are small. The records since the last call are
        appended, because the transcript is append-only and there is therefore
        nothing to rewrite. The sidecar is rewritten only when it has changed,
        which is once per compaction.

        This is what makes ``rehydrate`` able to restore *the context the run was
        using* rather than a differently-derived approximation of it: the recap
        and the boundary it applies at are on disk beside the records they
        describe, and the hash inside says whether they still belong together.
        """
        if self._journal is None:
            return
        records = self._transcript.records
        if len(records) > self._persisted:
            self._journal.append_records([r.as_dict() for r in records[self._persisted :]])
            self._persisted = len(records)
        payload = self._compaction.as_dict() if self._compaction else None
        marker = json.dumps(payload, sort_keys=True, default=str) if payload else ""
        if marker != self._persisted_compaction:
            self._journal.write_compaction(payload)
            self._persisted_compaction = marker

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def mode(self) -> Mode:
        return self._config.mode

    @property
    def budget(self) -> int:
        return self._config.prompt_budget

    @property
    def transcript(self) -> Transcript:
        """The canonical record. Read-only by convention and by API surface."""
        return self._transcript

    @property
    def compaction(self) -> CompactionState | None:
        """The compaction sidecar, or ``None`` when nothing has been compacted."""
        return self._compaction

    def observe_tool_schemas(self, tokens: int) -> None:
        """Record what the tools array costs on the wire.

        The schemas are part of the prompt and are sent on every call, but the
        constructor takes them as a number the caller supplies once — and the
        runtime never supplied one. ``serve.py`` built every session's context
        with the default of zero, so thirteen Planner schemas, about 1.4k
        tokens, were charged to the endpoint and counted as nothing here.

        Everything downstream is decided against that figure: when to compact,
        how much to retain, and whether ``complete`` refuses the turn.

        Measured per turn rather than fixed, because mode filtering means the
        array differs by mode: the Planner is offered read-only tools and the
        Coder the write ones.
        """
        self._tool_schema_tokens = max(0, tokens)

    @property
    def turn(self) -> int:
        return self._turn

    @property
    def compactions(self) -> int:
        return self._compactions

    # ── assembly ────────────────────────────────────────────────────────────

    def _head(self) -> list[Message]:
        out: list[Message] = [self._system]
        out.extend(self._mode_messages)
        if self._task is not None:
            out.append(self._task)
        return out

    def _tail(self) -> list[Message]:
        return [self._directive_message] if self._directive_message is not None else []

    def view(self) -> View:
        """Project the transcript into what this turn's request will be.

        The single derivation. ``build``, ``wire``, ``usage``, ``coverage`` and
        every "can the model see this" question are views of this one result,
        which is why they cannot disagree with each other or with the bytes that
        go on the wire.
        """
        return self._projector.project(
            self._transcript,
            calibration=self._calibration,
            compaction=self._compaction,
            head=self._head(),
            tail=self._tail(),
            supersede_slices=self.SUPERSEDE_SLICES,
            overrides=self._overrides,
            turn=self._turn,
        )

    def build(self) -> list[Message]:
        """Assemble the message list.

        The only builder. Order is fixed, and the head is stable:

            system -> mode -> task -> recap -> working set -> plan & directives

        **Why the last layer is last** (BUG L-18). Everything a steer or a plan
        submission mutates lives at the end. It used to live in the ``task``
        block, three messages from the top, and a prefix cache is a prefix: one
        changed byte there invalidates every token after it. The manager's own
        ``novel_tokens`` — "what a prefix cache actually has to prefill" — put
        numbers on it, over a context of the shape a migration run reaches:

            turns   prompt      a steer re-prefills
                5    8,633    3,854   (44.6%)
               20   19,983   15,204   (76.1%)
               50   42,693   37,914   (88.8%)
              100   80,543   75,764   (94.1%)
              200  156,243  151,464   (96.9%)

        The same words appended to the working set instead cost 11 tokens. So a
        developer typing one sentence at turn 100 paid to re-read the entire
        conversation, and ``set_plan`` — which fires on every plan submission —
        paid the same.

        Moving the *whole* task block down would have worked too and would have
        cost more: the task statement and the acceptance criteria never change
        after ``set_task``, so they are the stable head, and only the plan and
        the directives mutate.
        """
        return list(self.view().messages)

    def wire(self) -> list[dict[str, Any]]:
        """Assemble in the shape the API expects, with the tool-call invariant repaired.

        One ``role: "tool"`` message per declared ``tool_call_id``, no tool
        message whose call nothing declares: that is not a convention, it is the
        condition for the request being accepted at all. A strict
        OpenAI-compatible endpoint rejects the whole conversation over a single
        orphan.

        The repair is a backstop, not the fix. When it fires, something upstream
        is wrong and should be repaired there — hence ``wire_repairs``, which the
        loop turns into an ERROR event rather than a silent recovery.
        """
        return self.view().wire()

    @property
    def last_eviction(self) -> Eviction:
        """What the most recent ``compact()`` stopped showing the model."""
        return self._last_eviction

    @property
    def wire_repairs(self) -> tuple[str, ...]:
        """What the last projection had to repair. Empty is the only healthy value."""
        return self.view().repairs

    def prefix_signature(self) -> str:
        """A stable identifier for the cacheable head.

        Exposed so telemetry can alert when it changes. §18 makes a falling
        prefix-cache hit rate an alert rather than a dashboard, and this is the
        signal that says *why* it fell — but note the caveat: mode filtering
        means the tool schemas differ per mode (§7.1) while §6.4 asserts the
        ``system + schemas`` prefix is identical across phases. Both cannot be
        literally true. What is enforced here is the stronger half and the one
        that dominates: the system message is byte-identical across every mode
        and every turn.
        """
        return f"{len(self._system.content)}:{hash(self._system.content) & 0xFFFFFFFF:08x}"

    # ── the pinned head ─────────────────────────────────────────────────────

    def set_task(self, task: str, *, plan: str = "", acceptance: Sequence[str] = ()) -> None:
        """Pin the task, the plan and the acceptance criteria.

        Replaced rather than appended, because there is exactly one task per
        run — and it sits above the working set so compaction can never reach
        it.
        """
        self._task_text = task.strip()
        self._plan_text = plan.strip()
        self._acceptance = tuple(acceptance)
        self._rebuild_task()

    def set_plan(self, plan: str) -> None:
        """Pin the plan the Planner produced, keeping the task and criteria.

        A separate method rather than another ``set_task`` call, because the
        caller would otherwise have to hold the task and the acceptance criteria
        itself just to re-supply them — two copies of the same state, one of
        which will eventually be stale.
        """
        self._plan_text = plan.strip()
        self._rebuild_task()

    def set_state(self, text: str) -> None:
        """Pin the loop's statement of the task state for this turn.

        The loop knows exactly what has been written, what the plan asked for
        and what the gate said, and the model was told none of it: its only
        evidence about its own progress was the transcript, including its own
        statements of intent, which is how "I'll write migration.md" came to be
        read as a report of completion. This block is rebuilt from ground truth
        every turn and rendered at the end of the volatile layer.

        Derived, not model-written: a TodoWrite the model maintains can lie; a
        block rendered from ``router.touched`` cannot. Identical text is a no-op
        so the volatile block stays byte-stable across turns where nothing moved.
        """
        text = text.strip()
        if text == self._state_text:
            return
        self._state_text = text
        self._rebuild_task()

    def pin_directive(self, text: str) -> None:
        """Keep something the developer said where compaction cannot reach it.

        A follow-up and a mid-run correction both arrive as ordinary user
        messages in the working set, which is the layer compaction hides first.
        So the developer's instruction is the *first* thing to stop being
        visible, and in a run that compacts every few turns it is gone within
        two — which is how a session answered "hi" by carrying on reading the
        same two files for another forty turns.

        That defeats the point of steering. Its whole promise is that a wrong
        turn at turn 12 can be corrected without ending the run, and a
        correction that evaporates at turn 14 is worse than no correction at
        all: the developer believes the run was redirected.

        Pinned as well as appended, not instead. The transcript copy keeps the
        conversational position — the message sits after the answers it follows
        — and this copy keeps the instruction alive.
        """
        directive = text.strip()
        if not directive or directive in self._directives:
            return
        self._directives.append(directive)
        del self._directives[:-MAX_DIRECTIVES]
        self._rebuild_task()

    @property
    def task_text(self) -> str:
        return self._task_text

    @property
    def directives(self) -> tuple[str, ...]:
        return tuple(self._directives)

    @property
    def acceptance(self) -> tuple[str, ...]:
        return self._acceptance

    def _rebuild_task(self) -> None:
        """Rebuild both halves of what used to be one pinned block.

        The stable half — what the run was asked to do, and what it is measured
        against — is written once by ``set_task`` and never again. The volatile
        half is rebuilt on every plan submission and every steer, and it is
        assembled at the *end* of the prompt so that rebuilding it costs the
        tokens it contains rather than every token above it. See ``build``.
        """
        parts = [f"# Task\n{self._task_text}"]
        if self._acceptance:
            criteria = "\n".join(f"- {c}" for c in self._acceptance)
            parts.append(f"\n# Accepts\n{criteria}")
        self._task = Message(Role.USER, "\n".join(parts), Layer.TASK, source="task")

        volatile: list[str] = []
        if self._plan_text:
            volatile.append(f"# Plan\n{self._plan_text}")
        if self._directives:
            since = "\n".join(f"- {d}" for d in self._directives)
            volatile.append(f"# Since then, the developer has said\n{since}")
        if self._state_text:
            # Last, so the ground truth about the work is the closest thing to
            # the model's next token, and so that rebuilding it every turn
            # costs its own ~150 tokens and nothing above it.
            volatile.append(self._state_text)
        self._directive_message = (
            Message(Role.USER, "\n\n".join(volatile), Layer.DIRECTIVE, source="directive")
            if volatile
            else None
        )

    def switch_mode(self, mode: Mode | str, instruction: str) -> None:
        """Move to a new mode by *appending* its instruction.

        Not by rebuilding the list with a different system prompt. That is
        finding S6, and it is what makes a planner-to-coder handoff cost one
        message rather than a full prefill of everything already in context.

        Bounded, though, which it was not. ``MODE`` is a pinned layer, so
        compaction can never reach it, and a run that walks the escalation
        ladder switches mode on almost every turn: one session reached thirteen
        mode instructions stacked in the head, five of them the Coder's and four
        the Verifier's, each contradicting the one above it.
        """
        self._config = config_for(mode)
        text = instruction.strip()

        # One overlay, so there is never a question of which is in force.
        if self._mode_messages and self._last_mode_instruction == text:
            return

        self._mode_messages.append(
            Message(Role.USER, text, Layer.MODE, source=f"mode:{self._config.mode}")
        )
        self._last_mode_instruction = text
        del self._mode_messages[:-MAX_MODE_MESSAGES]

    # ── appending ───────────────────────────────────────────────────────────

    def begin_turn(self) -> int:
        self._turn += 1
        return self._turn

    def _project_one(self, record: Record) -> Message:
        """The projected form of a record just appended.

        Returned rather than the record itself because callers ask what the
        *model* got: ``append_tool_result``'s caller writes its read ledger from
        the returned ``line_range``, and that has to be the span that survived
        the cap, not the span the tool returned (BUG L-8). Since the cap now
        happens here, the two are one computation and cannot drift.
        """
        for message in reversed(self.view().messages):
            if message.seq == record.seq:
                return message
        # Only reachable for a display-only record, which is not on the wire.
        return Message(
            Role(record.role),
            record.content,
            Layer(record.layer),
            source=record.source,
            path=record.path,
            line_range=record.line_range,
            tool_call_id=record.tool_call_id,
            tool_calls=record.tool_calls,
            turn=record.turn,
            seq=record.seq,
        )

    def append_assistant(
        self, content: str, *, tool_calls: tuple[ToolCall, ...] = ()
    ) -> Message:
        record = self._transcript.append(
            "assistant",
            content,
            source="assistant",
            tool_calls=tool_calls,
            turn=self._turn,
        )
        return self._project_one(record)

    def append_user(self, content: str, *, visibility: Visibility = Visibility.BOTH) -> Message:
        """A follow-up or a steering message from the developer mid-run.

        ``visibility`` exists for the synthetic ones -- a retrieval-overlap
        note, a stall reminder, a hook's context block. They are addressed to
        the model and are not something the developer said, and rendering them
        in the transcript as though they were is how a panel comes to show
        instructions nobody typed.
        """
        record = self._transcript.append(
            "user", content, source="user", turn=self._turn, visibility=visibility
        )
        return self._project_one(record)

    def append_tool_result(
        self,
        tool: str,
        content: str,
        *,
        tool_call_id: str = "",
        path: str | None = None,
        line_range: tuple[int, int] | None = None,
        fingerprint: str = "",
        echo: str = "",
        body: str = "",
        ok: bool | None = None,
        mutation: bool = False,
    ) -> Message:
        """Record a tool result **whole**, and return what the model will see of it.

        The cap used to be applied here, and the uncapped result was then gone:
        a 400KB build log was cut to 12k on the way in and the other 388KB
        existed nowhere. The result now enters the transcript in full and is
        capped on the way out (``projection.apply_cap``), so "what did the tool
        actually return" has an answer, the recap can be built from more than
        the model happened to be shown, and the cap can be re-tuned without
        re-running the tool.

        ``echo`` carries the call fingerprint when this result is a replay from
        a ledger rather than a fresh dispatch. The projection keeps only the
        newest such answer per fingerprint and stubs the rest -- measured on the
        live endpoint, one (repeat -> "answered from the earlier result") pair
        in history and the model moves on 5/5, two and it repeats the call
        forever 5/5, whatever the answer says.
        """
        meta: dict[str, Any] = {}
        if fingerprint:
            meta["fingerprint"] = fingerprint
        if echo:
            meta["echo"] = echo
        if ok is not None:
            meta["ok"] = bool(ok)
        if mutation:
            meta["mutation"] = True
        # The caller's digest when it has one -- the loop fingerprints a body
        # by (tool name + rendered result), which is a different string from the
        # raw content and is the one its "have I seen this before" test uses.
        meta["body"] = body or digest_of(content)

        record = self._transcript.append(
            "tool",
            content,
            source=f"tool:{tool}",
            tool=tool,
            path=path,
            line_range=line_range,
            tool_call_id=tool_call_id or None,
            turn=self._turn,
            meta=meta,
        )
        return self._project_one(record)

    def supersede(self, message: Message, text: str) -> Message | None:
        """Make an earlier message *read* differently, without rewriting history.

        The escape hatch, and it is narrower than it used to be. It registers a
        projection-time override keyed by the record's ``seq``; the record keeps
        its bytes, keeps its place and keeps its ``tool_call_id``, so nothing is
        orphaned and the canonical transcript still says what happened.

        Prefer the ``echo`` argument to ``append_tool_result``: the projection
        collapses repeated answers on its own, which is the same effect with
        nothing for a caller to forget.

        Returns the overridden message, or ``None`` when the message is not in
        the transcript -- it may predate a rehydration, which removed the
        pattern by another route.
        """
        if message.seq < 0 or self._transcript.by_seq(message.seq) is None:
            return None
        self._overrides[message.seq] = text
        self._projector.invalidate()
        return self._project_one(self._transcript.by_seq(message.seq))  # type: ignore[arg-type]

    def discard(self, *messages: Message) -> int:
        """Stop showing exact messages to the model, keeping them in the record.

        Named for what it did before, which was to delete them. It now hides
        them at projection time. The distinction matters on exactly the axis
        this whole file is about: a transcript that has had messages removed
        from it cannot answer "what happened", and every ledger keyed to a
        removed message becomes a claim about nothing.
        """
        hidden = 0
        for message in messages:
            if message.seq < 0 or self._transcript.by_seq(message.seq) is None:
                continue
            self._overrides[message.seq] = None
            hidden += 1
        if hidden:
            self._projector.invalidate()
        return hidden

    # ── the ledger ──────────────────────────────────────────────────────────

    #: Whether a newer read replaces the earlier reads it contains.
    #:
    #: **On**, and this is a deliberate disagreement with the failure report,
    #: which asks for it off ("keep the slice-stub behaviour only if you keep
    #: the 32k budget; at 245k it has no purpose"). Measured, it has a purpose:
    #: with it off, `test_budget_regression` puts P95 at 166,801 tokens against
    #: a 128,000 target and the raw reduction falls from 2.4x to 1.6x on a
    #: read-heavy run.
    #:
    #: What the report is right about is the *bug*, and that bug is separately
    #: fixed. The version that broke two field runs superseded on the path
    #: alone, so a read of lines 40-150 was stubbed by a later read of 3777-3840
    #: over lines that then existed nowhere. Supersession now requires
    #: *containment* and happens at projection time, so the full read is still
    #: in the transcript and the stub says where the live lines are.
    SUPERSEDE_SLICES = True

    def stale_slices(self) -> int:
        """How many reads the projection has collapsed. For telemetry."""
        return self.view().stale_slices

    def coverage(self) -> dict[str, list[tuple[int, int]]]:
        """Which lines of which files the model can read *in this request*.

        The authority on "what has the model actually seen", and now the only
        one. The loop used to keep its own answer and never hear about eviction,
        so a recap saying "re-read one only if you need a line range you have not
        seen" sat beside an intercept refusing exactly those re-reads as "already
        in context above" (BUG L-10). Both were computed from something other
        than the request; this is computed from the request.

        Excluded, because none of them is content the model can read: superseded
        stubs, hidden records, records the compaction sidecar covers, and
        results whose lines the cap removed entirely.
        """
        return {path: list(spans) for path, spans in self.view().coverage.items()}

    def holds(self, path: str, low: int, high: int) -> bool:
        """Whether lines ``low..high`` of ``path`` are readable right now."""
        return self.view().holds(path, low, high)

    @property
    def visible_bodies(self) -> frozenset[str]:
        """Digests of the tool-result bodies the model can currently read."""
        return self.view().bodies

    @property
    def visible_results(self) -> frozenset[str]:
        """Call fingerprints whose result is readable in full, un-elided.

        What an intercept may honestly answer "you already have this" about. A
        result that has since been capped, stubbed or compacted away is not in
        here, which is what stops the run refusing a re-read of something it can
        no longer see.
        """
        return self.view().intact

    def canonical(self, seq: int) -> str | None:
        """What the record at ``seq`` actually said, uncapped.

        The question the old design could not answer. A caller wanting the whole
        build log, the whole file read or the whole search result asks here; the
        model still sees the capped projection.
        """
        record = self._transcript.by_seq(seq)
        return None if record is None else record.content

    # ── budget ──────────────────────────────────────────────────────────────

    def _message_cost(self, message: Message) -> int:
        """What one message costs on the wire. The only answer to that question.

        There used to be two. ``usage()`` counted ``tool_calls`` arguments and
        the retention cut did not, so a write-heavy working set — twenty
        ``write_file`` calls carrying 40KB of arguments each, with empty
        ``content`` — was 200k tokens to the compaction *trigger* and zero to
        the compaction *cut* (BUG L-3). Compaction fired every turn, hid
        nothing, and the run died either as NO_PROGRESS with a message blaming
        the working set or as ERROR "context cannot be reduced below budget".
        Write-heavy runs are this product's core loop.
        """
        cost = self._calibration.estimate(message.content)
        for call in message.tool_calls:
            cost += self._calibration.estimate(f"{call.name}{call.arguments or ''}")
        return cost

    def usage(self) -> Usage:
        by_layer: dict[Layer, int] = {layer: 0 for layer in Layer}
        for msg in self.view().messages:
            by_layer[msg.layer] += self._message_cost(msg)
        total = sum(by_layer.values()) + self._tool_schema_tokens
        return Usage(
            by_layer=by_layer,
            total=total,
            budget=self.budget,
            tools=self._tool_schema_tokens,
        )

    def should_compact(self) -> bool:
        """Whether the assembled prompt has reached the compaction threshold."""
        return self.usage().total >= self.budget * self._compact_at

    def novel_tokens(self, previous: Sequence[Message] | None) -> int:
        """Tokens in this prompt that were not in the previous one's prefix.

        This is what a prefix cache actually has to prefill, and it is the
        metric the design controls. The raw prompt total is what gets prefilled
        with no cache at all; the truth is between them, and *where* between
        them is plan.md §9 Q1 — ``prompt_tokens_details.cached_tokens`` is absent
        from this endpoint, so the hit rate cannot currently be measured.
        """
        current = self.view().messages
        if not previous:
            return sum(self._message_cost(m) for m in current)

        shared = 0
        for old, new in zip(previous, current):
            if old.content != new.content or old.role is not new.role:
                break
            if old.tool_calls != new.tool_calls:
                break
            shared += 1
        return sum(self._message_cost(m) for m in current[shared:])

    def observe_usage(self, *, prompt_tokens: int) -> None:
        """Fold a real ``prompt_tokens`` back into the estimate.

        Called once per turn from the streamed usage chunk. This is the whole
        reason ``stream_options: {"include_usage": true}`` is sent on every
        call: without it there is no measurement, and the estimate stays a
        guess for the life of the process.

        **Both sides of the ratio have to describe the same prompt.** The
        numerator is the projection -- the bytes actually sent -- including the
        ``tool_calls`` arguments strings, which travel on the wire and are
        charged for, plus the tool schemas converted back through the current
        ratio. Counting message content alone dragged the ratio to its floor and
        made every estimate run high, which matters twice over: that estimate is
        what compaction fires on and what ``X-Estimated-Tokens`` reserves against
        a 600k/hour quota.
        """
        messages = self.view().messages
        chars = sum(len(m.content) for m in messages)
        for msg in messages:
            for call in msg.tool_calls:
                chars += len(call.name) + len(call.arguments or "")
        chars += int(self._tool_schema_tokens * self._calibration.ratio)
        self._calibration.observe(estimated_chars=chars, actual_tokens=prompt_tokens)

    # ── compaction ──────────────────────────────────────────────────────────

    def adopt_compaction(self, state: CompactionState | None) -> bool:
        """Install a sidecar, if it still describes this transcript.

        The restore path. A sidecar that does not match is refused rather than
        applied, and the caller compacts again -- projecting a summary of a
        conversation that did not happen is the one failure mode this whole
        design exists to make impossible.
        """
        if state is not None and not state.matches(self._transcript):
            return False
        self._compaction = state
        self._projector.invalidate()
        return True

    def compact(
        self,
        summarise: Summariser,
        *,
        retain_pct: float = 0.35,
        keep_recent: int | None = None,
        strategy: str = "agentic",
    ) -> Recap:
        """Summarise the oldest visible turns and project a recap over them.

        Summarise, do not truncate. This is the lesson from Cline's move away
        from truncation: truncation silently drops the decision that explains
        the current diff, and the agent then re-derives it wrongly.
        Summarisation preserves it.

        **Nothing is deleted.** The evicted records stay in the transcript; what
        changes is the sidecar, and therefore the projection. "Undo this
        compaction" is ``adopt_compaction(previous)``; "what did the model
        actually see" is a projection of a stored sidecar; and no ledger
        anywhere needs invalidating, because no ledger anywhere is a copy of
        what the context holds.

        **How much is kept is measured in tokens, not messages**, and that
        distinction is the whole reason this signature has a percentage in it.
        Part B §10.4 retires the frontend agent's ``contextMaxMessages`` setting
        on exactly this ground — "a message *count* is the wrong unit; forty
        messages can be 5k tokens or 200k". Four capped ``read_file`` results
        are 24k tokens, which is 73% of a coder budget: a count-based compaction
        hands back a context that is already over the 70% threshold, so the next
        turn compacts again. The budget regression test caught it thrashing
        sixteen times in a twenty-five turn run.

        ``keep_recent`` is accepted for the cases where a caller genuinely wants
        a fixed number — the tests do — but it is not the default and it is not
        what the loop should use.
        """
        start = self._visible_start()
        records = self._transcript.records[start:]
        if not records:
            return Recap(turns=(self._turn, self._turn))

        costs = self._visible_costs(records)
        if keep_recent is not None:
            cut = max(0, len(records) - keep_recent)
        else:
            cut = self._retention_cut(records, costs, retain_pct)
        cut = self._whole_turn_cut(records, cut)
        if cut <= 0:
            return Recap(turns=(self._turn, self._turn))

        evicted = records[:cut]
        seqs = {r.seq for r in evicted}
        evicted_messages = [m for m in self.view().messages if m.seq in seqs]

        recap = (
            basic_recap(evicted, turns=(evicted[0].turn, evicted[-1].turn))
            if strategy == "basic"
            else summarise(evicted_messages)
        ).merge(self._compaction.recap if self._compaction else None)

        self._compaction = CompactionState.build(
            recap=recap,
            transcript=self._transcript,
            cut=start + cut,
            tokens=sum(costs[:cut]),
            paths=[r.path for r in evicted if r.path],
            strategy=strategy,
            turn=self._turn,
            generation=(self._compaction.generation + 1) if self._compaction else 1,
        )
        self._projector.invalidate()
        self._compactions += 1
        self._last_eviction = Eviction(
            paths=tuple(dict.fromkeys(r.path for r in evicted if r.path)),
            tool_call_ids=tuple(r.tool_call_id for r in evicted if r.tool_call_id),
            messages=len(evicted),
            tokens=sum(costs[:cut]),
        )
        return recap

    def _visible_start(self) -> int:
        """Index of the first record the model can still read."""
        if self._compaction is None or not self._compaction.matches(self._transcript):
            return 0
        return self._compaction.cut(self._transcript)

    def _visible_costs(self, records: Sequence[Record]) -> list[int]:
        """What each record costs *as projected*, aligned to ``records``.

        The budget is enforced against the request, so the cut has to be taken
        against the request too. A record whose 400KB body the cap reduces to
        12k costs 12k here; costing it at 400KB would evict a whole conversation
        to save nothing.
        """
        by_seq = {m.seq: m for m in self.view().messages if m.seq >= 0}
        out: list[int] = []
        for record in records:
            message = by_seq.get(record.seq)
            out.append(self._message_cost(message) if message is not None else 0)
        return out

    def _whole_turn_cut(self, records: Sequence[Record], cut: int) -> int:
        """Move a cut off a turn boundary it would have split.

        ``_retention_cut`` budgets in tokens and knows nothing about roles, so
        the index it returns lands wherever the allowance runs out — including
        between an assistant message carrying ``tool_calls`` and the ``role:
        "tool"`` messages answering them. The retained set then *begins* with a
        result whose ``tool_call_id`` nothing declares, which is the malformed
        shape the coherence pass is written to prevent.

        Measured before the fix: 14 turns of (assistant + ``read_file`` result)
        swept over 42 size combinations produced an orphan in 13 of them.

        Cut forward rather than back, so the retained set can only get smaller.
        Walking backwards to swallow the assistant would re-admit tokens the
        allowance had already refused, which is how a compaction returns still
        over budget. The last record is never hidden — a compaction that drops
        the result the model is reacting to is worse than not compacting.

        Those two rules met in one place and contradicted each other. When the
        whole retained set is the results of an assistant the cut hid, walking
        forward runs into the never-hide-last rule and stops on an orphan
        (BUG L-6). The coherence pass repairs that now, but a repair is a report
        of a defect, not the absence of one — so when the forward walk cannot
        clear the orphans, the cut steps *back* to include the assistant that
        declared them.
        """
        if cut <= 0 or cut >= len(records):
            return cut
        declared = {call.id for r in records[:cut] for call in r.tool_calls}
        limit = len(records) - 1
        while cut < limit:
            head = records[cut]
            if head.tool_call_id and head.tool_call_id in declared:
                cut += 1
                continue
            break

        head = records[cut] if cut < len(records) else None
        if head is not None and head.tool_call_id and head.tool_call_id in declared:
            back = cut
            while back > 0:
                back -= 1
                if any(call.id == head.tool_call_id for call in records[back].tool_calls):
                    return back
        return cut

    def _retention_cut(
        self, records: Sequence[Record], costs: Sequence[int], retain_pct: float
    ) -> int:
        """Index of the first record to keep, walking back from the newest.

        Budgeted in tokens against the *whole* prompt, not against the visible
        set alone: the pinned head and the recap are what the retained records
        share the budget with, and ignoring them is how a compaction leaves the
        context above the threshold it just fired at.

        Always keeps at least the most recent record. A compaction that
        summarised away the tool result the agent is currently reacting to would
        be worse than not compacting at all.
        """
        overhead = (
            self._tool_schema_tokens
            + self._message_cost(self._system)
            + sum(self._message_cost(m) for m in self._mode_messages)
            + (self._message_cost(self._task) if self._task else 0)
            # The recap is about to be replaced, so budget for a full-sized one
            # rather than for whatever is there now.
            + RECAP_BUDGET_TOKENS
        )
        allowance = max(0, int(self.budget * retain_pct) - overhead)

        used = 0
        cut = len(records)
        for i in range(len(records) - 1, -1, -1):
            cost = costs[i]
            if used + cost > allowance and cut < len(records):
                break
            used += cost
            cut = i
        return cut

    # ── inspection ──────────────────────────────────────────────────────────

    def inspect(self) -> dict[str, Any]:
        """A snapshot for the context inspector (Part B §10.2).

        The extension renders this rather than reconstructing it client-side:
        contract C5 makes the server authoritative on context, and a client that
        recomputes it will eventually disagree.
        """
        use = self.usage()
        view = self.view()
        return {
            "mode": str(self.mode),
            "turn": self._turn,
            "total_tokens": use.total,
            "budget": use.budget,
            "used_pct": use.used_pct,
            "tool_schema_tokens": use.tools,
            "by_layer": {str(k): v for k, v in use.by_layer.items() if v},
            "messages": len(view.messages),
            "compactions": self._compactions,
            "stale_slices": view.stale_slices,
            "calibrated": self._calibration.calibrated,
            "prefix": self.prefix_signature(),
            # The canonical/projected split, so the inspector can show both.
            "canonical_records": len(self._transcript),
            "compacted_records": view.compacted,
            "compaction_stale": view.compaction_stale,
            "elided_records": view.elided_records,
            "elided_lines": view.elided_lines,
            "collapsed_echoes": view.collapsed_echoes,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ContextManager {json.dumps(self.inspect(), sort_keys=True)}>"
