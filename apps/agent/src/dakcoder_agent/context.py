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

The four disciplines
--------------------
**Budget.** A hard prompt cap per mode, allocated across layers in eviction
order. The cap is a *quality* decision as much as a latency one: the
context-rot literature is consistent that accuracy degrades with input length
across every frontier model tested, so a large window is not free even when the
GPU allows it.

**Insertion caps.** Every tool result is capped at the moment it enters history,
not at display time. Elision always leaves a machine-readable marker, so the
model knows it can re-read rather than concluding the content does not exist.

**The file-slice ledger.** An agent that reads a file, patches it, re-reads,
patches, and re-reads again currently keeps three full copies forever. Only the
newest read of each path survives; older ones collapse to a one-line stub. This
bounds the working set by *distinct files touched* rather than by *number of
reads*, and it is the largest single win on edit-heavy tasks.

**Stable-prefix discipline.** One system prompt for every mode, and the message
list is append-only below the pinned head. Mode switches append an instruction
rather than rebuilding the list. The frontend agent assigns a fresh
``run_state.messages`` with a different system prompt in each of ``_run_planner``,
``_run_coder`` and ``_run_debugger`` — three cold prefills per task, by design,
even with prefix caching switched on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Callable, Iterable, Sequence

from dakcoder_shared.llm import ToolCall
from dakcoder_shared.tokens import Calibration

from .modes import Mode, ModeConfig, config_for

__all__ = [
    "Message",
    "Role",
    "Layer",
    "ToolCap",
    "TOOL_CAPS",
    "Eviction",
    "Usage",
    "Recap",
    "ContextManager",
    "OverBudgetError",
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Layer(StrEnum):
    """Eviction priority, in the order Part A §6.1 allocates them.

    Listed lowest-priority first: the working set is what compaction consumes,
    and the pinned layers are the last things standing.
    """

    WORKING_SET = "working_set"
    RECAP = "recap"
    TASK = "task"
    #: The plan and the developer's directives. Pinned like ``TASK``, and
    #: assembled *after* the working set rather than before it — see
    #: ``build`` for the measurement that put it there.
    DIRECTIVE = "directive"
    MODE = "mode"
    SYSTEM = "system"


#: Layers that are never evicted. The task and the acceptance criteria are what
#: the whole run is measured against; an agent that compacts away what it was
#: asked to do will confidently finish something else.
PINNED_LAYERS = frozenset({Layer.SYSTEM, Layer.MODE, Layer.TASK, Layer.DIRECTIVE})

#: How many developer directives the pinned task block may carry at once.
#:
#: Bounded because the layer is pinned and a long conversation would otherwise
#: grow it without limit. Six is well past any run that is still going well; the
#: oldest is dropped first, and every one of them is also in the working set
#: until compaction reaches it.
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
#:
#: There are three modes now and a run visits at most two, so this rarely fires;
#: keeping it at one is what makes the head's meaning unambiguous when it does.
#: The cost is a prefill on a mode switch, which is the correct price for the
#: prompt saying what is true.
MAX_MODE_MESSAGES = 1


@dataclass(frozen=True, slots=True)
class Message:
    """One message.

    Frozen on purpose. §6.4's rule — the message list is append-only below the
    pinned head, and any mutation of ``messages[0..k]`` is a cache-invalidating
    bug — is easy to state and easy to violate by accident three refactors
    later. Immutability makes the accident impossible rather than merely
    detectable.
    """

    role: Role
    content: str
    layer: Layer = Layer.WORKING_SET
    #: Provenance, for the context inspector (Part B §10.2) and for the ledger.
    source: str = ""
    #: Set on tool results, so a stale slice can be found and replaced.
    path: str | None = None
    #: The lines this read actually covers, clamped to the file. ``None`` means
    #: the whole file. Held because superseding on ``path`` alone discards
    #: content the model cannot get back: three disjoint reads of a 6,500-line
    #: file left two stubs saying "re-read if needed" over lines that were
    #: nowhere else in the context, and the repeat ledger then refused the
    #: re-read they asked for. See ``_supersede_slice``.
    line_range: tuple[int, int] | None = None
    tool_call_id: str | None = None
    #: The calls an assistant message made, so its own actions survive into the
    #: next request. Without these every ``role: "tool"`` message that follows
    #: refers to a ``tool_call_id`` no assistant message on the wire declares.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Turn this message was appended on, for the recap and the inspector.
    turn: int = 0

    def wire(self) -> dict[str, Any]:
        """Render to the OpenAI chat shape, dropping our own bookkeeping.

        The assistant's ``tool_calls`` are part of the shape, not bookkeeping.
        Omitting them left every tool result orphaned -- a ``tool_call_id``
        matching no call anywhere in the request, which a strict endpoint
        rejects outright and a lenient one simply cannot make sense of. It also
        rewrote the model's own history: each of its turns came back as a
        paragraph of prose, with results appearing beside them unexplained, so
        the conversation contained no example of the very message shape the
        model was being asked to produce.
        """
        out: dict[str, Any] = {"role": str(self.role), "content": self.content}
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _parseable_arguments(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        return out


def _contains(outer: tuple[int, int] | None, inner: tuple[int, int] | None) -> bool:
    """Whether a read covering ``outer`` makes one covering ``inner`` redundant.

    ``None`` is the whole file, so it contains everything and is contained only
    by another whole-file read. Ranges are the *clamped* ones the tool actually
    returned, not what the model asked for: ``end=99999`` on a 200-line file is
    ``(1, 200)``, and comparing the raw arguments would call it disjoint from a
    later whole-file read that in fact supersedes it.
    """
    if outer is None:
        return True
    if inner is None:
        return False
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _parseable_arguments(arguments: str) -> str:
    """The call's arguments, guaranteed to parse as a JSON object.

    vLLM's chat renderer runs ``json.loads`` over every recorded tool call's
    arguments before templating, so ONE unparseable string in history rejects
    every subsequent request with a 400 -- including a plain "hi", because the
    poisoned message is still there. The field case was a reply cut off by the
    output budget mid-call: arguments arrived as the bare character ``{``, the
    loop (correctly) told the model about the truncation and never dispatched
    the call, but the recorded message re-sent ``{`` on every turn thereafter
    and the endpoint refused them all. json.loads("{") raises the exact error
    the server logged: "Expecting property name enclosed in double quotes:
    line 1 column 2 (char 1)".

    Sanitised at render time rather than at append time, deliberately: the
    ledger keeps what the model actually sent (the loop's own bookkeeping
    fingerprints the raw string), and a session that already recorded a
    truncated call before this fix heals itself on its next request instead of
    staying bricked.

    The substitute is ``{}``, not a repair attempt. The tool result beside the
    call already tells the model the call was cut off and never ran; inventing
    completed-looking arguments would put words in its mouth.
    """
    try:
        if isinstance(json.loads(arguments), dict):
            return arguments
    except (json.JSONDecodeError, TypeError):
        pass
    return "{}"


@dataclass(frozen=True, slots=True)
class ToolCap:
    """The insertion cap for one tool (Part A §6.2)."""

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
#: ``audit`` is deliberately NOT raised: its cap sits above what its renderers
#: emit by design, and the comment on it below still holds — if one ever exceeds
#: the cap, the renderer needs tightening rather than the cap raising.
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
    "go_diagnostics": ToolCap(8_000, "head", "narrow to one file with `path`"),
    # The whole-service surveys, now one tool with a `kind`. `head` rather than
    # the default `tail`: every one of them is rendered worst-first, so the head
    # is the part worth keeping — and the default of tail-truncating a ranked
    # report keeps the least important findings and drops the N+1.
    #
    # One cap where there were five (8,000 / 2,500 / 2,500 / 2,000 / 1,500),
    # sized to the largest of them. The four small ones were set above what
    # their renderers emit, deliberately, so raising them to the legacy survey's
    # ceiling changes nothing about what is elided in practice: a report whose
    # whole purpose is to survive elision must not be the thing that gets
    # elided, and if one ever exceeds this the renderer needs tightening rather
    # than the cap raising.
    "audit": ToolCap(8_000, "head", "pass `paths` to scope it, or ask for one `kind`"),
}

#: Everything not named above. §6.2's "everything else".
DEFAULT_TOOL_CAP = ToolCap(8_000, "tail", "call the tool again with narrower arguments")

#: How many entries of each recap list survive a merge. A recap that grows
#: without limit eventually costs more than the working set it replaced; twelve
#: is roughly two screens of `do_not_retry` and well inside the recap budget.
MAX_RECAP_ITEMS = 12

#: The recap's allocation from §6.1. Reserved when deciding how much of the
#: working set to retain, because the recap grows as history is evicted and
#: budgeting against its current size would leave no room for the one about to
#: replace it.
RECAP_BUDGET_TOKENS = 2_000

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
    """What the last compaction removed.

    Returned as data rather than left implicit because the loop keeps ledgers
    that describe the same content — what it has read, what each call returned —
    and until they hear about an eviction they answer for messages that are no
    longer there (BUG L-10, L-17, L-25).
    """

    paths: tuple[str, ...] = ()
    tool_call_ids: tuple[str, ...] = ()
    messages: int = 0
    tokens: int = 0


@dataclass(frozen=True, slots=True)
class Recap:
    """A structured compaction recap (Part A §6.5).

    Structured, not prose, because of one field: ``do_not_retry`` records dead
    ends, and that is what stops the post-compaction agent cheerfully repeating
    them. ``merge`` folds each recap into the one before it, so a second
    compaction does not throw the first one's dead ends away (BUG L-4).

    ``markdown()`` is what goes into the pinned RECAP layer. It is not written to
    disk; an earlier version of this docstring said it was persisted to
    ``.dakcoder/session-<id>/recap.md`` and nothing ever wrote that file. What
    *is* on disk is the transcript (``journal.py``), which is the thing a restart
    needs.
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
    #:
    #: Recovered from the evicted messages rather than asked of the summariser,
    #: because it is a fact about what was dropped and the loop already knows it.
    files_read: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    verified: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    do_not_retry: tuple[str, ...] = ()
    turns: tuple[int, int] = (0, 0)

    def merge(self, previous: "Recap | None") -> "Recap":
        """Fold an earlier recap into this one.

        A compaction *replaces* the pinned recap, and the evicted set handed to
        the summariser never contains the previous one — so the first
        compaction's `do_not_retry`, the field this class's own docstring calls
        the reason it exists, vanished at the second compaction and the run
        cheerfully repeated the dead end that had caused it (BUG L-4). Long runs
        are exactly the runs that compact twice.

        Older entries come first: they are the earlier history, and a reader —
        model or human — should meet them in the order they happened. Bounded per
        field, oldest dropped first, because a recap that grows without limit
        eventually costs more than the working set it replaced.

        `turns` spans both, so the header stops claiming the run began at the
        last compaction.
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
            # The newest compaction is the closest to what the run is doing now,
            # so its goal and step win where both have one.
            goal=self.goal or previous.goal,
            plan_step=self.plan_step or previous.plan_step,
            files_created=fold(previous.files_created, self.files_created),
            files_modified=fold(previous.files_modified, self.files_modified),
            files_read=fold(previous.files_read, self.files_read),
            decisions=fold(previous.decisions, self.decisions),
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
        out.append(block("Verified", self.verified))
        out.append(block("Open", self.open_items))
        out.append(block("Do not retry", self.do_not_retry))
        return "".join(out)


# A summariser turns the evicted messages into a Recap. Injected rather than
# imported so the context manager stays testable without a model: §6.5 runs
# compaction on the `fast` role, which today resolves to the same 27B model, so
# a real compaction is a real model call and its cost shows up in telemetry.
Summariser = Callable[[Sequence[Message]], Recap]


class ContextManager:
    """Owns the message list for one run.

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
        #: What the loop asserts is true this turn. Rebuilt every turn from
        #: ground truth; see ``set_state``.
        self._state_text = ""
        self._acceptance: tuple[str, ...] = ()
        #: Follow-ups and corrections, pinned. See ``pin_directive``.
        self._directives: list[str] = []
        self._recap: Message | None = None
        #: The structured recap behind ``_recap``'s rendered text, so the next
        #: compaction can fold this one in rather than replace it (BUG L-4).
        self._previous_recap: Recap | None = None
        self._working: list[Message] = []

        # path -> indices into _working of that path's live reads, oldest
        # first. A list rather than one index because disjoint reads of one
        # file each carry content of their own; only a read that *contains* an
        # earlier one supersedes it.
        self._slices: dict[str, list[int]] = {}

        #: What the last ``wire()`` had to repair to keep the tool-call
        #: invariant. Read by the loop, which turns a non-empty value into an
        #: ERROR event: the repair keeps the request valid, but something
        #: upstream produced an invalid one and that is worth knowing about.
        self._wire_repairs: tuple[str, ...] = ()

        #: What the last compaction evicted. Read by the loop to invalidate the
        #: ledgers that describe evicted content.
        self._last_eviction = Eviction()

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def mode(self) -> Mode:
        return self._config.mode

    @property
    def budget(self) -> int:
        return self._config.prompt_budget

    def observe_tool_schemas(self, tokens: int) -> None:
        """Record what the tools array costs on the wire.

        The schemas are part of the prompt and are sent on every call, but the
        constructor takes them as a number the caller supplies once — and the
        runtime never supplied one. ``serve.py`` built every session's context
        with the default of zero, so thirteen Planner schemas, about 1.4k
        tokens, were charged to the endpoint and counted as nothing here.

        Everything downstream is decided against that figure: when to compact,
        how much to retain, and whether ``complete`` refuses the turn. All three
        were being decided against a prompt 1.4k smaller than the one actually
        sent. The budget regression suite passes 1,200 explicitly, which is why
        it never saw this — the simulation was more honest than production.

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
        paid the same. The prior audit carried this as CM-6 and it was accepted
        as by-design; what was missing was the measurement, and the measurement
        is not marginal.

        Moving the *whole* task block down would have worked too and would have
        cost more: the task statement and the acceptance criteria never change
        after ``set_task``, so they are the stable head, and only the plan and
        the directives mutate. Splitting them keeps §6.1's "the run is measured
        against what it was asked to do" where it was and moves the two things
        that actually move.

        The instruction the developer typed most recently now sits closest to
        the model's next token, which is where a correction belongs anyway.
        """
        out: list[Message] = [self._system]
        out.extend(self._mode_messages)
        if self._task is not None:
            out.append(self._task)
        if self._recap is not None:
            out.append(self._recap)
        out.extend(self._working)
        if self._directive_message is not None:
            out.append(self._directive_message)
        return out

    def wire(self) -> list[dict[str, Any]]:
        """Assemble in the shape the API expects, with the tool-call invariant repaired.

        One ``role: "tool"`` message per declared ``tool_call_id``, no tool
        message whose call nothing declares: that is not a convention, it is the
        condition for the request being accepted at all. A strict
        OpenAI-compatible endpoint rejects the whole conversation over a single
        orphan, and because the message list is append-only the rejection is
        permanent — every later turn of the session and every follow-up built on
        the same context carries the same defect.

        The invariant was a *discipline* before this: four call sites in the loop
        each remembered to answer abandoned calls, and two paths forgot (BUG L-1,
        L-6). Discipline scales with the number of people who read the comment.
        A checkpoint on the one path every request must pass through does not.

        This is a backstop, not the fix. When it fires, something upstream is
        wrong and should be repaired there — hence ``wire_repairs``, which the
        loop turns into an ERROR event rather than a silent recovery.
        """
        messages, repairs = self._coherent(self.build())
        self._wire_repairs = repairs
        return [m.wire() for m in messages]

    @property
    def last_eviction(self) -> Eviction:
        """What the most recent ``compact()`` removed."""
        return self._last_eviction

    @property
    def wire_repairs(self) -> tuple[str, ...]:
        """What the last ``wire()`` had to repair. Empty is the only healthy value."""
        return self._wire_repairs

    @staticmethod
    def _coherent(messages: Sequence[Message]) -> tuple[list[Message], tuple[str, ...]]:
        """Return the list with every declared call answered and no orphaned result.

        Two repairs, both information-preserving:

        * A declared call with no result gets a synthesised one saying it did not
          run. It is placed at the end of its assistant's block — the next
          assistant message, or the end of the list — because a batch's results
          are not always contiguous (a retrieval-overlap note is a ``role: user``
          message appended between two results of the same batch).
        * A result whose call no assistant declares becomes a ``role: user``
          message carrying the same text. Dropping it would delete something the
          model was told; leaving it would be malformed.
        """
        declared: set[str] = set()
        answered: set[str] = set()
        for message in messages:
            for call in message.tool_calls:
                declared.add(call.id)
            if message.tool_call_id:
                answered.add(message.tool_call_id)

        if declared <= answered and answered <= declared:
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
                    )
                )
                continue
            out.append(message)
        flush()
        return out, tuple(repairs)

    def prefix_signature(self) -> str:
        """A stable identifier for the cacheable head.

        Exposed so telemetry can alert when it changes. §18 makes a falling
        prefix-cache hit rate an alert rather than a dashboard, and this is the
        signal that says *why* it fell — but note the caveat: mode filtering
        means the tool schemas differ per mode (§7.1) while §6.4 asserts the
        ``system + schemas`` prefix is identical across phases. Both cannot be
        literally true. What is enforced here is the stronger half and the one
        that dominates: the system message is byte-identical across every mode
        and every turn, so a 25-turn run in one mode reuses its prefix
        throughout, and a mode switch costs one prefill rather than a rebuild.
        """
        return f"{len(self._system.content)}:{hash(self._system.content) & 0xFFFFFFFF:08x}"

    # ── appending ───────────────────────────────────────────────────────────

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
        """Assert what is true right now, at the bottom of the prompt.

        The loop rebuilds this every turn from ground truth it already holds --
        ``router.touched``, the plan's per-step statuses, the last gate verdict,
        what has been ruled out -- and nothing here interprets it. See
        ``AgentLoop._state_block``, which is the only caller.

        **Why this layer exists at all.** Before it, the model's entire account
        of its own progress was the transcript, *including its own prose*. A
        planner turn saying "I will write migration.md" is an assistant message
        in the working set, and nothing distinguishes it from a report that the
        file was written -- so a run that had written two files of three said it
        had written three, and a run whose reply was cut off mid-`write_file`
        reconstructed what it had done from memory and got it wrong. The loop
        knew the answer the whole time and was never asked.

        **Why it is affordable.** ``build`` assembles this layer *last*,
        measured: the same words appended at the end of a 100-turn context cost
        11 tokens of prefill against 75,764 for the same edit made in the pinned
        task block. A ~150-token state block rebuilt on every turn therefore
        costs ~150 tokens of prefill per turn, not a re-read of the
        conversation. That measurement was made for steering; this is the second
        thing it pays for.

        Idempotent, so a turn that changes nothing about the state does not
        invalidate even that much: the byte-identical rebuild is skipped.
        """
        state = text.strip()
        if state == self._state_text:
            return
        self._state_text = state
        self._rebuild_task()

    def pin_directive(self, text: str) -> None:
        """Keep something the developer said where compaction cannot reach it.

        A follow-up and a mid-run correction both arrive as ordinary user
        messages in the working set, which is the layer compaction consumes
        first. So the developer's instruction is the *first* thing thrown away,
        and in a run that compacts every few turns it is gone within two —
        which is how a session answered "hi" by carrying on reading the same
        two files for another forty turns, the message having been deleted from
        the run before the next turn assembled.

        That defeats the point of steering. Its whole promise is that a wrong
        turn at turn 12 can be corrected without ending the run, and a
        correction that evaporates at turn 14 is worse than no correction at
        all: the developer believes the run was redirected.

        Pinned as well as appended, not instead. The working-set copy keeps the
        conversational position — the message sits after the answers it follows
        — and this copy keeps the instruction alive. Deterministic on purpose:
        the alternative was asking the summariser to carry it, and a recap that
        silently degrades to a fallback is exactly the fragility this needs not
        to have.
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
        if self._state_text:
            volatile.append(self._state_text)
        elif self._plan_text:
            # Only until the loop has asserted a state block, which happens on
            # the first turn after `submit_plan`. The state block renders the
            # plan itself -- with each step's status against it -- so showing
            # both would put two copies of the plan in the prompt, and the stale
            # one would be the copy that says nothing is done.
            volatile.append(f"# Plan\n{self._plan_text}")
        if self._directives:
            since = "\n".join(f"- {d}" for d in self._directives)
            volatile.append(f"# Since then, the developer has said\n{since}")
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
        the Verifier's, each contradicting the one above it. That is an
        un-evictable layer growing without limit, and a prompt whose loudest
        signal is a pile of stale instructions.

        Two bounds, both cheap. Re-stating the instruction already at the bottom
        of the layer adds nothing, so it is skipped — which also keeps the head
        byte-identical across a re-entry and costs no prefill. Beyond
        ``MAX_MODE_MESSAGES`` the oldest is dropped: it is the one furthest from
        what the model is doing now, and paying one prefill to stop the head
        growing forever is the right trade. A healthy run makes four or five
        switches and never reaches it.
        """
        self._config = config_for(mode)
        text = instruction.strip()

        # One overlay, so there is never a question of which is in force.
        #
        # Re-stating the instruction already there adds nothing, so it is
        # skipped, which keeps the head byte-identical across a re-entry and
        # costs no prefill. Anything else supersedes: see MAX_MODE_MESSAGES
        # for what six of these did to a run.
        if self._mode_messages and self._last_mode_instruction == text:
            return

        self._mode_messages.append(
            Message(Role.USER, text, Layer.MODE, source=f"mode:{self._config.mode}")
        )
        self._last_mode_instruction = instruction.strip()
        # The oldest go, so the head holds only what is in force. At
        # MAX_MODE_MESSAGES = 1 that is the current instruction and nothing
        # else, which is why the "this replaces the above" preamble that used
        # to be prepended here is gone: there is no longer an above.
        del self._mode_messages[:-MAX_MODE_MESSAGES]

    def begin_turn(self) -> int:
        self._turn += 1
        return self._turn

    def append_assistant(
        self, content: str, *, tool_calls: tuple[ToolCall, ...] = ()
    ) -> Message:
        msg = Message(
            Role.ASSISTANT,
            content,
            Layer.WORKING_SET,
            source="assistant",
            tool_calls=tool_calls,
            turn=self._turn,
        )
        self._working.append(msg)
        return msg

    def supersede(self, message: Message, text: str) -> Message | None:
        """Replace a working-set message's content in place, keeping everything else.

        The narrow exception to §6.4's append-only rule, and the same one
        ``_supersede_slice`` already takes: the message keeps its index, its
        role and its ``tool_call_id``, so nothing is orphaned and the wire stays
        well-formed. Only the bytes change.

        What it is for is the accumulating few-shot pattern. Measured against
        the live endpoint: **one** (repeated call -> "answered from the previous
        result") pair in history and Qwen3.8-27B moves on 5/5; **two** and it
        repeats the call 5/5 forever, whatever the answer says. The transcript is
        a stronger instruction than any instruction, so the transcript must not
        be allowed to demonstrate the behaviour we are asking it to stop.

        Returns the replacement, or None when the message is no longer in the
        working set -- compaction may have evicted it, which removed the pattern
        by another route.
        """
        try:
            index = self._working.index(message)
        except ValueError:
            return None
        replacement = replace(self._working[index], content=text)
        self._working[index] = replacement
        return replacement

    def discard(self, *messages: Message) -> int:
        """Remove exact working-set messages, for superseded intercept pairs.

        §6.4's append-only rule exists to protect the cacheable prefix, and
        this is a deliberate, narrow exception to it — the same trade the slice
        ledger already makes when it collapses a superseded read. What it buys
        was measured against the live endpoint: two identical (tool-call →
        "not run") pairs in history flip Qwen3.8-27B from moving on 5/5 to
        repeating the call 5/5, at temperature 0.1, regardless of what the
        intercept text says. The transcript is a stronger instruction than any
        instruction, so the transcript must not accumulate the pattern.

        Removal is by identity, from the working set only; the pinned head is
        untouchable. A message already evicted by compaction is simply not
        found, which is fine — compaction removed the pattern too. The prefix
        invalidated is the tail below the removed pair, which is at most a few
        messages old by construction.
        """
        removed = 0
        for message in messages:
            try:
                self._working.remove(message)
                removed += 1
            except ValueError:
                pass
        if removed:
            # Removing renumbers everything below, so the slice ledger's indices
            # are stale from here on. Compaction already knew that; this path
            # did not, and a stale index stubs out whichever message has since
            # slid into that position — which need not even be a read.
            self._reindex_slices()
        return removed

    def append_user(self, content: str) -> Message:
        """A follow-up or a steering message from the developer mid-run."""
        msg = Message(Role.USER, content, Layer.WORKING_SET, source="user", turn=self._turn)
        self._working.append(msg)
        return msg

    def append_tool_result(
        self,
        tool: str,
        content: str,
        *,
        tool_call_id: str = "",
        path: str | None = None,
        line_range: tuple[int, int] | None = None,
    ) -> Message:
        """Append a tool result, capped at insertion.

        Capped *here*, not at display time. The frontend agent caps the SSE
        event at 4,000 characters while ``ToolResult.to_payload()`` applies no
        cap at all, so the developer sees a tidy preview of something that put
        25k tokens into history permanently (finding S8).
        """
        cap = TOOL_CAPS.get(tool, DEFAULT_TOOL_CAP)
        capped, survived = self._apply_cap(content, cap, path=path, line_range=line_range)

        # The message carries what is *in* it, not what the tool returned. Every
        # consumer of `line_range` — the slice ledger here, the loop's read
        # ledger, the re-read intercept — is answering "has the model seen these
        # lines", and the cap is where those two stopped being the same question
        # (BUG L-8).
        msg = Message(
            Role.TOOL,
            capped,
            Layer.WORKING_SET,
            source=f"tool:{tool}",
            path=path,
            line_range=survived,
            tool_call_id=tool_call_id or None,
            turn=self._turn,
        )

        if path and (survived is not None or line_range is None):
            # A read whose content the cap removed entirely claims no coverage
            # and supersedes nothing. Passing its `None` through would read as
            # "the whole file" to `_contains` — the exact opposite of what
            # happened, and it would stub out every earlier read of the file.
            self._supersede_slice(path, survived)
            self._slices.setdefault(path, []).append(len(self._working))
        self._working.append(msg)
        return msg

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
    #: over lines that then existed nowhere -- and the stub said "re-read if
    #: needed" while the loop's repeat ledger refused exactly that. Two true
    #: messages that could not both be obeyed.
    #:
    #: `_supersede_slice` now requires *containment*: a read is only replaced
    #: when every line of it is inside a newer read further down, and the stub
    #: says where those lines are rather than telling the model to fetch them
    #: again. Nothing is removed -- the message stays, with its `tool_call_id`,
    #: at its index. That is what section 6.4's append-only rule is protecting,
    #: and it is a different thing from the `collapse`/`discard` ledgers that
    #: deleted assistant messages and their results by identity, which are gone.
    #:
    #: Kept as a switch so the trade can be re-made if the stub is ever
    #: implicated again.
    SUPERSEDE_SLICES = True

    def _supersede_slice(self, path: str, line_range: tuple[int, int] | None) -> None:
        """Collapse the earlier reads of ``path`` that this one contains.

        A no-op unless ``SUPERSEDE_SLICES`` is on. The path ledger is still
        maintained, because compaction reads it to say which files it is about
        to evict; what is switched off is rewriting messages already sent.

        When it is on: containment, not identity of path. Superseding on the
        path alone is correct only when every read of a file covers the same
        lines, and the field disproved that -- a Planner read one 6,571-line
        handler at 40-150, 153-205 and 3777-3840, and the first two were
        replaced by stubs over lines that then existed nowhere in the context.
        A read that contains an earlier one makes it genuinely redundant; a
        disjoint or partially-overlapping one does not, and stays.
        """
        if not self.SUPERSEDE_SLICES:
            return

        live = self._slices.get(path)
        if not live:
            return

        kept: list[int] = []
        for index in live:
            if index >= len(self._working):
                continue
            old = self._working[index]
            if old.content.startswith("[stale read of "):
                continue
            if not _contains(line_range, old.line_range):
                kept.append(index)
                continue
            where = (
                f" lines {old.line_range[0]}-{old.line_range[1]}" if old.line_range else ""
            )
            self._working[index] = replace(
                old,
                content=(
                    f"[stale read of {path}{where}: those lines are inside the "
                    f"newer read of this file below]"
                ),
            )
        self._slices[path] = kept

    def stale_slices(self) -> int:
        """How many reads the ledger has collapsed. For telemetry."""
        return sum(1 for m in self._working if m.content.startswith("[stale read of "))

    def coverage(self) -> dict[str, list[tuple[int, int]]]:
        """Which lines of which files are in the working set *right now*.

        The authority on "what has the model actually seen". The loop used to
        keep its own answer to that question and never hear about eviction, so a
        recap saying "re-read one only if you need a line range you have not
        seen" sat beside an intercept refusing exactly those re-reads as "already
        in context above" (BUG L-10). The content was gone and the ledger did not
        know.

        Excluded, because none of them is content the model can read: superseded
        stubs, and results whose lines the insertion cap removed entirely
        (``line_range is None`` after a cap that kept no body line).
        """
        out: dict[str, list[tuple[int, int]]] = {}
        for msg in self._working:
            if msg.role is not Role.TOOL or not msg.path or msg.line_range is None:
                continue
            if msg.content.startswith("[stale read of "):
                continue
            out.setdefault(msg.path, []).append(msg.line_range)
        return out

    # ── caps ────────────────────────────────────────────────────────────────

    def _apply_cap(
        self,
        content: str,
        cap: ToolCap,
        *,
        path: str | None,
        line_range: tuple[int, int] | None,
    ) -> tuple[str, tuple[int, int] | None]:
        """Cap the content, and say which of its lines actually survived.

        The second return value is the fix for BUG L-8. The cap is where the
        context stops agreeing with the tool: ``read_file`` hands over lines
        1-8000 of a large file, the 48k-token cap keeps roughly the first third,
        and the loop then recorded the *tool's* span in its read ledger. The
        model was told two things that were each true on their own — the elision
        marker said "re-read with a narrower line range", the repeat intercept
        said "lines 6000-6500 are already in context above" — and could obey
        neither. On a file over about 150KB the tail became unreachable for the
        rest of the run.

        So the cap reports what it kept, and everything downstream — the message
        the model sees, the slice ledger, the loop's read ledger — is built from
        that instead of from what the tool returned. ``None`` means "not
        expressible as a range": a scattered ``errors`` elision, or a head cut so
        tight that no content line survived at all. A caller must treat that as
        *no* coverage, never as whole-file coverage.
        """
        tokens = self._calibration.estimate(content)
        if tokens <= cap.max_tokens:
            return content, line_range

        lines = content.splitlines()
        if cap.strategy == "errors":
            kept, elided = self._keep_diagnostics(lines, cap.max_tokens)
        elif cap.strategy == "head":
            kept, elided = self._keep_edge(lines, cap.max_tokens, head=True)
        else:
            kept, elided = self._keep_edge(lines, cap.max_tokens, head=False)

        survived = self._surviving_range(
            line_range, total_lines=len(lines), kept=len(kept), strategy=cap.strategy
        )
        marker = self._marker(
            elided, cap, path=path, line_range=line_range, survived=survived
        )
        if cap.strategy == "tail":
            return marker + "\n" + "\n".join(kept), survived
        return "\n".join(kept) + "\n" + marker, survived

    @staticmethod
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
        header. Deriving the offset rather than assuming one line keeps this
        honest if the renderer ever gains a second.
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

    def _keep_edge(self, lines: list[str], budget: int, *, head: bool) -> tuple[list[str], int]:
        ordered = lines if head else list(reversed(lines))
        kept: list[str] = []
        used = 0
        for line in ordered:
            cost = self._calibration.estimate(line) + 1
            if used + cost > budget:
                break
            kept.append(line)
            used += cost
        if not head:
            kept.reverse()
        return kept, len(lines) - len(kept)

    def _keep_diagnostics(self, lines: list[str], budget: int) -> tuple[list[str], int]:
        """Keep every diagnostic line, then fill with context around them.

        Diagnostics first and unconditionally: they are the agent's best fuel,
        and a build log that elided its own error messages is worse than no
        build log, because the agent will conclude the build passed.
        """
        keep_flags = [any(m in line for m in _DIAGNOSTIC_MARKERS) for line in lines]

        kept_idx: list[int] = []
        used = 0
        for i, line in enumerate(lines):
            if not keep_flags[i]:
                continue
            cost = self._calibration.estimate(line) + 1
            kept_idx.append(i)
            used += cost

        # Then as much surrounding context as fits, nearest-first.
        for i, line in enumerate(lines):
            if keep_flags[i]:
                continue
            cost = self._calibration.estimate(line) + 1
            if used + cost > budget:
                continue
            kept_idx.append(i)
            used += cost

        kept_idx.sort()
        return [lines[i] for i in kept_idx], len(lines) - len(kept_idx)

    @staticmethod
    def _marker(
        elided: int,
        cap: ToolCap,
        *,
        path: str | None,
        line_range: tuple[int, int] | None,
        survived: tuple[int, int] | None = None,
    ) -> str:
        """Render the elision marker.

        Always machine-readable and always actionable. An elision the model
        cannot see is one it treats as absence — it concludes the symbol it was
        looking for does not exist, and plans around a repository that has more
        in it than it was shown.

        When the surviving span is known it is named, because "re-read with a
        narrower range" is only actionable if the model can tell which range is
        missing. Without it the advice reads as "read a smaller slice of the
        part you already have".
        """
        where = ""
        if path and line_range:
            where = f" of {path}:{line_range[0]}-{line_range[1]}"
        elif path:
            where = f" of {path}"
        kept = f"; lines {survived[0]}-{survived[1]} are above" if survived else ""
        recover = f" — {cap.recover}" if cap.recover else ""
        return f"[... {elided} line(s) elided{where}{kept}{recover} ...]"

    # ── budget ──────────────────────────────────────────────────────────────

    def _message_cost(self, message: Message) -> int:
        """What one message costs on the wire. The only answer to that question.

        There used to be two. ``usage()`` counted ``tool_calls`` arguments and
        the retention cut did not, so a write-heavy working set — twenty
        ``write_file`` calls carrying 40KB of arguments each, with empty
        ``content`` — was 200k tokens to the compaction *trigger* and zero to
        the compaction *cut* (BUG L-3). Compaction fired every turn, evicted
        nothing, and the run died either as NO_PROGRESS with a message blaming
        the working set or as ERROR "context cannot be reduced below budget".
        Write-heavy runs are this product's core loop.

        A turn whose whole content is a tool call has an empty ``content`` and a
        real cost. Anything deciding how much room a message takes has to ask
        here.
        """
        cost = self._calibration.estimate(message.content)
        for call in message.tool_calls:
            cost += self._calibration.estimate(f"{call.name}{call.arguments or ''}")
        return cost

    def usage(self) -> Usage:
        by_layer: dict[Layer, int] = {layer: 0 for layer in Layer}
        for msg in self.build():
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

        §18 proposes alerting on P95 prompt tokens as a stand-in until that
        field appears. This is a better stand-in: P95 catches a prompt growing,
        but novel tokens catches the thing that actually costs money, which is a
        prefix being *invalidated* — a mutated system message, a mode switch
        inserted in the wrong place, a compaction rewriting the middle of the
        list. Those cost a full prefill while leaving P95 untouched.
        """
        current = self.build()
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

        **Both sides of the ratio have to describe the same prompt.** They did
        not. The numerator counted message *content* only; the denominator is
        the endpoint's ``prompt_tokens``, which includes the tool schemas and
        every ``tool_calls`` arguments string as well. So the observed
        characters-per-token came out systematically low, the calibrated ratio
        was dragged toward its floor, and every estimate built on it ran high —
        which matters twice over, because that estimate is what compaction fires
        on and what ``X-Estimated-Tokens`` reserves against a 600k/hour quota. A
        long run over-reserved its way into 429s it had not earned.

        The schemas are already measured per turn by ``observe_tool_schemas``,
        in tokens rather than characters, so they are converted back through the
        current ratio rather than guessed at. That is circular in principle and
        harmless in practice: it is one term of a smoothed average, and it is far
        closer than omitting them entirely.
        """
        chars = sum(len(m.content) for m in self.build())
        # The arguments the model sent travel on the wire and are charged for.
        for msg in self.build():
            for call in msg.tool_calls:
                chars += len(call.name) + len(call.arguments or "")
        chars += int(self._tool_schema_tokens * self._calibration.ratio)
        self._calibration.observe(estimated_chars=chars, actual_tokens=prompt_tokens)

    # ── compaction ──────────────────────────────────────────────────────────

    def compact(
        self,
        summarise: Summariser,
        *,
        retain_pct: float = 0.35,
        keep_recent: int | None = None,
    ) -> Recap:
        """Summarise the working set and replace it with a recap.

        Summarise, do not truncate. This is the lesson from Cline's move away
        from truncation: truncation silently drops the decision that explains
        the current diff, and the agent then re-derives it wrongly.
        Summarisation preserves it.

        The most recent turns are kept verbatim — the agent is usually mid-edit,
        and a summary of what it did four seconds ago is strictly worse than the
        thing itself.

        **How much is kept is measured in tokens, not messages**, and that
        distinction is the whole reason this signature has a percentage in it.
        Part B §10.4 retires the frontend agent's ``contextMaxMessages`` setting
        on exactly this ground — "a message *count* is the wrong unit; forty
        messages can be 5k tokens or 200k" — and keeping a fixed number of
        recent messages reproduces the mistake one layer down. Four capped
        ``read_file`` results are 24k tokens, which is 73% of a coder budget: a
        count-based compaction hands back a context that is already over the
        70% threshold, so the next turn compacts again. The budget regression
        test caught it thrashing sixteen times in a twenty-five turn run.

        Compacting to a floor instead means compaction is rare, and rarity
        matters twice over: each compaction rewrites the middle of the message
        list, which invalidates every cached prefix below it.

        ``keep_recent`` is accepted for the cases where a caller genuinely wants
        a fixed number — the tests do — but it is not the default and it is not
        what the loop should use.
        """
        if not self._working:
            return Recap(turns=(self._turn, self._turn))

        if keep_recent is not None:
            cut = max(0, len(self._working) - keep_recent)
        else:
            cut = self._retention_cut(retain_pct)

        cut = self._whole_turn_cut(cut)
        evicted, retained = self._working[:cut], self._working[cut:]
        if not evicted:
            return Recap(turns=(self._turn, self._turn))

        recap = summarise(evicted).merge(self._previous_recap)
        self._previous_recap = recap
        self._recap = Message(
            Role.USER, recap.markdown(), Layer.RECAP, source="recap", turn=self._turn
        )
        self._working = retained
        self._reindex_slices()
        self._compactions += 1
        self._last_eviction = Eviction(
            paths=tuple(dict.fromkeys(m.path for m in evicted if m.path)),
            tool_call_ids=tuple(
                m.tool_call_id for m in evicted if m.tool_call_id
            ),
            messages=len(evicted),
            tokens=sum(self._message_cost(m) for m in evicted),
        )
        return recap

    def _whole_turn_cut(self, cut: int) -> int:
        """Move a cut off a turn boundary it would have split.

        ``_retention_cut`` budgets in tokens and knows nothing about roles, so
        the index it returns lands wherever the allowance runs out — including
        between an assistant message carrying ``tool_calls`` and the ``role:
        "tool"`` messages answering them. The retained set then *begins* with a
        result whose ``tool_call_id`` nothing declares, which is the malformed
        shape ``Message.wire`` is written to prevent.

        It does not heal. ``_parseable_arguments`` repairs a bad arguments
        string; nothing repairs a missing message, and ``loopback.follow_up``
        hands this same ContextManager to the next run — so one badly-placed cut
        poisons every later request in the session, down to a plain "hi".

        Measured before the fix: 14 turns of (assistant + ``read_file`` result)
        swept over 42 size combinations produced an orphan in 13 of them.

        Cut forward rather than back, so the retained set can only get smaller.
        Walking backwards to swallow the assistant would re-admit tokens the
        allowance had already refused, which is how a compaction returns still
        over budget. The last message is never evicted — a compaction that drops
        the result the model is reacting to is worse than not compacting.

        Those two rules met in one place and contradicted each other. When the
        whole retained set is the results of an assistant the cut evicted, walking
        forward runs into the never-evict-last rule and stops on an orphan: the
        retained head is a ``role:"tool"`` message whose call nothing declares
        (BUG L-6). ``wire()`` repairs that now, but a repair is a report of a
        defect, not the absence of one — so when the forward walk cannot clear
        the orphans, the cut steps *back* to include the assistant that declared
        them. That re-admits its tokens, which is the lesser cost: the alternative
        is a compaction whose output is a malformed conversation.
        """
        if cut <= 0 or cut >= len(self._working):
            return cut
        declared = {
            call.id
            for msg in self._working[:cut]
            for call in msg.tool_calls
        }
        limit = len(self._working) - 1
        while cut < limit:
            head = self._working[cut]
            if head.tool_call_id and head.tool_call_id in declared:
                cut += 1
                continue
            break

        head = self._working[cut] if cut < len(self._working) else None
        if head is not None and head.tool_call_id and head.tool_call_id in declared:
            # The forward walk hit the last message and it is still an orphan.
            # Step back to the assistant that declared it.
            back = cut
            while back > 0:
                back -= 1
                if any(call.id == head.tool_call_id for call in self._working[back].tool_calls):
                    return back
        return cut

    def _retention_cut(self, retain_pct: float) -> int:
        """Index of the first message to keep, walking back from the newest.

        Budgeted in tokens against the *whole* prompt, not against the working
        set alone: the pinned head and the recap are what the retained messages
        share the budget with, and ignoring them is how a compaction leaves the
        context above the threshold it just fired at.

        Always keeps at least the most recent message. A compaction that
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
        cut = len(self._working)
        for i in range(len(self._working) - 1, -1, -1):
            # `_message_cost`, not `estimate(content)`: the cut has to cost a
            # message the same way the budget that fired the compaction did, or
            # a working set made of tool-call arguments is invisible to exactly
            # the machinery meant to shrink it (BUG L-3).
            cost = self._message_cost(self._working[i])
            if used + cost > allowance and cut < len(self._working):
                break
            used += cost
            cut = i
        return cut

    def _reindex_slices(self) -> None:
        """Rebuild the ledger after the working set is re-sliced."""
        rebuilt: dict[str, list[int]] = {}
        for i, msg in enumerate(self._working):
            if msg.path and not msg.content.startswith("[stale read of "):
                rebuilt.setdefault(msg.path, []).append(i)
        self._slices = rebuilt

    # ── inspection ──────────────────────────────────────────────────────────

    def inspect(self) -> dict[str, Any]:
        """A snapshot for the context inspector (Part B §10.2).

        The extension renders this rather than reconstructing it client-side:
        contract C5 makes the server authoritative on context, and a client that
        recomputes it will eventually disagree.
        """
        use = self.usage()
        return {
            "mode": str(self.mode),
            "turn": self._turn,
            "total_tokens": use.total,
            "budget": use.budget,
            "used_pct": use.used_pct,
            "tool_schema_tokens": use.tools,
            "by_layer": {str(k): v for k, v in use.by_layer.items() if v},
            "messages": len(self.build()),
            "compactions": self._compactions,
            "stale_slices": self.stale_slices(),
            "calibrated": self._calibration.calibrated,
            "prefix": self.prefix_signature(),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ContextManager {json.dumps(self.inspect(), sort_keys=True)}>"
