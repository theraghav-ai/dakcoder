"""What the loop tracks, in five groups instead of thirty-eight fields.

The problem with a flat ``_State``
----------------------------------
Thirty-eight fields on one dataclass, with a docstring claiming twenty. That is
not a naming problem. Fields that belong to different questions were sitting
next to each other with nothing saying so, and the consequences were concrete:

* **Invalidation had to be written out by hand, per field.** ``_forget_evicted``
  knew that ``last_results``, ``partial_results`` and ``truncated_at`` are one
  ledger and cleared all three in a row; nothing in the type said so, so the
  fourth one added would have been missed.
* **The clearing rules were spread across the loop.** "A mutation invalidates
  the call ledgers" was four ``.clear()`` calls inline in ``_tool_calls``.
* **Nothing could be reasoned about as a unit.** "What does a follow-up carry
  over" (``carry_from``) is a question about groups, and it was answered field
  by field.

The groups
----------
Each one is a question the loop asks, with the fields that answer it and the
operations that keep them consistent:

``TaskState``   what the run was asked for, and what it committed to.
``CallLedger``  what has been asked and answered. Invalidated as a unit.
``ReadState``   what has been read and searched, and how much of it.
``GateState``   what the verification gate has said, and what has changed since.
``Progress``    how the turn budget is being spent, and every bound on it.

``_State`` survives as a facade over them. Every one of its field names still
works -- there are two hundred and seventy-nine uses across the loop and the
tests -- but each now resolves to a group that owns it. The point of the facade
is that the split is a refactor and not a rewrite: it can be done without
touching the call sites, and the call sites can then move group by group.

What is deliberately *not* here
-------------------------------
Anything the projection can answer. "Has the model seen these lines", "can it
still read this result", "has this body been shown" are questions about the
request, and keeping a copy of the answer here is what put the loop and its own
context out of step. Those live in ``projection.View``. What remains in the
ledgers below are facts about the *run* -- how often something was asked, what a
tool declared impossible, what a search returned -- which the context cannot
know and which stay true whatever the context is holding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .gate import Baseline
from .migration import MigrationState
from .modes import Intent, Mode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .gate import GateReport
    from .tools.control import PlanStep

__all__ = [
    "CallLedger",
    "GateState",
    "Progress",
    "ReadState",
    "TaskState",
]


@dataclass
class TaskState:
    """What the run was asked for, and what it committed to doing about it."""

    mode: Mode = Mode.ASK
    #: What the developer asked for. Fixed before the first turn.
    intent: Intent = Intent.AUTO
    #: Where ``intent`` came from: "given" (the panel's Ask/Agent toggle, or an
    #: explicit caller), "start" (a legacy mode name), or "classified".
    #:
    #: The decision the whole run turns on, and nothing on the wire distinguished
    #: a developer who *said* this was work from a 64-token call that guessed it.
    #: Twenty turns into an unrequested migration is a late moment to find out
    #: which it was.
    intent_source: str = "given"
    #: The classifier's own one-line reason. The schema has asked for it since
    #: the classifier was written and the reply was thrown away.
    intent_why: str = ""
    #: The plan, as ``submit_plan`` typed it. Empty in ASK.
    plan: tuple["PlanStep", ...] = ()
    plan_summary: str = ""
    #: Whether the current plan came out of a forced ``submit_plan``.
    #:
    #: A plan is normally a commitment, and ``_unwritten_targets`` treats it as
    #: one. A plan extracted at the research fence is not: the developer asked a
    #: question, the fence fired, and the only call the turn accepted was
    #: ``submit_plan``, so the model wrote one. Enforcing it turns a question
    #: into an unrequested migration and gives the run no exit (BUG L-28).
    #:
    #: Asking the model to reconsider before acting on a forced plan was tried
    #: and reverted. It read as an invitation to stop: measured live at the
    #: fence, genuine change tasks went from 4/4 writing the code to 2/4, to buy
    #: one analysis run in four.
    plan_forced: bool = False
    #: The step the plan cursor points at, and the turn it first pointed there:
    #: ``(file, turn)``. Not a bound and not derivable -- it is the one fact the
    #: cursor needs and cannot recompute, because "how long have I been here" is
    #: a property of the *history* of the plan rather than of its current state.
    #:
    #: It exists because the cursor block was otherwise byte-identical on every
    #: turn while a step stayed pending, and a standing order in the recency
    #: slot with nothing in it that moves is a standing order the model obeys
    #: from the top each turn. A field session restated it verbatim fifteen
    #: times and wrote nothing.
    cursor: tuple[str, int] = ("", 0)
    #: Whether this session is converting a legacy service, and where that
    #: conversion has got to.
    #:
    #: On ``TaskState`` because it is the same kind of fact as ``plan``: what
    #: the run committed to. It is carried between messages and persisted with
    #: the plan, because a migration outlives a message by construction -- the
    #: roadmap is agreed on message one and phase five lands on message nine,
    #: and a roadmap rebuilt per message is a roadmap the run restarts from.
    #: See ``migration.py`` for what it changes about the gate, the plan and
    #: the branch.
    migration: MigrationState = field(default_factory=MigrationState)
    #: Whether the pre-migration route inventory has been taken, and how many
    #: routes it held.
    #:
    #: Once per session, and the flag is set before the attempt rather than
    #: after it: a workspace with no sidecar answers the same way every time,
    #: and retrying on each of forty write calls costs a subprocess launch
    #: apiece to learn it again.
    routes_saved: bool = False
    routes_before: int = 0
    #: Files this session deleted that nothing has written again.
    #:
    #: `write_file` refuses to overwrite, so replacing a file means deleting it
    #: and writing it back, and a field run did the first half four times --
    #: `handler/paogen.go` at 6,571 lines among them -- and never the second.
    #: The deletion counted as the step's mutation, so the step read as written
    #: and the cursor moved on. This is the set that disagrees.
    #:
    #: Carried between messages, because "you deleted this and did not replace
    #: it" does not stop being true when the developer sends another message.
    #: Checked against the disk when it is read, so a file the developer
    #: restored drops out of it by itself.
    removed: set[str] = field(default_factory=set)
    #: Every path this session has deleted, whether or not it came back, and
    #: every path that has been written again since it went. Together they are
    #: how a *cycle* is recognised: a delete of something in ``rewritten`` is
    #: the second delete of a file that was already deleted once and restored.
    #:
    #: Separate from ``removed`` because ``removed`` is about loss and empties
    #: itself as files return -- it has to, or the objection outlives the thing
    #: it is about. Churn is about history, and the history is the point.
    gone_once: set[str] = field(default_factory=set)
    rewritten: set[str] = field(default_factory=set)
    #: Path -> how many delete/restore/delete cycles it has been through.
    #:
    #: A run deleted `go.work`, was told the plan did not ask for that and wrote
    #: it back, read its plan step saying to delete it, and deleted it again --
    #: four times in eight turns. Every one of those turns mutated the
    #: workspace, so `stalled_turns` reset on each of them and no bound in the
    #: loop could see it. A mutation on a path that is already cycling is not
    #: progress, and this is the counter that says so.
    churn: dict[str, int] = field(default_factory=dict)
    #: The intent of the run that stopped to ask the developer something, held
    #: until their answer arrives and then spent.
    #:
    #: An answer to `ask_developer` is a continuation of the work that asked,
    #: and it is the one follow-up whose intent is known without guessing. The
    #: classifier guessed anyway and guessed wrong: a field session's Planner
    #: asked four questions about a migration, the developer answered them, and
    #: the reply was classified "Asking for validation, not code changes" --
    #: read-only. Thirteen turns later the run had no write tools, no
    #: `submit_plan`, and a migration plan it could only emit as prose.
    awaiting: Intent = Intent.AUTO


@dataclass
class CallLedger:
    """What has been asked, what it returned, and what cannot succeed.

    One object because these fields are invalidated together and always have
    been -- the loop cleared four of them in a row on a mutation and three more
    on an eviction, in two different places, with nothing but a comment binding
    them. ``forget()`` is that rule, written once.

    What is **not** here any more: whether the model can still *read* a cached
    result. That is a question about the request, ``projection.View`` answers it,
    and the copy that used to live here is what let an intercept tell the model
    "you already have this" about a message a compaction had projected a recap
    over (BUG L-10, L-25).
    """

    #: How many times each exact call has been asked this run. Cleared when a
    #: mutation lands, because repeating a call after an edit is re-checking
    #: work rather than looping.
    seen_calls: dict[str, int] = field(default_factory=dict)
    #: ``router.mutations`` as of the last call, so the ledgers can be cleared
    #: when something actually changed.
    mutations_seen: int = 0
    #: What each fingerprinted call last returned, so a repeat is answered with
    #: the result rather than run again. Cut to ``CACHED_RESULT_CHARS``.
    last_results: dict[str, str] = field(default_factory=dict)
    #: Fingerprints whose cached result is only the head of what the tool
    #: returned, and how long the whole thing was. A replay of one of these has
    #: to say it is partial: presenting a third of a result as "the current
    #: answer" is what makes asking again the reasonable move (BUG L-17).
    partial_results: dict[str, int] = field(default_factory=dict)
    #: Calls the tools themselves declared can never succeed as asked.
    #: fingerprint -> the tool's one-line reason.
    dead_ends: dict[str, str] = field(default_factory=dict)
    #: Fingerprint -> the ``max``/``limit`` that produced a *truncated* answer,
    #: so raising the cap genuinely asks for something the ledger does not hold.
    truncated_at: dict[str, int] = field(default_factory=dict)

    def asked(self, fingerprint: str) -> int:
        """Count this ask, and return how many there have now been."""
        self.seen_calls[fingerprint] = self.seen_calls.get(fingerprint, 0) + 1
        return self.seen_calls[fingerprint]

    def forget(self, fingerprint: str) -> None:
        """Drop everything remembered about one call.

        Used when the context can no longer show the result this ledger would
        point the model at: the cache is not wrong about what the tool returned,
        it is wrong about the model being able to read it.
        """
        self.last_results.pop(fingerprint, None)
        self.partial_results.pop(fingerprint, None)

    def world_changed(self, mutations: int) -> bool:
        """Clear what a write invalidates, and say whether anything did.

        A mutation invalidates all of it at once -- a cached search may now be
        wrong, a missing path may now exist, and a repeat is re-checking work
        rather than looping. ``dead_ends`` goes too, for the same reason: a call
        that could not succeed against the old tree may succeed against the new
        one.
        """
        if mutations == self.mutations_seen:
            return False
        self.mutations_seen = mutations
        self.seen_calls.clear()
        self.last_results.clear()
        self.partial_results.clear()
        self.dead_ends.clear()
        self.truncated_at.clear()
        return True


@dataclass
class ReadState:
    """What has been read and searched, and how much of each.

    The read ledger holds two things the context cannot: how many separate
    dispatches a file has cost (a budget against reading one line at a time) and
    how long the file is. Its *coverage* is re-derived from the projection --
    see ``AgentLoop._live_reads`` -- because that is a fact about the request.
    """

    #: path -> ``_ReadLedger``. Typed loosely here so this module does not
    #: import the loop; the loop owns the ledger type.
    reads: dict[str, Any] = field(default_factory=dict)
    #: What each ``search_docs`` query returned, as section citations.
    retrievals: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    retrieval_repeats: int = 0
    #: What each ``search_repo`` returned, as ``path:line`` keys, for the same
    #: overlap test ``search_docs`` has had all along.
    search_hits: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    search_repeats: int = 0

    def forget_searches(self) -> None:
        """A search's places are worth re-seeing once the messages naming them go.

        Unlike coverage, this cannot be re-derived: it records what a *query*
        returned, not what is in context.
        """
        self.search_hits.clear()
        self.search_repeats = 0


@dataclass
class GateState:
    """What the verification gate has said, and what has happened since."""

    #: What was already broken when the run started. See ``_take_baseline``.
    baseline: Baseline = field(default_factory=Baseline)
    last_gate: "GateReport | None" = None
    #: The workspace state the last full gate ran against.
    gate_key: tuple[int, tuple[str, ...]] | None = None
    #: Failing gates in a row with no new edit between them.
    gate_failures: int = 0
    #: ``router.mutations`` as of the last failing gate, so turns that follow it
    #: without editing anything can be counted. See ``_gate_stalled``.
    gate_mutations: int = 0
    #: Turns since a failing gate in which nothing was written.
    idle_since_gate: int = 0
    #: The turn the last full gate ran on, for the state block.
    gate_turn: int = 0
    dependencies_changed: bool = False


@dataclass
class Progress:
    """How the turn budget is being spent, and every bound on spending it.

    Every counter here ends a run or changes what the next turn is allowed to
    do. That is the test for belonging in this group: a field that only informs
    a message belongs with the thing it describes.
    """

    #: Consecutive tool-calling turns that added nothing new -- every call in
    #: them answered from a ledger rather than dispatched.
    stalled_turns: int = 0
    #: Tool-calling turns in this phase that have not reached a terminal tool.
    #: Reset when a phase ends, because the next one starts its own count.
    research_turns: int = 0
    #: Set when a turn asked for nothing it had not already been given, and the
    #: next turn must therefore answer rather than call. Cleared once used.
    must_answer: bool = False
    #: Why the next turn is being made to answer. Two situations reach the same
    #: mechanism and they are not the same thing: a *stall* is the model asking
    #: for what it has already been given; a *refused terminal* is the model
    #: reaching for the exit with arguments the schema would not take. Telling
    #: it "that call has already been answered" after the second is false on
    #: every clause, and it teaches the wrong correction (BUG L-14).
    answer_because: str = ""
    #: Whether a turn has already been re-asked with ``tool_choice: required``.
    #: Once per **phase**, not once per turn: forcing on every prose turn makes
    #: a Planner that has decided there is nothing to plan relitigate that
    #: decision twice a turn (prior-audit TC-4).
    forced: bool = False
    #: Terminal calls forced that did not land -- the model was made to call
    #: ``submit_plan`` and sent arguments the schema refused, say. Bounded,
    #: because forcing the same call again is the loop this exists to escape.
    forced_terminal: int = 0
    #: Whether the turn just sent was one the loop forced to end the phase.
    #: Read by ``_phase_ended``, which is the only thing that can tell a plan
    #: the model volunteered from one it was left no other move to produce.
    terminal_forced: bool = False
    #: Consecutive replies the output budget cut off. Reset by any reply that
    #: completes. The per-turn handling of a truncated reply is careful, but
    #: nothing counted the *repetition*, so a model that always overran its
    #: output budget spent the entire turn budget doing it and ended EXHAUSTED
    #: without truncation ever being mentioned (BUG L-13).
    truncated_turns: int = 0
    #: Truncated replies in the whole run, never reset. The streak above can be
    #: dodged by one complete reply between two overruns, which is exactly what
    #: a model does while it hunts for a way to send something too large
    #: (BUG FS-3).
    truncations: int = 0
    #: How many times a ``finish`` that abandoned the plan has been sent back.
    #: Bounded: the model may have decided a step is unnecessary, and this reads
    #: paths out of the plan rather than out of the work, so it is not the
    #: arbiter of who is right.
    finish_refused: int = 0
    #: How many times an answer that was only its own opening line was sent
    #: back. Separate from ``finish_refused`` because they are different
    #: mistakes -- one is work not done, the other is work done and not
    #: delivered -- and a run may honestly make both.
    preamble_refused: int = 0
    #: How many times an answer that had stopped being language was sent back.
    #: The third of the same family, and the one with a cost the other two do
    #: not have: a `finish` answer travels as the assistant's tool-call
    #: arguments, so a degenerate one is not merely shown to the developer, it
    #: is kept in the conversation and read back on every turn after it.
    degenerate_refused: int = 0
    #: How many times a migration plan was sent back for not being phased.
    #: Bounded like the three above it and for the same reason: a plan the
    #: model cannot reshape to the loop's satisfaction must eventually be
    #: adopted, because a push-back that never stops asking spends the whole
    #: budget on the shape of the work instead of the work.
    plan_objections: int = 0
    #: Loop-initiated returns to the Planner this run. See ``_replan``.
    replans: int = 0
    #: Model-initiated ``revise_plan`` calls this run.
    revisions: int = 0
    #: The turn each compaction fired on, for the thrash detector.
    compactions: list[int] = field(default_factory=list)
    #: What has been tried and did not work, one line each, oldest first: a gate
    #: failure and the stage it blocked at, a ``revise_plan`` and its reason, a
    #: search the corpus could not answer. Rendered into the state block every
    #: turn, so the model reads its own dead ends rather than reconstructing
    #: them from a transcript that contains its statements of intent.
    tried: list[str] = field(default_factory=list)

    def phase_ended(self) -> None:
        """Reset what is scoped to one phase rather than to the run.

        Both counters are about one mode relitigating one decision. Carrying
        them across a hand-off left the acting mode with a research budget the
        planner had already spent, and without a forced re-ask of its own.
        """
        self.research_turns = 0
        self.forced = False
