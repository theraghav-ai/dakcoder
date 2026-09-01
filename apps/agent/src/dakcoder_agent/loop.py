"""The agent loop (Part A section 10).

One loop, one system prompt, five mode overlays. Modes narrow the tool schema and
sharpen the instruction; they do not fork the process and they do not each get
their own system prompt. That last part is finding S6, and it cost the frontend
agent three cold prefills per task.

The loop is a **generator of events**, not a function that returns an answer.
That shape falls out of contract C2: the extension renders a run as it happens,
and a loop that only speaks at the end would have to buffer everything and then
replay it. It also makes the loop testable without a server — a test drives it by
iterating, which is exactly what the transport does.

Four properties are load-bearing, and each has a specific failure it prevents.

**The gate runs, whatever the model says.** Not because the model asked, and not
because the prompt told it to. "It said it was done and it wasn't" is the failure
this design exists to prevent, and the only way to prevent it is to not ask.

**Failure has a budget.** Two Coder attempts against a failing gate, then the
Debugger. Without a limit a loop retries the same wrong fix until the token
budget runs out, which is the same outcome an hour later.

**No progress stops the run.** The same tool with the same arguments three turns
running means the model is stuck, and the loop can see that when the model
cannot. Inherited from ``postgen``, where it was the single most effective stop
condition.

**Approval is asked, not assumed.** The loop yields a ``tool_pending`` event and
consults a callback. The default denies, because a runtime that silently
auto-approves is one where the approval layer is decoration.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from dakcoder_shared.envelope import DeltaCoalescer, Event, EventType, ToolResult
from dakcoder_shared.llm import LLMClient, Metering, ToolCall
from dakcoder_shared.tokens import estimate_tokens

from .context import ContextManager, Message, OverBudgetError, Recap
from .gate import GateReport, full_gate, inner_loop
from .llm import TurnResult, complete, reasoning_leaked
from .modes import Mode, config_for
from .prompts import mode_instruction, system_prompt
from .tools.router import ApprovalRequest, Router

__all__ = ["AgentLoop", "Approver", "Outcome", "RunResult", "deny_all", "system_prompt"]

#: Asked before a mutating call the developer has not pre-approved. Returns True
#: to let it through. Blocking is the caller's choice — the loop is a generator
#: and will simply wait.
Approver = Callable[[ApprovalRequest], bool]


def deny_all(_request: ApprovalRequest) -> bool:
    """The default. A runtime that silently approves is one with no approval layer."""
    return False


class Outcome:
    DONE = "done"
    #: The developer stopped it.
    ABORTED = "aborted"
    #: The gate never came clean within the attempt budget.
    UNVERIFIED = "unverified"
    #: The same call three turns running.
    NO_PROGRESS = "no_progress"
    #: Turn or token budget reached.
    EXHAUSTED = "exhausted"
    #: The transport or a sidecar failed in a way the model cannot act on.
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: str
    summary: str
    turns: int
    mutations: tuple[str, ...] = ()
    gate: GateReport | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.DONE

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "summary": self.summary,
            "turns": self.turns,
            "mutations": list(self.mutations),
            "gate": self.gate.as_dict() if self.gate else None,
        }


@dataclass
class _State:
    """Everything the loop tracks that is not in the context manager."""

    mode: Mode = Mode.PLANNER
    #: Consecutive Coder attempts against a failing gate.
    attempts: int = 0
    #: Debugger cycles.
    cycles: int = 0
    #: The last few tool-call-free replies, for the repeated-prose detector.
    said: list[str] = field(default_factory=list)
    #: How many times each ``(mode, reply)`` has been seen. ``said`` catches
    #: three identical replies in a row; this catches the same reply coming back
    #: every time the loop re-enters a mode, which is what a two-mode ping-pong
    #: looks like and what ``said`` is structurally unable to see — with Coder
    #: and Verifier alternating, ``said`` holds ``[coder, verifier, coder]`` and
    #: never reaches three of anything.
    echoes: dict[tuple[str, str], int] = field(default_factory=dict)
    #: How many times each exact call has been asked this run — dispatched or
    #: answered from a ledger. Cleared when a mutation lands, because repeating
    #: a call after an edit is re-checking work. Also read as a plain "has this
    #: run started using tools yet" signal by the Planner's no-step branch.
    seen_calls: dict[str, int] = field(default_factory=dict)
    #: ``router.mutations`` as of the last call, so ``seen_calls`` can be cleared
    #: when something actually changed. Repeating a call after an edit is
    #: re-checking work; repeating it after nothing is a loop.
    mutations_seen: int = 0
    #: What each fingerprinted call last returned, so a repeat can be answered
    #: with the result rather than by running it again — or by ending the run,
    #: which is what used to happen on the third ask. Bounded by the number of
    #: distinct calls in a run, which is small. Cleared when a mutation lands.
    last_results: dict[str, str] = field(default_factory=dict)
    #: Calls the tools themselves have declared can never succeed as asked — a
    #: path that does not exist, a pattern that does not parse, a tool that is
    #: not there. fingerprint -> the tool's one-line reason. Answered before
    #: dispatch, an unlimited number of times, at the cost of one tool result;
    #: never counted toward ending the run. Cleared when a mutation lands,
    #: because a write can create the very path that was missing.
    dead_ends: dict[str, str] = field(default_factory=dict)
    #: Consecutive tool-calling turns in which nothing was dispatched — every
    #: call answered from ``dead_ends`` or ``last_results``. The run ends at
    #: ``MAX_STALLED_TURNS``. Reset by any dispatched call or mutation.
    stalled_turns: int = 0
    #: The most recent (single-call reply -> result) pair per (tool, exact
    #: result content) — dispatched calls, not intercepts. A model probing with
    #: slightly different arguments gets a different fingerprint every time, so
    #: every probe dispatches; when the answers come back byte-identical the
    #: pairs are the same few-shot pattern the intercept collapse exists to
    #: remove, built out of successes. Superseding an identical-result pair
    #: loses nothing: the surviving copy carries the same bytes.
    dup_results: dict[str, list[Message]] = field(default_factory=dict)
    #: Fingerprint -> the ``max``/``limit`` that produced a *truncated* answer.
    #: The fingerprint ignores those parameters, because varying one is not a
    #: new question — except in the one case where it is. An answer that stopped
    #: at the cap has more behind it, so raising the cap genuinely asks for
    #: something the ledger does not hold, and that call is dispatched rather
    #: than echoed. A complete answer is complete at any cap.
    truncated_at: dict[str, int] = field(default_factory=dict)
    #: The context messages of the most recent intercept per fingerprint — the
    #: repeated assistant call and the ledger answers it drew — so the next
    #: repeat can supersede them instead of stacking beside them. Measured on
    #: the live endpoint: ONE such pair in history and the model moves on 5/5;
    #: TWO and it repeats the call 5/5 forever, whatever the answer says. The
    #: accumulated pairs are a few-shot pattern, and the pattern outweighs the
    #: prose. See ``ContextManager.discard``.
    intercepts: dict[str, list[Message]] = field(default_factory=dict)
    #: The turn each compaction fired on, for the thrash detector.
    compactions: list[int] = field(default_factory=list)
    #: Consecutive turns in a mode that can write which wrote nothing and called
    #: nothing. Announcing an edit is not making one, and the difference is
    #: invisible to every text-based detector here: the model paraphrases itself
    #: each turn — "Making the edit now", "I have the exact text. Making the edit
    #: now" — so ``said`` and ``echoes`` never match twice. This counts actions
    #: rather than words, which cannot be paraphrased around. Reset by any
    #: mutation. See ``_narrating``.
    idle: int = 0
    #: ``router.mutations`` as of the last idle check. Separate from
    #: ``mutations_seen`` because that one belongs to ``_stuck`` and is already
    #: up to date by the time ``_narrating`` runs.
    idle_mutations: int = 0
    #: ``router.mutations`` as of the last trip through ``_route_failure``, so
    #: an escalation slot is spent on a fix that was attempted rather than on a
    #: Verifier turn that merely happened.
    route_mutations: int = 0
    #: The stage that was blocking last time the ladder ran. When a different
    #: one blocks, the previous problem was solved and the budget starts over.
    blocked_stage: str = ""
    #: Compliance violations already present when the run began, as
    #: ``rule|path|message`` keys. Taken once, before the first edit; see
    #: ``_take_baseline``.
    baseline: frozenset[str] = frozenset()
    #: Tool-free Planner turns that produced no plan, after the Planner had
    #: already started reading. One is a turn whose tool call was never emitted;
    #: two is a Planner with nothing to say. See ``_advance``.
    planner_idle: int = 0
    #: Planner turns that called a tool. The Planner's only stopping condition
    #: is running out of turns, so this is what bounds research. See
    #: ``PLANNER_RESEARCH_NUDGE``.
    planner_research: int = 0
    #: Whether the "you have read enough" nudge has been delivered. Once only:
    #: repeating it every turn would be the accumulating pattern that
    #: ``ContextManager.discard`` exists to keep out of the transcript.
    planner_nudged: bool = False
    #: Consecutive tool-calling turns in a writing mode that wrote nothing, and
    #: whether that mode has been told. Cleared by a mutation. See
    #: ``EXECUTING_RESEARCH_NUDGE``.
    executing_research: int = 0
    executing_nudged: bool = False
    #: How many times a clean gate has been refused because the plan's edit
    #: steps named files nothing wrote. See ``MAX_UNFINISHED_NUDGES``.
    unfinished_nudges: int = 0
    #: Whether a scaffold has run and written files. The Scaffolder is one-shot;
    #: this is what lets it hand on. See ``_SCAFFOLD_TOOLS``.
    scaffolded: bool = False
    #: ``router.mutations`` as of the last time ``executing_research`` was
    #: cleared. Its own watermark for the reason ``idle_mutations`` has one: a
    #: counter shared with ``_stuck`` is already synced by the time this runs,
    #: so every edit would be invisible here.
    research_mutations: int = 0
    #: How many times each path has been read, and the ranges it was read at.
    #: The slice ledger keeps the *context* from growing when a file is read
    #: seven times; it does nothing about the seven turns. Reset for a path when
    #: that path is written, because re-reading what you just changed is the
    #: correct move. See ``_re_reading``.
    reads: dict[str, list[str]] = field(default_factory=dict)
    #: What each ``search_docs`` query this run got back, as the set of section
    #: citations. Keyed by query so a rephrasing can be compared against every
    #: earlier one rather than only the last. See ``_retrieval_overlap``.
    retrievals: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    #: Consecutive retrievals that returned nothing the run had not already been
    #: given. Reset by a retrieval that brings back something new.
    retrieval_repeats: int = 0
    plan: str = ""
    last_gate: GateReport | None = None
    #: The workspace state the last full gate ran against: how many mutations
    #: the router had recorded, and which files. The gate is a function of those
    #: plus the toolchain, so an unchanged pair means an unchanged verdict.
    gate_key: tuple[int, tuple[str, ...]] | None = None
    dependencies_changed: bool = False


_RECAP_PROMPT = """Summarise this agent transcript for a handover to a fresh context.

Reply with JSON only, no prose around it, using exactly these keys:
  goal            one sentence: what the run is trying to achieve
  plan_step       which step of the plan it is on, if the transcript says
  files_created   list of workspace-relative paths created
  files_modified  list of workspace-relative paths modified
  decisions       list of decisions taken AND the reason for each
  verified        list of things confirmed working (gate stages that passed)
  open_items      list of what is still unresolved
  do_not_retry    list of approaches already tried that did NOT work

`do_not_retry` matters most: without it the next turns repeat the dead end that
made this compaction necessary. Keep every file path exactly as written.

TRANSCRIPT:
"""


def _parse_recap(text: str) -> dict[str, Any] | None:
    """Pull the JSON object out of a reply, tolerating fences and stray prose.

    Tolerant because the alternative is discarding a good recap over a markdown
    fence, and a compaction is expensive enough that it should not be spent
    twice.
    """
    if not text.strip():
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


MAX_ATTEMPTS = 2
MAX_DEBUG_CYCLES = 3
NO_PROGRESS_REPEATS = 3

#: How many times one mode may repeat one tool-free reply before the run stops.
#:
#: Two, not three, and deliberately tighter than ``NO_PROGRESS_REPEATS``. This
#: counts occurrences of the *same reply from the same mode* with no tool call
#: in it, which is a narrower thing than three identical turns in a row: a mode
#: that has already been re-entered once and said exactly what it said last time
#: has demonstrated that returning to it does not change the answer. Turns that
#: call a tool never reach this ledger, so a Coder prefixing each edit with one
#: stock sentence is unaffected.
MODE_ECHO_LIMIT = 2

#: How many question marks make a Planner reply a question rather than a plan
#: that happens to contain one. See ``_asks_the_developer``.
MIN_QUESTIONS = 2

#: The compaction-thrash window, in turns, and how many compactions inside it
#: mean the run is evicting rather than working. A healthy run compacts roughly
#: every seven or eight turns — reaching three inside eight means each
#: compaction is being undone by the turn that follows it. See ``_thrashing``.
COMPACTION_WINDOW = 8
MAX_CLOSE_COMPACTIONS = 3

#: How many turns a writing mode may spend calling nothing before the run stops.
#:
#: One is ordinary and means "I am done, run the gate" — that is the normal exit
#: from an executing mode. Two is the ping-pong starting: the gate came back
#: failing, the Verifier reported it, and the Coder returned with prose. Three
#: is a model narrating an edit it is not making, which is what happened for six
#: turns until the Verifier said so itself: *"I'm stuck in a loop — I keep
#: saying I'll make the edit but not actually calling patch_file."*
MAX_IDLE_EXECUTING = 3

#: How many consecutive tool-calling turns may add nothing new — every call in
#: them a verbatim repeat or a known dead end, answered from a ledger rather
#: than dispatched — before the run is ended as making no progress.
#:
#: Six, where the old rule was three occurrences of one call across the whole
#: run. The difference is what is being counted. A repeated call now costs a
#: cached answer rather than a dispatch, so repetition itself is cheap noise;
#: what actually ends a run is a *sequence of whole turns* in which the model
#: asked for nothing it had not already been given. Two field transcripts died
#: under the old rule on its third identical `search_repo` and its third
#: missing-path `read_file` — one of them after being told, correctly, what to
#: do instead. Neither run was looping; both were killed for asking twice too
#: often.
MAX_STALLED_TURNS = 6

#: How many tool-calling turns the Planner gets before it is asked for the plan,
#: and how many before it is made to write one.
#:
#: The Planner is the one mode with no natural stopping condition. Every other
#: detector here fires on a turn that called nothing — ``_narrating`` counts
#: idle turns, ``_repeating`` and ``_restated_the_plan`` compare reply text —
#: and a Planner that calls a tool every turn reaches none of them. A run on
#: 2026-09-01 spent all forty turns reading twenty-line windows of handler
#: files, in perfect health by every measure the loop has, and finished
#: ``exhausted`` with no plan, no edit and nothing to show the developer.
#:
#: Twelve is where the nudge lands and sixteen is where the read tools are
#: withdrawn for one turn, which leaves prose as the only reply available. That
#: is deliberately not a way to end the run: what comes back is a plan, a
#: question or a refusal, and ``_advance`` already knows what each of those
#: means. Orienting genuinely takes a dozen calls on a service this size; it
#: does not take forty.
PLANNER_RESEARCH_NUDGE = 12
PLANNER_RESEARCH_LIMIT = 16

#: The same ceiling one mode down, for a writing mode that has stopped writing.
#:
#: ``_narrating`` already catches a Coder that talks instead of editing, but it
#: counts turns that called *nothing*, so a Coder that calls `read_file` every
#: turn walks past it — which is what run B did on 2026-09-01, twenty-six turns
#: of reading a service's handlers with `stalled_turns` at zero the whole way
#: and not one edit at the end of it. The gap is the same one the Planner had:
#: a mode with no stopping condition except the turn budget.
#:
#: Reset by a mutation rather than by a tool call, because the thing being
#: measured is writing, not activity.
EXECUTING_RESEARCH_NUDGE = 12
EXECUTING_RESEARCH_LIMIT = 16

#: How many times a clean gate on a half-applied plan sends the Coder back.
#:
#: Two, because the second one is the useful one and the third would be a loop.
#: The first can be answered by simply doing the next step; the second catches
#: the run that came back, wrote something else, and still left a target
#: untouched. After that the gap is reported rather than argued about — the
#: model may have decided a step was unnecessary, and this reads paths out of
#: prose, so it is not the arbiter of who is right.
MAX_UNFINISHED_NUDGES = 2

#: The tools that end the Scaffolder's job. One succeeds and the phase is over:
#: the mode's whole instruction is "produce a spec, not code", the templates
#: write the files, and everything after that is ordinary editing.
#:
#: It had no way to say so. ``_advance`` hands a writing mode on only when it
#: ends a turn calling nothing, and a Scaffolder that keeps calling tools never
#: gets there — so a field run scaffolded its seven files, tried to `patch_file`
#: one of them, was correctly told that belongs to the Coder, and spent its
#: remaining fifteen turns guessing an overwrite flag for `resource_scaffold`
#: (`replace`, `clean`, `reset`, `fresh`, `recreate`) because re-scaffolding was
#: the only writing move its mode had left.
_SCAFFOLD_TOOLS = frozenset({"resource_scaffold", "project_scaffold"})

#: The tools withdrawn at ``EXECUTING_RESEARCH_LIMIT`` — the ones whose whole
#: purpose is looking something up.
#:
#: Verification is deliberately not here. A Debugger re-running ``go_build``
#: between hypotheses is working, not stalling, and an identical re-run is
#: already answered from the repeat ledger; taking the gates away from the mode
#: whose job is to run them would break the one loop that is supposed to
#: iterate without editing.
_LOOKUP_TOOLS = frozenset(
    {"repo_map", "read_file", "search_repo", "search_docs", "playbook", "git_blame"}
)

#: The modes that can write. ``_narrating`` counts idle turns only in these:
#: the Planner and the Verifier are supposed to end with prose and no tool call,
#: because that is how they hand on.
_EXECUTING = frozenset({Mode.CODER, Mode.SCAFFOLDER, Mode.DEBUGGER})

#: How many times one path may be read before the loop answers with what it
#: already has instead of dispatching the read again.
#:
#: Worth stating what the number buys, because it was raised from 3 and the
#: trade is real. The run this guard was written for read one file seven times
#: in eight turns, each at a different line range; ``_stuck`` fingerprints the
#: whole call, so it saw seven different calls and nothing to object to, and the
#: Planner stopped for no progress without ever producing a plan. At 10 that run
#: is no longer caught here -- it is left to ``_narrating`` and the turn budget.
#: What 10 still catches is the tighter loop: the same file, over and over,
#: until the context is nothing else.
MAX_READS = 10

#: How many retrievals in a row may return nothing new before the run is told
#: the corpus is exhausted for this question.
#:
#: Three, because the first repeat is an ordinary rephrasing and the second is a
#: developer's instinct to try once more. A model that has asked three different
#: questions and been handed the same sections every time is not going to get a
#: different answer from a fourth.
#:
#: This is the signal that was missing in the field, and it is worth saying why
#: it is *this* signal and not a relevance score. ``search_docs`` runs BM25 with
#: no floor, and a floor cannot be added, because the score does not separate
#: the two cases. Measured against the real 92-section corpus:
#:
#:     in-domain  ("how do I add a new endpoint")          5.330
#:     in-domain  ("where do I put the sql scripts")       3.640
#:     OUT        ("api-server Router struct Engine field") 28.141   <- the highest
#:
#: The query the field transcript died on scores higher than every question the
#: corpus genuinely answers, because it is built from words the corpus uses
#: constantly. Term coverage fails for the same reason: all five of its words are
#: in the vocabulary. No statistic computed from one query can tell "the corpus
#: answers this" from "the corpus contains these words".
#:
#: What *is* reliable is the answer, not the question. Twenty search_docs turns
#: in the field returned the same four sections for six different phrasings --
#: the same 196 lines at turns 21, 22 and 23 -- and nothing said so. That is a
#: set comparison, it needs no threshold, and it cannot refuse a question the
#: corpus can actually answer.
MAX_RETRIEVAL_REPEATS = 3

#: How much of a retrieval must be old for it to count as adding nothing. Half,
#: so two fresh sections out of four still counts as progress.
RETRIEVAL_OVERLAP = 0.5


class AgentLoop:
    """One run, from task to verified change."""

    def __init__(
        self,
        context: ContextManager,
        client: LLMClient,
        router: Router,
        *,
        approve: Approver = deny_all,
        on_pending: Callable[[ApprovalRequest], None] = lambda _request: None,
        on_event: Callable[[Event], None] = lambda _event: None,
        steer: Callable[[], list[str]] = list,
        cancelled: Callable[[], bool] = lambda: False,
        winding_down: Callable[[], bool] = lambda: False,
        max_turns: int = 40,
        session_id: str = "",
    ) -> None:
        self.context = context
        self.client = client
        self.router = router
        self.approve = approve
        #: Called with the request *before* ``tool_pending`` is emitted, so the
        #: runtime has registered it under ``request.id`` by the time a client
        #: can possibly see that id. Default is a no-op: the CLI and the tests
        #: have nothing to register.
        self.on_pending = on_pending
        #: Where transient events go — the ones that cannot travel by ``yield``.
        #:
        #: This loop is a generator, and its events reach a client by being
        #: yielded. That works for everything that happens *between* calls, and
        #: not at all for something that happens *during* one: the model streams
        #: its answer while this thread is blocked inside the completion, and a
        #: callback firing there cannot yield. So streamed text takes the other
        #: road the runtime already had — the same ``emit`` the caller uses to
        #: relay yielded events, called directly.
        #:
        #: Only transient events may go this way. They are defined as relayed
        #: and never stored, so they carry no id and impose no order on the
        #: transcript; a stored event sent out of band would interleave with the
        #: yielded ones and the log would no longer be the run.
        self.on_event = on_event
        #: Cleared to False the first time the sink raises. Streaming is a view
        #: of a turn, never the turn itself, so a sink that has broken must not
        #: be allowed to take the run down with it.
        self._relaying = True
        #: Drained at the top of every turn. This is the whole answer to "the
        #: agent is going the wrong way at turn 12": without it the only
        #: correction is Stop, which ends the run and throws away twelve turns
        #: of context, and a message typed during a run cannot arrive until
        #: after the run it was meant to change.
        self.steer = steer
        #: Checked at two points, not one. Part B §12 keeps both because they
        #: exist for real "stopped but kept moving" reports: a turn can be
        #: several minutes long, and a tool batch can contain five writes after
        #: the developer pressed stop.
        self.cancelled = cancelled
        #: Checked only *between* turns, so a turn already running is allowed to
        #: finish and leave the workspace in a coherent state.
        self.winding_down = winding_down
        self.max_turns = max_turns
        #: Passed to the gateway on every call, so quota and the ledger can
        #: attribute a turn to the run it belongs to. Empty is legitimate — the
        #: CLI and the tests drive the loop with no session behind it — but a
        #: run served over the loopback API always sets it.
        self.session_id = session_id
        self.state = _State()
        self.result: RunResult | None = None

    # -- the run ----------------------------------------------------------

    def run(
        self,
        task: str,
        *,
        acceptance: Sequence[str] = (),
        start: Mode = Mode.PLANNER,
        continued: bool = False,
    ) -> Iterator[Event]:
        """Drive the run, yielding events as they happen.

        ``continued`` is a follow-up on a context that already holds an
        exchange. The new message is *appended to the working set* rather than
        re-pinned as the task, for two reasons. The pinned task layer sits above
        the working set, so re-pinning would put the newest message before the
        answers to the older ones and read as though the model replied before
        being asked. And the original task is what the conversation is about —
        replacing it discards the subject while keeping the answers.
        """
        if continued:
            self.context.append_user(task)
            # And pinned, because the working-set copy is the first thing the
            # next compaction evicts. See `ContextManager.pin_directive`.
            self.context.pin_directive(task)
        else:
            self.context.set_task(task, acceptance=acceptance)
        self._switch(start)
        self._take_baseline()

        for _ in range(self.max_turns):
            if self.result is not None:
                break
            if self.cancelled():
                self.result = self._abort()
                break
            yield from self._turn()
            if self.result is None and self.winding_down():
                # "Stop after this turn" — the developer wants out without
                # killing a turn that is mid-flight. Distinct from cancel, which
                # is checked *inside* the turn and abandons work in progress.
                self.result = RunResult(
                    Outcome.ABORTED,
                    f"stopped at the developer's request after turn {self.context.turn}",
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                break

        if self.result is None:
            self.result = RunResult(
                Outcome.EXHAUSTED,
                f"stopped after {self.context.turn} turns without a clean gate. "
                "Nothing is lost: the edits are in the workspace and the session "
                "is resumable — Resume continues on this same transcript with a "
                "fresh turn budget. For a task this size, raise dakcoder.maxTurns",
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )

        yield Event(EventType.FINISH, self.result.as_dict())
        yield Event(EventType.END, {})

    # -- one turn ---------------------------------------------------------

    def _turn(self) -> Iterator[Event]:
        for correction in self.steer():
            # Appended as a user message so it lands in the working set the same
            # way the original task did, and the model treats it as instruction
            # rather than as tool output it can weigh against its own plan.
            self.context.append_user(correction)
            self.context.pin_directive(correction)
            yield Event(EventType.STEER, {"text": correction, "turn": self.context.turn})

        # The scaffold has run, so the Scaffolder is finished whether or not it
        # has noticed. Everything from here is editing what the templates wrote,
        # and the tools for that live one mode down. Before `begin_turn`, so the
        # turn is announced as the mode it will actually dispatch.
        if self.state.mode is Mode.SCAFFOLDER and self.state.scaffolded:
            self._switch(Mode.CODER)
            self.context.append_tool_result(
                "resource_scaffold",
                "The scaffold has run and its files are in the workspace, so that "
                "phase is over — you are the Coder now, and `patch_file` is "
                "available.\n\n"
                "Do not scaffold again: it is not idempotent and there is no "
                "overwrite flag. Edit what it wrote, and finish the steps it did "
                "not cover.",
            )

        turn = self.context.begin_turn()
        yield Event(
            EventType.TURN_START,
            {"turn": turn, "mode": str(self.state.mode), "attempt": self.state.attempts},
        )

        # Measured before the compaction decision, not after: the schemas are
        # part of the prompt this turn will send, so a threshold consulted
        # without them is consulted against the wrong number. Computed once and
        # reused below, which also stops the array being built twice per turn.
        tools = self.router.schemas_for(self.state.mode)

        # The Planner has read enough. Asked for the plan at the nudge; left
        # with no tools to call at the limit, so that prose is the only reply
        # available and `_advance` gets to run at all. Withdrawing the schemas
        # rather than ending the run keeps every outcome on the table: what
        # comes back is a plan, a clarifying question or "nothing to do here",
        # and each of those is already handled below.
        if self.state.mode is Mode.PLANNER and not self.state.plan:
            if self.state.planner_research >= PLANNER_RESEARCH_LIMIT:
                tools = []
            elif (
                self.state.planner_research >= PLANNER_RESEARCH_NUDGE
                and not self.state.planner_nudged
            ):
                self.state.planner_nudged = True
                self.context.append_tool_result(
                    "repo_map",
                    f"You have spent {self.state.planner_research} turns reading and "
                    "have not written a plan yet. That is enough to plan from — the "
                    "Coder phase reads the files it edits, so anything you have not "
                    "opened yet, it will.\n\n"
                    "Write the plan now: at most eight numbered steps, each naming a "
                    "real file and carrying an Accepts: line saying how you will know "
                    "it worked. If the task is too large for eight steps, plan the "
                    "first slice of it and say what you left out.",
                )

        # The same thing for a mode that can write and has not. A mutation
        # clears it, so the ordinary read-edit-read cycle never reaches either
        # A corpus that has been asked and has nothing more is not asked again.
        #
        # Telling the model was not enough on its own: the message is advice, and
        # a model that has decided the answer must be in the knowledge base will
        # keep rewording the question — which is precisely the sixteen turns this
        # is here to stop. So once the retrieval ledger has said three times that
        # a search returned only sections the run already had, ``search_docs``
        # stops being offered. Withdrawn for the rest of the run rather than the
        # turn: the corpus does not acquire new sections mid-run, so nothing that
        # follows could change the answer.
        #
        # Only ``search_docs``. ``read_file`` and ``search_repo`` read the
        # workspace, which the run does change, and taking those away would stop
        # the model reading the file it is about to edit.
        if self.state.retrieval_repeats >= MAX_RETRIEVAL_REPEATS:
            tools = [s for s in tools if s["function"]["name"] != "search_docs"]

        # threshold; what does reach them is a Coder reading a whole service.
        if self.state.mode in _EXECUTING:
            if self.router.mutations != self.state.research_mutations:
                self.state.research_mutations = self.router.mutations
                self.state.executing_research = 0
                self.state.executing_nudged = False
            elif self.state.executing_research >= EXECUTING_RESEARCH_LIMIT:
                # Lookups withdrawn, writing and verification left in place, so
                # the only moves available are the ones that finish the step.
                tools = [
                    s for s in tools if s["function"]["name"] not in _LOOKUP_TOOLS
                ]
            elif (
                self.state.executing_research >= EXECUTING_RESEARCH_NUDGE
                and not self.state.executing_nudged
            ):
                self.state.executing_nudged = True
                self.context.append_tool_result(
                    "read_file",
                    f"You have called {self.state.executing_research} turns' worth of "
                    "tools in this phase and changed no file. Reading more of the "
                    "service will not make the first edit easier to write.\n\n"
                    "Make the smallest change that advances the current step now — "
                    "`patch_file` with a unique anchor from something you have "
                    "already read. If the step cannot be done as specified, say "
                    "which one and why, in one line, instead of reading further.",
                )

        self.context.observe_tool_schemas(estimate_tokens(json.dumps(tools)))

        if self.context.should_compact():
            yield from self._compact()
            if reason := self._thrashing():
                self.result = RunResult(
                    Outcome.NO_PROGRESS,
                    reason,
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                return

        # One coalescer per turn, and the tail flushed however the turn ends.
        # Forgetting that flush is the classic bug in code shaped like this:
        # everything works except that the last sentence never arrives.
        deltas = DeltaCoalescer()
        try:
            result = complete(
                self.context,
                self.client,
                tools=tools,
                session_id=self.session_id,
                on_delta=lambda fragment: self._relay(deltas.feed(fragment)),
            )
        except OverBudgetError as exc:
            # The context manager exists to prevent this, so reaching it means
            # compaction could not free enough. Compacting harder and retrying
            # once is worth a turn; failing the run outright is not.
            yield Event(EventType.GATE, {"kind": "compaction", "reason": "over budget"})
            self.context.compact(self._summarise, retain_pct=0.15)
            try:
                result = complete(
                    self.context,
                    self.client,
                    tools=tools,
                    session_id=self.session_id,
                    on_delta=lambda fragment: self._relay(deltas.feed(fragment)),
                )
            except OverBudgetError:
                self.result = RunResult(
                    Outcome.ERROR, f"context cannot be reduced below budget: {exc}",
                    self.context.turn, tuple(self.router.touched),
                )
                return
        except Exception as exc:  # noqa: BLE001 - the transport failing is not the model's fault
            yield Event(EventType.ERROR, {"message": str(exc)})
            self.result = RunResult(
                Outcome.ERROR, str(exc), self.context.turn, tuple(self.router.touched)
            )
            return
        finally:
            self._relay(deltas.flush())

        yield from self._usage(result)

        if self._restated_the_plan(result):
            # The handoff was wrong, and this turn is the proof of it.
            #
            # A Coder handed a plan restates it word for word only when there
            # was nothing in it to execute — which means the Planner was
            # answering rather than planning, and the numbered list it happened
            # to use was read as steps. `_count_steps` cannot tell a plan from a
            # numbered answer, and no regex over prose reliably can; what the
            # loop *can* see is the mode below finding nothing to do with it.
            #
            # Caught before the reply is emitted, so the developer is not shown
            # the same paragraph twice and then told it was a mistake. The turn
            # is still accounted for above: it cost tokens whatever it said.
            self.result = RunResult(
                Outcome.DONE,
                "answered; the reply was not a plan and nothing needed changing",
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        # The assistant's own turn goes into context before anything it caused.
        #
        # Its calls travel with it. Recording only the prose left every tool
        # result that followed referring to a ``tool_call_id`` that no message on
        # the wire declared -- malformed against a strict endpoint, and worse
        # than malformed as a prompt: the model's entire visible history of
        # itself became thirty paragraphs of narration with results appearing
        # beside them unexplained. It was being asked to produce a message shape
        # it could not see itself ever having produced.
        #
        # Appended even when the content is empty, because a turn that is purely
        # a tool call is exactly the turn whose record matters most.
        if result.chat.content:
            yield Event(EventType.ASSISTANT, {"text": result.chat.content})
        assistant_msg: Message | None = None
        if result.chat.content or result.chat.tool_calls:
            assistant_msg = self.context.append_assistant(
                result.chat.content or "",
                tool_calls=tuple(result.chat.tool_calls),
            )

        # A reply cut off by the output budget, mid-tool-call.
        #
        # The arguments are a valid-looking JSON prefix — `{` is what it looks
        # like in practice — so dispatching them produces "malformed arguments",
        # and the model is told to send valid JSON. It did send valid JSON. It
        # was interrupted. Acting on that advice means making the same oversized
        # reply, being cut off at the same place, and dying against the
        # no-progress detector three turns later, which is exactly what happened
        # in the field.
        #
        # Named for what it is, and fed back before dispatch so the turn is not
        # spent on a call that cannot succeed.
        if incomplete := result.chat.incomplete_tool_calls():
            names = ", ".join(sorted({c.name for c in incomplete}))
            # Every call the assistant message declared gets an answer, not just
            # the first cut-off one.
            #
            # ``incomplete_tool_calls`` returns a *list*, and this branch used to
            # answer ``incomplete[0]`` and return. The assistant message carrying
            # all of the calls was already appended above, so every other call in
            # that reply was left with no ``role:"tool"`` message pointing at it.
            # ``Message.wire`` says what that costs: a result whose
            # ``tool_call_id`` no assistant message declares is malformed against
            # a strict endpoint, and the poisoned message stays in the working
            # set for the rest of the run *and the rest of the session*, because
            # ``loopback.follow_up`` reuses this ContextManager. Unlike a bad
            # arguments string, which ``_parseable_arguments`` repairs, a missing
            # message never heals.
            #
            # Measured on a mixed reply (one complete `read_file`, one cut-off
            # `write_file`): declared ['cut1', 'good1'], answered ['cut1'],
            # orphaned ['good1'].
            cut = {c.id for c in incomplete}
            for call in result.chat.tool_calls:
                if call.id in cut:
                    body = (
                        f"Your call to {call.name} arrived cut off — the arguments stop "
                        "partway through, so the call was not made. Nothing is wrong with "
                        "your JSON; this is what running into the "
                        f"{config_for(self.state.mode).max_tokens:,}-token output limit "
                        "looks like.\n\n"
                        "Make the next reply shorter: fewer tool calls in one turn, and "
                        "less prose before them. One call is enough."
                    )
                    said = f"output limit reached mid-call; {names} was not dispatched"
                else:
                    # Complete, but never dispatched: the turn is abandoned at the
                    # truncation. Saying so is what keeps the wire coherent.
                    body = (
                        f"{call.name} was not run. Another call in the same reply was cut "
                        "off by the output limit, so the whole turn was abandoned before "
                        "anything was dispatched.\n\nAsk for it again in a shorter reply."
                    )
                    said = f"{call.name} was not dispatched; the reply was cut off"
                self.context.append_tool_result(call.name, body, tool_call_id=call.id)
                yield Event(
                    EventType.TOOL_RESULT,
                    {"id": call.id, "name": call.name, "ok": False, "content": said},
                )
            return

        if result.chat.tool_calls:
            if self.state.mode is Mode.PLANNER:
                self.state.planner_research += 1
            elif self.state.mode in _EXECUTING:
                self.state.executing_research += 1
            yield from self._tool_calls(result.chat.tool_calls, assistant_msg)
            return

        if why := self._narrating():
            self.result = RunResult(
                Outcome.NO_PROGRESS,
                why,
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )
            return

        if result.chat.content and (why := self._repeating(result.chat.content)):
            self.result = RunResult(
                Outcome.NO_PROGRESS,
                why,
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )
            return

        # No tool calls: the model has said its piece, so the mode is over.
        yield from self._advance(result)

    def _usage(self, result: TurnResult) -> Iterator[Event]:
        usage = self.context.usage()
        payload = {
            "prompt_tokens": result.actual_prompt_tokens,
            "completion_tokens": result.chat.usage.completion_tokens,
            "cached_tokens": result.chat.usage.cached_tokens,
            # The absolute denominator, not just the percentage. A client given
            # only `budget_used_pct` has to divide to recover it, and two
            # surfaces dividing independently produce two different numbers on
            # screen at low usage — which is exactly what happened when the
            # console row and the header meter each rolled their own.
            "budget": usage.budget,
            "budget_used_pct": round(usage.used_pct, 1),
            # Reported every turn, not only when it leaks. Thinking is a real
            # cost and a developer learns that "plan this carefully" is not free
            # by watching the number; reporting it only on the anomaly path
            # meant a healthy run showed nothing at all.
            "reasoning_tokens": result.chat.usage.reasoning_tokens,
            "estimate_error": result.estimate_error,
        }
        if reasoning_leaked(result):
            # Section 18's alert. Non-zero reasoning in a thinking-off mode means
            # chat_template_kwargs is not reaching the model: ~15x the latency
            # for no quality gain, and it presents as the agent simply being slow
            # rather than as a failure.
            payload["reasoning_leaked"] = result.chat.usage.reasoning_tokens
        yield Event(EventType.USAGE, payload)

    # -- tools ------------------------------------------------------------

    def _tool_calls(
        self, calls: Sequence[ToolCall], assistant_msg: Message | None = None
    ) -> Iterator[Event]:
        # The world changed since the ledgers were written: forget them.
        #
        # A mutation invalidates all three at once. The cached result of a
        # search may now be wrong, a missing path may now exist, and a repeat
        # is re-checking work rather than looping. Watermarked against
        # ``router.mutations`` rather than cleared inline so a mutation made
        # outside this batch — the gate's ``go mod tidy`` writing go.mod — is
        # noticed too.
        if self.router.mutations != self.state.mutations_seen:
            self.state.mutations_seen = self.router.mutations
            self.state.seen_calls.clear()
            self.state.last_results.clear()
            self.state.dead_ends.clear()
            self.state.intercepts.clear()
            self.state.dup_results.clear()
            self.state.truncated_at.clear()

        mutated = False
        dispatched = False
        #: Dispatched calls that actually told the run something it did not
        #: already have. ``dispatched`` answers "did anything run";
        #: ``informed`` answers "did anything change what we know", which is
        #: the question the stall detector is actually asking.
        informed = 0

        # Whether this whole reply is one fingerprint asked again — the only
        # shape it is safe to supersede wholesale. A reply that also said
        # something, or also called something else, keeps its place in history;
        # removing it would lose the prose or orphan the other calls' results.
        def sole_fingerprint(fp: str) -> bool:
            return (
                assistant_msg is not None
                and not assistant_msg.content
                and all(_fingerprint(c) == fp for c in assistant_msg.tool_calls)
            )

        def collapse(fp: str, echo: Message) -> None:
            """Supersede the previous intercept pair for ``fp`` with this one.

            Keeps at most one (repeated call -> ledger answer) pair per
            fingerprint in the working set. The pair being replaced is removed
            whole — assistant message and its answers together — so no tool
            result is ever left pointing at a call the wire no longer carries.
            """
            prior = self.state.intercepts.get(fp)
            if prior and (not prior or prior[0] is not assistant_msg):
                self.context.discard(*prior)
                self.state.intercepts.pop(fp, None)
            if sole_fingerprint(fp):
                pair = self.state.intercepts.setdefault(fp, [])
                if not pair:
                    pair.append(assistant_msg)
                pair.append(echo)

        for index, call in enumerate(calls):
            if self.cancelled():
                # Before the call, not after. A batch can hold five writes, and
                # "it stopped but three more files changed" is the report this
                # check exists to prevent.
                #
                # The calls we are abandoning still get answered. The assistant
                # message declaring all of them is already in the working set, so
                # returning here left every undispatched call orphaned — and an
                # aborted session is ``Status.resumable``, so the orphan was
                # carried into the resume and every later request with it.
                # Measured with a three-call batch cancelled after the first:
                # orphaned ['r2', 'r3'].
                for pending in calls[index:]:
                    self.context.append_tool_result(
                        pending.name,
                        f"{pending.name} was not run: the developer stopped the run "
                        "before this call was dispatched.",
                        tool_call_id=pending.id,
                    )
                self.result = self._abort()
                return

            fingerprint = _fingerprint(call)

            # An intercepted call never reaches TOOL_CALL — that event means
            # "dispatched", and the tests below depend on it meaning only that.
            # But the client builds its row from TOOL_CALL, so an intercept
            # arrived as a result for a row that had never been opened and was
            # drawn as a bare tool name with no arguments beside it. A
            # transcript of a looping run was a column of those, which is
            # exactly the case where seeing the arguments is how anyone works
            # out what the loop is about. So the intercepts carry their own
            # arguments on the result instead.
            args = _safe_args(call)

            # A known dead end is answered, every time, and is never fatal.
            #
            # The tool itself declared this exact call unable to succeed — the
            # path does not exist, the pattern does not parse — when it first
            # ran. Asking again cannot change the answer, so the answer is
            # repeated for the price of one tool result. What it must never do
            # is end the run: a field transcript died on its third read of a
            # file that was not there, one turn after being told, correctly,
            # what to do instead. Being slow to take a hint costs a turn; it is
            # not a reason to throw away twenty-five.
            if reason := self.state.dead_ends.get(fingerprint):
                self.state.seen_calls[fingerprint] = (
                    self.state.seen_calls.get(fingerprint, 0) + 1
                )
                echo = self.context.append_tool_result(
                    call.name,
                    f"{call.name} with these arguments cannot succeed: {reason}. "
                    "That was established earlier this run and nothing has changed "
                    "since, so it was answered from the earlier result rather than "
                    "run again.\n\n"
                    "This is the answer, not a failure. Act on what does exist — the "
                    "alternatives named in the earlier result still stand.",
                    tool_call_id=call.id,
                )
                collapse(fingerprint, echo)
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": True,
                        "intercepted": True,
                        "arguments": args,
                        "content": f"{call.name}: known dead end; answered without re-running",
                    },
                )
                continue

            # An exact repeat while nothing has changed is answered from what it
            # returned last time.
            #
            # Not merely the second consecutive ask — any repeat since the last
            # mutation. The consecutive version was defeated by alternation in
            # the field: read A, read B, read A slipped past a tail-of-two check
            # and the third A ended the run. The result is right here in the
            # ledger; handing it over again costs nothing and keeps the turn
            # recoverable. The tools this must not throttle — re-running the
            # build after an edit, re-reading a file just written — are exactly
            # the ones a mutation precedes, and the mutation cleared this ledger.
            # An answer that stopped at its cap has more behind it, so asking
            # again with a bigger one is a question the ledger cannot answer.
            # Absent from `truncated_at` means the earlier answer was complete,
            # and a complete answer is complete at any cap.
            capped = self.state.truncated_at.get(fingerprint)
            wants_more = capped is not None and _volume(call) > capped

            if (cached := self.state.last_results.get(fingerprint)) is not None and not wants_more:
                asks = self.state.seen_calls.get(fingerprint, 0) + 1
                self.state.seen_calls[fingerprint] = asks
                # The answer first, the bookkeeping after.
                #
                # This message used to open "Not run:" and arrive with
                # ``ok: false``, which is how a call that succeeded came to look
                # like a call that failed -- and a model that reads a failure
                # retries it. The content was always here; it was three lines
                # below a refusal. Leading with the result costs nothing and
                # removes the reason to ask again.
                body = (
                    f"{call.name} returned:\n\n{cached}\n\n"
                    "— that is the current answer. The call ran earlier this turn "
                    "or an earlier one, nothing in the workspace has changed since, "
                    "so it was answered from that result rather than dispatched "
                    "again. Use it and move to the next step; if it does not tell "
                    "you what you need, ask something different or say plainly what "
                    "is blocking you."
                )
                if asks >= 3:
                    # Said plainly at the third ask, because by the sixth stalled
                    # turn the run ends and the model deserves to have been told.
                    body += (
                        f"\n\nThis is ask number {asks} for this exact call, and it "
                        "will keep returning the answer above while the workspace "
                        "is unchanged. Turns that only repeat earlier calls end the "
                        "run."
                    )
                echo = self.context.append_tool_result(
                    call.name,
                    body,
                    tool_call_id=call.id,
                )
                collapse(fingerprint, echo)
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": True,
                        "intercepted": True,
                        "arguments": args,
                        "content": (
                            f"{call.name} asked again with the same arguments; "
                            "answered from the previous result"
                        ),
                    },
                )
                continue

            # The fourth read of one file, at a fourth set of line numbers.
            #
            # `_stuck` fingerprints the whole call, so re-reading `message.go` at
            # lines 1-120, then 60-180, then 200-480 is three different calls and
            # invisible to it. The slice ledger keeps that from growing the
            # *context* — only the newest read survives — and does nothing about
            # the turns, which is how a Planner spent seven of its first eight
            # reading one file and then stopped for no progress without ever
            # producing a plan.
            #
            # Answered rather than refused: the model is told what it has already
            # read and that the newest read is still in front of it. A path that
            # has been written since is not counted, because re-reading what you
            # just changed is the correct move.
            if ranges := self._re_reading(call):
                self.context.append_tool_result(
                    call.name,
                    f"You have read this file {len(ranges)} times this run, at "
                    f"{', '.join(ranges)}, and every one of those reads is still in "
                    "context above except where a later read covered the same lines.\n\n"
                    "Reading it again is not going to show you anything those did "
                    "not. Act on what you have, or say plainly what you are looking "
                    "for and cannot find.",
                    tool_call_id=call.id,
                )
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": True,
                        "intercepted": True,
                        "arguments": args,
                        "content": f"already read {len(ranges)} times this run; not re-read",
                    },
                )
                continue

            yield Event(
                EventType.TOOL_CALL,
                {"id": call.id, "name": call.name, "arguments": _safe_args(call)},
            )

            outcome = self.router.dispatch(call.name, call.arguments, mode=self.state.mode)

            if isinstance(outcome, ApprovalRequest):
                request = outcome
                # Registered before it is announced. The event and the runtime
                # now agree on one id, and a client that answers the instant it
                # reads the event cannot arrive before the approval exists.
                self.on_pending(request)
                yield Event(EventType.TOOL_PENDING, request.as_dict())
                if self.approve(request):
                    # Re-dispatched with the *request's* arguments, not the
                    # model's original string. An approver may have corrected
                    # them — Part B §9's `edit` decision, where fixing a path
                    # beats rejecting and re-prompting because the model
                    # usually makes the same mistake again. Using call.arguments
                    # here would apply the approval and discard the correction,
                    # which is the worst of the three possible outcomes.
                    outcome = self.router.dispatch(
                        call.name, request.arguments, mode=self.state.mode, approved=True
                    )
                else:
                    outcome = ToolResult.failure(
                        f"{call.name} was not approved by the developer.",
                        fix="Explain why the change is needed, or take a different "
                        "approach that does not touch that file.",
                    )

            assert isinstance(outcome, ToolResult)
            # A tool refused because this mode does not hold it is the one
            # outcome that says nothing about the call — only about who is
            # asking. It is not progress, and it is not cacheable.
            #
            # Not progress: without this, uncaching the refusal below would let a
            # model repeat a refused call forever, each repeat reaching the
            # router and resetting ``stalled_turns``, and no detector would ever
            # end the run.
            refused_by_mode = bool(outcome.meta.get("refused_by_mode"))
            dispatched = dispatched or not refused_by_mode
            informed += 0 if refused_by_mode else 1
            mutated = mutated or bool(outcome.mutations)
            if call.name == "go_mod":
                self.state.dependencies_changed = True
            if call.name in _SCAFFOLD_TOOLS and outcome.ok and outcome.mutations:
                self.state.scaffolded = True

            # Kept so a repeat of this exact call can be answered with what it
            # returned, instead of running it a second time to find out.
            self.state.seen_calls[fingerprint] = (
                self.state.seen_calls.get(fingerprint, 0) + 1
            )
            # Uncapped save for a capped context: ``append_tool_result`` applies
            # the per-tool insertion caps when an echo is played back, so what
            # is stored here only bounds memory. The old 600-character cut
            # replayed a rich first answer as a stub -- and a model handed a
            # worse answer than the one it remembered getting kept asking for
            # the original, seven times in one field run.
            #
            # A mode refusal is never stored. The fingerprint is the tool and its
            # arguments, with no mode in it, so a refusal earned in one mode
            # would answer the identical call made in the mode that *can* run it
            # -- and that is not a hypothetical. The field transcript: the
            # Verifier called ``patch_file`` and was correctly refused; the
            # ladder switched to the Coder; the Coder made the same call; the
            # fingerprint matched, the call was never dispatched, and the Coder
            # was handed "patch_file is not available in verifier mode" as the
            # result of its own patch. It then said so in prose, was counted as
            # narrating, and the run died seventeen turns later having changed
            # nothing.
            #
            # ``router.py``'s own comment beside ``_unknown_tool`` already states
            # the rule this restores: a tool hidden from a mode is deliberately
            # NOT a dead end, because a mode switch can make it callable. The
            # ``dead_ends`` ledger honoured that. This one did not.
            if not refused_by_mode:
                self.state.last_results[fingerprint] = outcome.for_model()[:6000]
            if outcome.truncated:
                self.state.truncated_at[fingerprint] = _volume(call)
            else:
                self.state.truncated_at.pop(fingerprint, None)
            if reason := outcome.meta.get("dead_end"):
                # The tool has said this exact call can never succeed as asked.
                # From here on it is answered from the ledger above, forever,
                # instead of being re-dispatched — or worse, counted.
                self.state.dead_ends[fingerprint] = str(reason)

            # A file that was just written is worth reading again; the ledger of
            # how often it has been read starts over. Without this, the read
            # limit below would eventually refuse the one re-read that matters.
            for mutation in outcome.mutations:
                self.state.reads.pop(mutation.path, None)

            slice_path, slice_range = _slice_path(call, outcome)
            result_msg = self.context.append_tool_result(
                call.name,
                outcome.for_model(),
                tool_call_id=call.id,
                path=slice_path,
                line_range=slice_range,
            )
            if slice_path is None:
                # Same tool, same bytes back: the older pair is superseded.
                #
                # Only single-call, prose-free replies participate, so a pair is
                # always removable whole — never an orphaned result, never lost
                # prose. Sliced reads sit this out: the slice ledger already
                # collapses those by path.
                dup_key = f"{call.name}|{hash(outcome.for_model())}"
                prior = self.state.dup_results.get(dup_key)
                if prior and prior[0] is not assistant_msg:
                    self.context.discard(*prior)
                    self.state.dup_results.pop(dup_key, None)
                if (
                    assistant_msg is not None
                    and not assistant_msg.content
                    and len(assistant_msg.tool_calls) == 1
                ):
                    pair = self.state.dup_results.setdefault(dup_key, [])
                    if not pair:
                        pair.append(assistant_msg)
                    pair.append(result_msg)

            # Said as a message of its own, deliberately, and placed here.
            #
            # Appending it to ``outcome.content`` instead would put a
            # run-relative sentence inside ``last_results[fingerprint]`` — where
            # it would be replayed forever — and would change the bytes
            # ``dup_key`` hashes, disabling duplicate collapse for the one tool
            # this is about. Both were measured on an earlier draft. So the note
            # goes after every ledger has been written, carrying no
            # ``tool_call_id``, exactly as the loop's other nudges do.
            if note := self._retrieval_overlap(call, outcome):
                self.context.append_tool_result(call.name, note)
                # A retrieval that returned only sections the run already had did
                # not inform this turn, so it must not count as progress.
                #
                # ``stalled_turns`` measures dispatch, and that is why the field
                # run survived twenty search_docs turns: every query was worded
                # differently, so every fingerprint was new, so every call
                # dispatched and reset the counter to zero. It stayed at zero
                # until the model finally repeated itself verbatim.
                #
                # Scoped to retrieval on purpose. The same idea applied to the
                # acting tools was measured to end a legitimate run: six declined
                # approvals on ``db/*.sql`` return a byte-identical refusal, and
                # counting those as six stalled turns kills the run the developer
                # is in the middle of steering.
                informed -= 1

            yield Event(
                EventType.TOOL_RESULT,
                {"id": call.id, "name": call.name, **outcome.as_dict()},
            )

        # Turn-level progress, judged on the batch rather than on any one call.
        #
        # A batch that dispatched nothing — every call in it a verbatim repeat
        # or a known dead end — moved the run nowhere, however many calls it
        # held. That is the unit the old detector got wrong twice over: it
        # counted *calls*, so three identical ones inside a single batch ended
        # a run in one turn with a message claiming three; and it counted them
        # *forever*, so the third ask across a whole read-only phase was fatal
        # no matter what happened in between. Turns without progress is the
        # thing actually worth ending a run over, so it is the thing counted.
        if informed > 0 or mutated:
            self.state.stalled_turns = 0
        else:
            self.state.stalled_turns += 1
            if self.state.stalled_turns >= MAX_STALLED_TURNS:
                worst_key, worst_n = max(
                    self.state.seen_calls.items(),
                    key=lambda item: item[1],
                    default=("", 0),
                )
                detail = (
                    f"; {worst_key.split(':', 1)[0]} was asked {worst_n} times"
                    if worst_n > 1
                    else ""
                )
                self.result = RunResult(
                    Outcome.NO_PROGRESS,
                    f"the last {MAX_STALLED_TURNS} tool-calling turns only "
                    "repeated earlier calls or known dead ends, and added "
                    f"nothing new{detail}",
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                return

        if mutated:
            yield from self._inner_loop()

    def _inner_loop(self) -> Iterator[Event]:
        """Format and lint what was just written, sub-second.

        The result goes into context as a tool message rather than being merely
        reported, because its whole purpose is to be in front of the model on the
        next turn while the edit is still what it is thinking about.
        """
        report = inner_loop(self.router, self.router.touched)
        yield Event(EventType.GATE, {"kind": "inner", **report.as_dict()})
        if not report.ok or report.warnings:
            self.context.append_tool_result("rules_lint", report.summary())

    # -- mode transitions -------------------------------------------------

    def _advance(self, result: TurnResult) -> Iterator[Event]:
        """Decide what happens after a turn that called no tools."""
        mode = self.state.mode
        text = result.chat.content or ""

        if mode is Mode.PLANNER:
            steps = _count_steps(text)
            if _asks_the_developer(text):
                # A question is not a plan, however it is numbered. Ending here
                # leaves the questions on screen as the last thing said, and the
                # developer's answer arrives as a follow-up on this transcript —
                # which is what `continued` is for and what the Planner was
                # waiting on all along.
                self.result = RunResult(
                    Outcome.DONE,
                    "the planner asked for a decision before it could plan; answer it "
                    "and the run continues from here",
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return
            if _refuses_to_plan(text) and not _is_scaffold_plan(text):
                # Not pinned, whatever it is. A refusal in the task layer is
                # read by every mode below for the rest of the run.
                if not self.state.planner_idle:
                    self.state.planner_idle += 1
                    self.context.append_tool_result(
                        "repo_map",
                        "You are the Planner. You do not write files, and nothing has "
                        "asked you to: the Coder that runs after you holds write_file "
                        "and patch_file, and it is the one that will apply this.\n\n"
                        "So a plan is not blocked by what you can reach. Write the "
                        "numbered steps, each naming the file it changes and carrying "
                        "an Accepts: line saying how that step is checked.",
                    )
                    return
                self.result = RunResult(
                    Outcome.DONE,
                    "the planner said it could not do the work; nothing was changed",
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return
            if steps == 0 and not _is_scaffold_plan(text):
                # A reply with no steps is not a plan, and the Planner is the one
                # mode positioned to say so: it has read the task and answered it.
                # Greetings, typos and questions land here, and so does a
                # clarifying question the developer has to answer before there is
                # anything to plan. Handing that text to the Coder as if it were a
                # plan is what turned "he how are you doijng" into seventeen turns
                # — the Coder had no step to execute, so every mode below it fired
                # in order against a workspace nothing had touched.
                # A preamble is not a conclusion, and the two look identical
                # here.
                #
                # "Let me see the rest of the handler to match its full shape."
                # is a turn whose tool call was never emitted. Ending on it
                # reports ``Done`` for a run that read eight files, wrote
                # nothing and never produced a plan -- which is exactly what the
                # field transcript shows twice, at seventeen turns and at
                # twenty, with the developer left asking "what happened?".
                #
                # Told apart by whether the Planner had started work, not by
                # reading the prose: a greeting or a typo is answered on the
                # first turn with no tool call behind it, so ``seen_calls`` is
                # empty and that reply still ends the run in one turn. A Planner
                # that has been reading the repository and then says something
                # with no step in it gets one nudge; a second such turn is a
                # Planner with nothing to say, and ends the run as before.
                if self.state.seen_calls and not self.state.planner_idle:
                    self.state.planner_idle += 1
                    self.context.append_tool_result(
                        "repo_map",
                        "You ended that turn with no plan and no tool call, after "
                        "reading the repository. That is what a turn looks like when "
                        "the call you were about to make was never sent.\n\n"
                        "Do one of three things now: call the tool you were about to "
                        "call; or write the plan as numbered steps, each with an "
                        "Accepts: line naming how it is checked; or, if the task "
                        "genuinely needs no change, say so in one line and stop.",
                    )
                    return
                self.result = RunResult(
                    Outcome.DONE,
                    "answered; no plan was needed and nothing was changed",
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return
            if _is_explanation(self.context.task_text, text, self.context.directives):
                # Numbered, and not a plan. "Explain this bootstrapper and how
                # it deviates from the template" is answered in numbered
                # paragraphs, and counting them as steps sent the answer to the
                # Coder — which had nothing to execute and either re-ran the
                # Planner's survey to a no-progress stop or went looking for
                # work and edited a file nobody had mentioned.
                #
                # The answer is already on screen; the run is over. If the
                # developer wanted the change as well, "go" arrives as a
                # follow-up on this transcript, which is what `continued` is
                # for and how the clarifying-question path already behaves.
                self.result = RunResult(
                    Outcome.DONE,
                    "answered the question; the reply describes the code rather than "
                    "proposing a change, so nothing was executed and nothing was "
                    'touched. Reply "go" if you want the changes it implies made',
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return
            self.state.plan = text
            self.context.set_plan(text)
            yield Event(EventType.PLAN, {"text": text, "steps": steps})
            self._switch(Mode.SCAFFOLDER if _is_scaffold_plan(text) else Mode.CODER)
            return

        if mode in (Mode.SCAFFOLDER, Mode.CODER, Mode.DEBUGGER):
            yield from self._verify()
            return

        if mode is Mode.VERIFIER:
            # The Verifier reports; it never fixes. Reaching here means the gate
            # was already run and the model has explained the failure, so the
            # next decision is whose problem it is.
            yield from self._route_failure(text)
            return

    def _verify(self) -> Iterator[Event]:
        """Run the gate and act on it. The one thing the model cannot skip.

        Skipped in exactly one case, and it is not the model's to invoke: the
        workspace is byte-for-byte what it was when the gate last ran. The gate
        is a function of the files and the toolchain, so re-running it there
        cannot produce a different verdict — it can only spend another `go
        build`, `go vet` and `swagger_check` arriving at the report already in
        context.

        That is not merely wasteful. Re-appending an identical report is what
        kept a real run alive: the Verifier read the same failure and wrote the
        same paragraph, the ladder sent it back to the Coder, and round it went.
        The model was being handed the exact input that produced the last reply
        and asked for a different one. So when nothing has changed it is told
        *that*, which is new information and the only thing here that is.
        """
        key = (self.router.mutations, tuple(self.router.touched))
        if self.state.last_gate is not None and key == self.state.gate_key:
            # Only reachable on a failing report: a clean one ends the run below.
            report = self.state.last_gate
            yield Event(EventType.GATE, {"kind": "full", "cached": True, **report.as_dict()})
            if report.ok and (unstarted := self._unstarted_work()):
                self.context.append_tool_result("go_build", unstarted)
                return
            blocker = report.blocked_by.name if report.blocked_by else "the gate"
            self.context.append_tool_result(
                "go_build",
                f"The gate was not re-run. Nothing in the workspace has changed since it "
                f"last ran, so its verdict cannot have changed: still blocked at "
                f"{blocker}, and the output above is still the whole of it.\n\n"
                "Nothing will move until a file does. Make the edit, or say plainly what "
                "is stopping you from making it — restating the failure is not a change.",
            )
            self._switch(Mode.VERIFIER)
            return

        report = full_gate(
            self.router,
            self.router.touched,
            dependencies_changed=self.state.dependencies_changed,
            baseline=self.state.baseline,
        )
        self.state.last_gate = report
        self.state.gate_key = key
        yield Event(EventType.GATE, {"kind": "full", **report.as_dict()})

        if report.ok:
            if unstarted := self._unstarted_work():
                # Clean because nothing was checked, not because nothing was
                # wrong. Every path-scoped stage records "nothing in scope" on an
                # empty change set, so on a repository that already builds this
                # report is what an unstarted run looks like -- and ending here
                # reports "nothing needed changing" when all the loop knows is
                # that nothing was changed.
                self.context.append_tool_result("go_build", unstarted)
                return
            if (missing := self._unwritten_targets()) and (
                self.state.unfinished_nudges < MAX_UNFINISHED_NUDGES
            ):
                # A clean gate on half a plan.
                #
                # The gate is a function of the files that changed, so it has
                # nothing to say about the ones that did not — a run that
                # applied step one of four passes it exactly as a finished run
                # does. That is how a field run ended DONE with "2 file(s)
                # changed and the gate is clean" having written the domain model
                # and neither the repository nor the handler, which is a struct
                # nothing constructs and a route nobody can call.
                #
                # Bounded, and the bound matters more than the nudge: a step the
                # model has decided against, or a path this reads out of the
                # plan that was never a target, must cost two turns and then be
                # reported honestly — not loop until the turn budget ends.
                self.state.unfinished_nudges += 1
                self.context.append_tool_result(
                    "go_build",
                    "The gate is clean, and it only checked what you changed — it "
                    "cannot see a step you have not started. Your plan set out to "
                    "write " + ", ".join(missing) + ", and "
                    f"{'none of those files has' if len(missing) > 1 else 'that file has not'}"
                    " been written this run.\n\n"
                    "Carry on with the next step. If one of those is no longer "
                    "needed — the plan changed, or it was never a file you meant to "
                    "write — say which and why in one line, and finish the rest.",
                )
                self._switch(Mode.CODER)
                return
            self.result = RunResult(
                Outcome.DONE,
                self._done_summary(report),
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
            return

        self.context.append_tool_result("go_build", report.summary())
        self._switch(Mode.VERIFIER)

    def _unstarted_work(self) -> str:
        """Why a clean gate is not a finished run, or ``""`` if it is.

        The gate is a function of the files. On an empty change set every
        path-scoped stage records "nothing in scope" and counts as ok, so on a
        repository that already builds the report comes back clean -- and the
        run ended DONE with "nothing needed changing; the gate is clean". The
        loop knows only that nothing *was* changed. Reporting that nothing
        *needed* to be is a different claim, and the developer acts on it: the
        field transcripts end "Done, 17 turns" and "Done, 20 turns" on runs that
        read eight files, wrote none, and were asked to write two.

        Only when the plan named files. A plan whose steps are checks rather
        than edits legitimately finishes without writing anything, and this must
        not turn that into a failure.
        """
        if not self.state.plan or self.router.touched:
            return ""
        if not _PLAN_EDITS.search(self.state.plan):
            # The plan asked to look at something, not to change it. A step that
            # reads, inspects or confirms is finished by reading, and calling that
            # an unstarted run would turn every investigation into a failure.
            return ""
        named = sorted({m.group(0) for m in _PLAN_PATH.finditer(self.state.plan)})
        if not named:
            return ""
        return (
            "The gate came back clean, and that is not worth anything yet: no file "
            "has been written this run, so every scoped stage had nothing to check. "
            "The plan names " + ", ".join(named) + ".\n\n"
            "This run is not finished, it is not started. Make the first edit, or "
            "say plainly what is stopping you from making it."
        )

    def _take_baseline(self) -> None:
        """Record what was already broken, before this run can break anything.

        Taken once, at the top of the run, because that is the only moment the
        workspace is definitely untouched -- and correctness here depends
        entirely on the timing. A snapshot taken later would contain the run's
        own damage and excuse it.

        The alternative was to keep blocking on it, and that is what shipped: a
        service with eight handlers predating the contract, every one of them
        missing ``Routes()``. Scoping the stage to touched files cured seven.
        The eighth is whichever handler the task is about, so a vertical slice
        was unshippable by construction -- the gate failed on damage the change
        did not cause, on every attempt, until the escalation budget ran out and
        the run was reported ``unverified``.

        Cheap enough to take unconditionally: one sidecar call, measured in
        hundredths of a second. Failure is not fatal and not reported -- an
        empty baseline is exactly the behaviour that shipped, so the worst case
        of not getting one is the status quo.
        """
        try:
            outcome = self.router.run_gate_tool("swagger_check", {})
        except Exception:  # noqa: BLE001 - a baseline is an optimisation, not a precondition
            return
        if isinstance(outcome, ToolResult):
            keys = outcome.meta.get("violations") or ()
            self.state.baseline = frozenset(str(k) for k in keys)

    def _route_failure(self, verdict: str) -> Iterator[Event]:
        """Coder twice, then the Debugger, then stop.

        The escalation matters more than the numbers. A third identical Coder
        attempt on a gate that has failed twice is not more likely to work — the
        model is applying the same understanding to the same evidence. The
        Debugger has different instructions and a playbook, which is a different
        understanding rather than another try.
        """
        del verdict  # already in context; the routing decision is ours

        # The budget counts fix attempts. It was counting Verifier turns.
        #
        # This runs once per tool-free Verifier turn, and every one of them spent
        # a slot whether or not anything had been tried. In the field transcript
        # five slots went in eleven turns, of which two were edits -- so "two
        # Coder attempts, then the Debugger" was exhausted by narration, and the
        # ladder ran out while the plan still had an unwritten step.
        #
        # An attempt is charged when the workspace changed since the last time
        # round. A Coder that answers a failing gate with prose is not spending
        # the budget; it is caught by ``_narrating`` after three such turns,
        # which is a better outcome than a ladder that ends the run early having
        # never had a fix to judge.
        report = self.state.last_gate
        blocker = report.blocked_by.name if report and report.blocked_by else ""
        if blocker and blocker != self.state.blocked_stage:
            # A different stage blocks than last time: the previous one was
            # cleared. Charging this problem for the last one's attempts is how a
            # run that fixed two things in a row was reported as having failed
            # three times at the second.
            self.state.blocked_stage = blocker
            self.state.attempts = 0
            self.state.cycles = 0

        attempted = self.router.mutations != self.state.route_mutations
        self.state.route_mutations = self.router.mutations

        if self.state.attempts < MAX_ATTEMPTS:
            if attempted:
                self.state.attempts += 1
            self._switch(Mode.CODER)
            return

        if self.state.cycles < MAX_DEBUG_CYCLES:
            if attempted:
                self.state.cycles += 1
            self._switch(Mode.DEBUGGER)
            return

        report = self.state.last_gate
        self.result = RunResult(
            Outcome.UNVERIFIED,
            "the gate did not come clean after "
            f"{MAX_ATTEMPTS} coder attempts and {MAX_DEBUG_CYCLES} debug cycles"
            + (f"; blocked at {report.blocked_by.name}" if report and report.blocked_by else "")
            + self._unfinished(),
            self.context.turn,
            tuple(self.router.touched),
            report,
        )
        yield Event(EventType.ERROR, {"message": self.result.summary})

    def _abort(self) -> RunResult:
        return RunResult(
            Outcome.ABORTED,
            "stopped by the developer"
            + (f"; {len(self.router.touched)} file(s) had already changed"
               if self.router.touched else " before anything changed"),
            self.context.turn,
            tuple(self.router.touched),
            self.state.last_gate,
        )

    def _restated_the_plan(self, result: TurnResult) -> bool:
        """Whether this turn did nothing but repeat the plan it was given.

        Deliberately narrower than ``_repeating``, which watches for a model
        stuck in a loop and needs three replies before it is sure. This is a
        different signal and one reply is conclusive: an executing mode was
        handed a plan, called no tool, and said the plan back.
        """
        return (
            self.state.mode in (Mode.CODER, Mode.SCAFFOLDER)
            and not result.chat.tool_calls
            and bool(self.state.plan.strip())
            and (result.chat.content or "").strip() == self.state.plan.strip()
        )

    def _relay(self, event: Event | None) -> None:
        """Send a transient event out of band, and never let it cost the run.

        ``None`` is the ordinary case: the coalescer holds a fragment back until
        it has enough text or enough time has passed, and says so by returning
        nothing.

        The sink is a client that may have gone — a closed event loop, a panel
        that was torn down. Streaming is a view of a turn and never the turn
        itself, so a broken sink is switched off rather than raised: the answer
        still arrives in full as the ``assistant`` message at the end of the
        turn, which is the message every client treats as authoritative anyway.
        """
        if event is None or not self._relaying:
            return
        try:
            self.on_event(event)
        except Exception:  # noqa: BLE001 - see the docstring
            self._relaying = False

    def _switch(self, mode: Mode) -> None:
        """Enter a mode, unless we are already in it.

        Both halves of "already" are checked. ``state.mode`` is this loop's idea
        of where it is; ``context.mode`` is what the message list actually
        carries, and on a follow-up they start out disagreeing — the loop is
        newly built and defaults to Planner, while the context is still wherever
        the previous run left it. Guarding on the loop's copy alone let a
        conversation that ended in the Debugger answer its next message with the
        Debugger's overlay and budget under a loop dispatching the Planner's
        tools.
        """
        if mode is self.state.mode and mode is self.context.mode and self.context.turn > 0:
            return
        self.state.mode = mode
        self.context.switch_mode(mode, mode_instruction(mode))

    # -- helpers ----------------------------------------------------------

    def _unfinished(self) -> str:
        """Files the plan named that were never written.

        A run that stops with half its plan applied says so. The field
        transcript ended "38 turns · 1 file, blocked at swagger_check" — true,
        and it buried the thing that actually went wrong: the repo function
        landed and the handler never did, so the developer was left with a
        query nothing called. The blocked stage was the pre-existing one; the
        unwritten handler was this run's.

        Read off the plan text rather than tracked per step, because the loop
        never learns which step it is on — a step is prose, and the only
        machine-checkable thing in it is the paths it names.
        """
        return (
            ". The plan named files this run never wrote: " + ", ".join(missing)
            if (missing := self._unwritten_targets())
            else ""
        )

    def _unwritten_targets(self) -> list[str]:
        """Targets of the plan's edit steps that no file change reached.

        Empty when the plan named none, when every one was written, or when
        *none* was — the last because a plan naming its files by some convention
        this does not read would otherwise report all of them, which is noise
        dressed as a finding, and because a run that wrote nothing at all is
        ``_unstarted_work``'s to describe rather than this one's.
        """
        if not self.state.plan:
            return []
        targets = _plan_targets(self.state.plan)
        missing = [p for p in targets if p not in set(self.router.touched)]
        return [] if len(missing) == len(targets) else missing

    def _retrieval_overlap(self, call: ToolCall, outcome: ToolResult) -> str:
        """What to tell a run that keeps asking the corpus the same thing.

        The field failure this exists for: sixteen Coder turns, every one of them
        ``search_docs`` and nothing else, each query worded differently so every
        fingerprint was new and ``stalled_turns`` never left zero. Three of those
        queries returned the same four sections — the same 196 lines at turns 21,
        22 and 23 — and the tool said nothing about it, because it cannot. It
        answers with the best four sections it has whether or not they are any
        good, and its scores do not separate a question the corpus answers from
        one that merely uses the corpus's vocabulary (see
        ``MAX_RETRIEVAL_REPEATS``).

        So the judgement is made here, where the run's history is, and on the
        answer rather than on the question. A retrieval that brings back sections
        this run has already been given has not added anything, however new the
        wording was.

        Returns ``""`` when there is nothing to say, which is the common case.
        """
        if call.name != "search_docs" or not outcome.ok:
            return ""
        hits = frozenset(str(h) for h in (outcome.meta.get("hits") or ()))
        if not hits:
            # "nothing matches" is already an explicit answer; it needs no gloss,
            # and counting it as a repeat would punish the one reply that is
            # honest about coming back empty.
            self.state.retrieval_repeats = 0
            return ""
        try:
            query = str((call.parsed() or {}).get("query", ""))
        except ValueError:
            query = ""

        seen: set[str] = set()
        source = ""
        for earlier_query, earlier in self.state.retrievals:
            if len(hits & earlier) / len(hits) >= RETRIEVAL_OVERLAP and not source:
                source = earlier_query
            seen |= earlier
        self.state.retrievals.append((query, hits))

        fresh = hits - seen
        if fresh:
            self.state.retrieval_repeats = 0
            return ""
        self.state.retrieval_repeats += 1

        if self.state.retrieval_repeats < MAX_RETRIEVAL_REPEATS:
            return (
                f"Those are the same sections {source or 'an earlier search'!r} already "
                "returned — this search added nothing you had not been given.\n\n"
                "Rewording the question will not reach different sections. Ask about "
                "something else, or work from what is already above."
            )
        return (
            f"That is {self.state.retrieval_repeats} searches in a row returning sections "
            "you already have. The knowledge base does not cover this question — that is "
            "an answer, not a gap to keep searching for.\n\n"
            "Stop rephrasing it. Follow the pattern in the nearest existing code instead, "
            "and if the step genuinely cannot be done without knowing this, say which step "
            "and what you need, in one line."
        )

    def _re_reading(self, call: ToolCall) -> list[str]:
        """The ranges a path has already been read at, once that is too many.

        Returns empty until the limit is reached, so the ordinary read-edit-read
        cycle is untouched; the ledger is cleared for a path the moment that
        path is written.
        """
        if call.name != "read_file":
            return []
        parsed = _safe_args(call)
        path = parsed.get("path") if isinstance(parsed, dict) else None
        if not isinstance(path, str) or not path:
            return []

        start, end = parsed.get("start"), parsed.get("end")
        where = f"lines {start}-{end}" if start or end else "the whole file"
        seen = self.state.reads.setdefault(path, [])
        if len(seen) >= MAX_READS:
            return seen
        seen.append(where)
        return []

    def _narrating(self) -> str:
        """Whether a mode that can write has stopped writing and started talking.

        Counts actions, not words, and that is the whole point. Every other
        detector here compares reply text, and the run this exists for evaded
        all of them by paraphrasing: "Making the edit now", then "I have the
        exact current text. Making the edit now", then the same with the gate
        restated in front of it. Six turns of that, across Coder and Debugger,
        with `patch_file` called once. The Verifier eventually wrote *"I'm stuck
        in a loop — I keep saying I'll make the edit but not actually calling
        patch_file"*, which is the diagnosis this method makes two turns
        earlier and acts on.

        Only executing modes count. The Planner and the Verifier are *supposed*
        to end their turn with prose and no tool call — that is how they hand
        on — so counting them would stop every healthy run at its first
        handoff.

        One idle turn is ordinary: it means "I am done, run the gate". Two is
        the ping-pong starting. Three is a model narrating an edit it is not
        making, and no further turn of it will produce one.
        """
        if self.state.mode not in _EXECUTING:
            return ""
        if self.router.mutations != self.state.idle_mutations:
            # Something landed. Whatever it was saying, it was also working.
            #
            # Its own watermark, not ``mutations_seen``: that one is maintained
            # by ``_stuck`` and is already synced by the time this runs, so
            # sharing it made every edit invisible here and stopped a Coder that
            # was editing on every other turn.
            self.state.idle_mutations = self.router.mutations
            self.state.idle = 0
            return ""
        self.state.idle += 1
        if self.state.idle < MAX_IDLE_EXECUTING:
            return ""
        return (
            f"{self.state.mode} spent {self.state.idle} turns describing an edit "
            "without making one — no tool was called and no file changed"
        )

    def _repeating(self, text: str) -> str:
        """Why this tool-free reply counts as no progress, or ``""`` if it does not.

        ``_stuck`` judges a turn by the arguments it dispatched, so a turn that
        dispatches nothing is invisible to it. That is the hole a typo fell
        through: the modes kept advancing, each one restated the same refusal,
        and the loop had no way to notice because not one of those turns called
        a tool. Whitespace is normalised because a model that re-wraps the same
        paragraph has not made progress either.

        Two ledgers, because a stuck run has two shapes and neither sees the
        other.

        ``said`` is the original: the same reply three turns running, whatever
        mode said it. It catches a model that has given up and is restating one
        refusal as the ladder walks past it.

        ``echoes`` is keyed by mode as well as text and counts occurrences
        rather than a consecutive run — which is the shape ``said`` cannot see
        and the one that cost a real session nineteen turns. Coder and Verifier
        alternated, each repeating its own sentence, so ``said`` held
        ``[coder, verifier, coder]`` and never reached three of anything. The
        run was ended by the escalation ladder running out, ten turns and five
        escalation slots later, and reported as ``unverified`` — which named the
        gate as the problem when the problem was that nothing was being
        attempted at all.
        """
        normalised = " ".join(text.split())
        if not normalised:
            return ""

        self.state.said.append(normalised)
        del self.state.said[:-NO_PROGRESS_REPEATS]
        if len(self.state.said) == NO_PROGRESS_REPEATS and len(set(self.state.said)) == 1:
            return f"the same reply {NO_PROGRESS_REPEATS} turns running, with no tool call"

        key = (str(self.state.mode), normalised)
        self.state.echoes[key] = self.state.echoes.get(key, 0) + 1
        if self.state.echoes[key] >= MODE_ECHO_LIMIT:
            return (
                f"the {self.state.mode} said the same thing {self.state.echoes[key]} times "
                "without calling a tool; the loop is cycling between modes rather than "
                "making progress"
            )
        return ""

    def _thrashing(self) -> str:
        """Whether the run is spending its turns on eviction rather than work.

        Compaction is designed to be rare: it retains to a token floor
        precisely so the next turn does not immediately trip the threshold
        again. When it stops being rare, the working set is bigger than the
        budget can hold, and every compaction evicts the file the model then
        re-reads — which puts it straight back over the threshold. That is a
        closed circuit and it does not open on its own.

        A real session ran it seventy-one turns: two files totalling a thousand
        lines against a 24k Planner budget, twenty-five compactions, not one
        plan, and it ended on the turn budget rather than on anything that
        understood what had happened. Reported as ``exhausted``, which named the
        turn cap as the problem.

        Judged on density rather than a total, because a long healthy run does
        compact several times — just never three times inside eight turns.
        """
        window = [t for t in self.state.compactions if self.context.turn - t < COMPACTION_WINDOW]
        if len(window) < MAX_CLOSE_COMPACTIONS:
            return ""
        return (
            f"{len(window)} compactions in {COMPACTION_WINDOW} turns: the working set is "
            f"larger than the {self.context.budget:,}-token {self.state.mode} budget can "
            "hold, so each turn is evicting what the last one read. Narrow the task, or "
            "work on fewer files at once"
        )

    def _compact(self) -> Iterator[Event]:
        self.state.compactions.append(self.context.turn)
        before = self.context.usage().total
        recap = self.context.compact(self._summarise)
        yield Event(
            EventType.GATE,
            {
                "kind": "compaction",
                "before": before,
                "after": self.context.usage().total,
                "turns": recap.turns if hasattr(recap, "turns") else None,
            },
        )

    def _summarise(self, messages: Sequence[Message]) -> Recap:
        """Summarise the evicted working set into a structured recap.

        **The signature is the point.** ``ContextManager.compact`` is typed
        ``Callable[[Sequence[Message]], Recap]`` and calls ``.markdown()`` on
        what it gets back. The first version of this method took a ``str`` and
        returned a ``str``, so the first compaction of any long run raised a
        ``TypeError`` inside the ``except Exception`` below, returned a *list*
        slice, and then died on ``recap.markdown()`` one frame later — where the
        real cause was invisible. Every compaction test supplied its own
        correctly-typed summariser and drove the context manager directly, so
        461 green tests sat on top of it.

        Structured rather than prose, because ``do_not_retry`` is what stops the
        post-compaction agent cheerfully repeating the dead end that got it
        here. A prose summary loses exactly the field that earns the compaction.

        Falls back to a recap built from the tail if the call fails or comes
        back unparseable: a degraded recap beats ending the run, which is what
        the original ``except`` was reaching for and did not achieve.
        """
        turns = (
            min((m.turn for m in messages), default=self.context.turn),
            max((m.turn for m in messages), default=self.context.turn),
        )
        transcript = "\n\n".join(
            f"[{m.role}{'' if not m.path else ' ' + m.path}] {m.content}" for m in messages
        )

        read = self._read_paths(messages)
        modified = tuple(self.router.touched)
        fallback = Recap(
            goal=self.state.plan.splitlines()[0] if self.state.plan else "",
            files_modified=modified,
            files_read=read,
            open_items=("the recap could not be summarised; the tail is preserved below",),
            decisions=(transcript[-2000:],) if transcript else (),
            turns=turns,
        )

        try:
            reply = self.client.chat(
                [{"role": "user", "content": _RECAP_PROMPT + transcript}],
                # `fast`, which is what §6.5 specifies and what
                # `LLMConfig.model_for` actually accepts.
                #
                # This said "summariser" for its whole life, and `model_for`
                # rejects that on both deployments — it whitelists coder, fast
                # and embed. The model is resolved inside `chat` before any
                # request is sent, so the ValueError landed in the `except`
                # below and every compaction in production silently returned
                # the fallback recap. Which means `do_not_retry` — the field
                # context.py calls "what stops the post-compaction agent
                # cheerfully repeating the dead end that got it here" — has
                # never once been populated outside a test.
                #
                # The gateway's own role table does map "summariser"
                # (proxy.py), which is presumably where the name came from; the
                # client never gets far enough to use it.
                role="fast",
                max_tokens=1024,
                enable_thinking=False,
                # Compaction is a real cost against the developer's quota, and
                # an unmetered one is a hole in the accounting that grows with
                # exactly the long runs the ledger most needs to explain.
                metering=Metering(
                    session_id=self.session_id,
                    turn=self.context.turn,
                    mode="summariser",
                    estimated_tokens=estimate_tokens(_RECAP_PROMPT + transcript),
                ),
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            # Ours, not the endpoint's. A misconfigured role or a bad argument
            # is a permanent failure that degrades every compaction for the life
            # of the process, and the broad `except` below hid exactly that for
            # this method's entire history. Announced so it is visible in the
            # transcript rather than inferred later from a thin recap.
            self.on_event(
                Event(
                    EventType.ERROR,
                    {
                        "where": "summariser",
                        "message": f"the recap could not be requested: {exc}",
                        "effect": "compaction degraded to a fallback recap; dead ends "
                        "will not be carried across it",
                    },
                )
            )
            return fallback
        except Exception:  # noqa: BLE001 - a degraded recap beats ending the run
            return fallback

        parsed = _parse_recap(reply.content or "")
        if parsed is None:
            return fallback
        return Recap(
            goal=parsed.get("goal", "") or fallback.goal,
            plan_step=parsed.get("plan_step", ""),
            files_created=tuple(parsed.get("files_created") or ()),
            files_modified=tuple(parsed.get("files_modified") or modified),
            # Not taken from the model. What was evicted is a fact about this
            # compaction, and the loop is the only thing that knows it.
            files_read=read,
            decisions=tuple(parsed.get("decisions") or ()),
            verified=tuple(parsed.get("verified") or ()),
            open_items=tuple(parsed.get("open_items") or ()),
            do_not_retry=tuple(parsed.get("do_not_retry") or ()),
            turns=turns,
        )

    @staticmethod
    def _read_paths(messages: Sequence[Message]) -> tuple[str, ...]:
        """Files whose contents this compaction is about to throw away.

        Recovered from the messages rather than asked of the summariser, so the
        list is right even when the model returns nothing usable — and because
        it is a fact about the eviction, which the summariser is not told about.

        ``Message.path`` is set only by ``_slice_path``, and only for a
        successful ``read_file``. These are reads, not writes: the previous
        version returned them as ``files_modified``, which told the model it had
        edited files it had merely opened.
        """
        seen = [m.path for m in messages if m.path]
        return tuple(dict.fromkeys(seen))

    def _done_summary(self, report: GateReport) -> str:
        """What the developer reads when a run ends DONE.

        Read off the report rather than off the outcome, because ``ok`` now
        covers two different claims: the stages passed, and the stages did not
        apply. Saying "the gate is clean" for the second is the same overclaim
        D-42 refuses from the model, pointed at the developer instead — and on a
        workspace with no root ``go.mod`` it is the ordinary case, not a corner.
        """
        files = self.router.touched
        verified = any(not r.skipped for r in report.results)
        if not files:
            return (
                "nothing needed changing; the gate is clean"
                if verified
                else "nothing needed changing, and the gate had nothing to verify"
            )
        listed = "\n".join(f"  - {p}" for p in files)
        if not verified:
            reason = report.results[0].skipped if report.results else "no applicable stage"
            return f"{len(files)} file(s) changed, but the gate did not run ({reason}):\n{listed}"
        # The gap goes in the headline, not in a footnote after it. Reaching
        # here with targets outstanding means the run was asked twice to finish
        # them and did not, so "the gate is clean" on its own is the overclaim
        # D-42 refuses from the model, made by the loop instead: every stage
        # that passed was scoped to the files that changed.
        if missing := self._unwritten_targets():
            return (
                f"{len(files)} file(s) changed and the gate is clean, but the plan is "
                f"not finished — it set out to write {', '.join(missing)}, and "
                f"{'those were' if len(missing) > 1 else 'that was'} never written:\n"
                f"{listed}"
            )
        return f"{len(files)} file(s) changed and the gate is clean:\n{listed}"


def _safe_args(call: ToolCall) -> Any:
    """The arguments for display, tolerating malformed JSON.

    The router will reject bad arguments with a message the model can act on.
    Raising here instead would kill the run over the very thing the run is
    supposed to recover from.
    """
    try:
        return call.parsed()
    except ValueError:
        # Named for what it is. Rendered, ``git_status {"_raw":"{"}`` reads as
        # though the router invented a parameter and passed it on — it did not,
        # ``_coerce`` refused the call and told the model why. A pilot reported
        # that line as the bug, which cost the report its actual evidence.
        return {"_malformed_arguments": call.arguments[:500]}


#: Parameters that bound how much of an answer comes back, never what the
#: answer is. Excluded from the fingerprint because varying one is not a new
#: question: a Planner that had established a legacy service has no ``Routes()``
#: method asked again as ``max: 200``, got the same "no matches", and bought two
#: more dispatched turns for it. Every ledger keyed on the raw argument string
#: saw a call it had never seen.
#: A tuple, not a set: ``_volume`` returns the first one present, and a set's
#: iteration order would make that answer depend on the hash seed.
_VOLUME_PARAMS = ("max", "limit")


def _volume(call: ToolCall) -> int:
    """How much this call asked for, or 0 when it did not say."""
    try:
        parsed = call.parsed()
    except ValueError:
        return 0
    if not isinstance(parsed, dict):
        return 0
    for name in _VOLUME_PARAMS:
        value = parsed.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


def _fingerprint(call: ToolCall) -> str:
    """What makes two calls the same question.

    Falls back to the raw string when the arguments do not parse — a model that
    resends the same malformed arguments is exactly what the ledgers are for, so
    this must not be the thing that raises on them.
    """
    try:
        parsed = call.parsed()
    except ValueError:
        return f"{call.name}:{call.arguments}"
    if not isinstance(parsed, dict):
        return f"{call.name}:{call.arguments}"
    kept = {k: v for k, v in parsed.items() if k not in _VOLUME_PARAMS}
    return f"{call.name}:" + json.dumps(kept, sort_keys=True, separators=(",", ":"))


def _slice_path(call: ToolCall, result: ToolResult) -> tuple[str | None, tuple[int, int] | None]:
    """The file a tool result is *about* and the lines it covers.

    Only ``read_file`` supersedes: a read that contains an earlier one replaces
    it rather than stacking beside it. A mutation is not a supersede — the
    record that a file was written is not made obsolete by writing it again.

    The range comes from the result's ``span``, which the tool clamped to the
    file, rather than from the call's ``start``/``end``. An older runtime whose
    ``read_file`` predates ``span`` reports ``None``, which reads as "the whole
    file" and collapses the way it always did.
    """
    if call.name != "read_file" or not result.ok:
        return None, None
    parsed = _safe_args(call)
    path = parsed.get("path") if isinstance(parsed, dict) else None
    if not isinstance(path, str):
        return None, None
    span = result.meta.get("span")
    if isinstance(span, (list, tuple)) and len(span) == 2:
        try:
            return path, (int(span[0]), int(span[1]))
        except (TypeError, ValueError):
            pass
    return path, None


#: A numbered step, however the model chose to dress it up.
#:
#: This matched a bare ``1. `` at line start and nothing else, and that is the
#: whole of a run that spent twelve turns refusing to start work. The Planner
#: produced a real eight-step plan with ``Accepts:`` on every step — and wrote
#: the step titles as ``**1. Add the repo function**``. Zero steps matched, so
#: ``_advance`` read it as "not a plan", ended the run ``done``, and the
#: developer's "go" arrived as a follow-up that began by planning again. "do
#: it", "you are not writing anything" and one obscenity later, the model was
#: still in the Planner, correctly reporting that it had no write tools.
#:
#: Markdown heading markers, bold and italics all sit between the line start and
#: the number, and a plan is not less of a plan for being formatted. ``Step 1:``
#: is here for the same reason: it is a step, and the loop's job is to recognise
#: one rather than to insist on a syntax the prompt never specified.
_STEP = re.compile(
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:[*_]{1,3}[ \t]*)?"
    r"(?:\d+[.)][ \t]|step[ \t]+\d+[.):][ \t])",
    re.MULTILINE | re.IGNORECASE,
)


def _count_steps(plan: str) -> int:
    return len(_STEP.findall(plan))


#: A workspace-relative Go/SQL/YAML path as a plan writes one. Deliberately
#: narrow: it has to match what the router reports in `touched` for the
#: comparison in ``_unfinished`` to mean anything.
_PLAN_PATH = re.compile(r"\b[\w./-]+/[\w.-]+\.(?:go|sql|ya?ml)\b")

#: A numbered step that asks for a change rather than a look. Conservative on
#: purpose: the cost of missing one is the old behaviour, and the cost of a
#: false positive is telling a developer their finished read-only run failed.
#: Anchored at the step number so a verb in prose underneath does not count.
_PLAN_EDITS = re.compile(
    # Markdown decoration between the line start and the number, exactly as
    # ``_STEP`` allows it. A plan is not less of a plan for being formatted,
    # and ``**1. Add the repo function**`` is the shape the Planner reaches for
    # most: it matched ``_STEP`` and not this, so a plan counted its steps and
    # named no edit, and ``_unstarted_work`` let a run that wrote nothing
    # finish clean.
    r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?(?:[*_]{1,3}[ \t]*)?\d+[.)]\s*[-*>\s]*\**\s*"
    # The step may lead with the file it is about before it says what to do:
    # ``1. **`core/domain/product.go`** — add the Product struct``. That shape
    # matched nothing, so a four-step plan read as an investigation and the run
    # that applied one step of it reported DONE. Only a path is allowed here,
    # not arbitrary prose — widening this to "anything up to a verb" is how
    # "Confirm the handler needs no change" becomes an edit step.
    r"(?:[`*\"']*[\w./-]+\.(?:go|sql|ya?ml)[`*\"']*\s*[-—–:]+\s*)?"
    # Stems, and the trailing ``\b`` is the whole point of writing them this
    # way. Without it ``add`` matched "Adds", ``register`` matched "Registers"
    # and ``wire`` matched "Wires" -- so a numbered *explanation* whose
    # paragraphs open "1. Creates the Temporal worker" / "2. Registers the
    # workflow" / "3. Wires the lifecycle hooks" read as four edit steps, and
    # the answer to a question was pinned as a plan and handed to the Coder.
    #
    # A plan gives instructions, and an instruction is imperative. The third
    # person singular is a description of what the code already does and no
    # plan is ever written in it. ``e`` and ``ing`` are allowed, because
    # "1. Creating the repository" is a step; ``s`` is not, because
    # "1. Creates the repository" is a sentence about code that exists.
    r"(?:add|creat|writ|edit|updat|modify|modifying|chang|register|wir|"
    r"implement|renam|delet|remov|insert|append|scaffold|generat|refactor|fix)"
    r"(?:e|ing)?\b",
    re.MULTILINE | re.IGNORECASE,
)


def _plan_targets(plan: str) -> list[str]:
    """The files the plan's *edit* steps set out to write, in plan order.

    One target per step, and it is the first path the step names. Plans are
    written with the file first and the reasoning after — ``1. **product.go** —
    add the struct, mirroring core/domain/user.go`` — so every path after the
    first is a reference, an example or a neighbour to copy. Counting those as
    targets tells a finished run it forgot to write a file it was only asked to
    look at, which is worse than not checking at all.

    Steps that ask for a look rather than a change contribute nothing: a plan
    whose steps are checks is finished without writing anything.
    """
    targets: list[str] = []
    for step in _STEP_START.split(plan):
        if not _PLAN_EDITS.search("1. " + step.lstrip()):
            continue
        if found := _PLAN_PATH.search(step):
            if found.group(0) not in targets:
                targets.append(found.group(0))
    return targets

#: A reply that says it cannot do the work, rather than saying how it will be
#: done. Matched only near the start, so a plan that mentions a limitation in
#: its eighth step is untouched -- what this catches is a refusal standing where
#: the plan should be.
#: The first numbered step, which is where the preamble ends.
_STEP_START = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)

_REFUSES = re.compile(
    r"\b(?:i (?:can'?t|cannot|am unable to|do(?:es)? not have|don'?t have|have no)"
    r"|no (?:write|edit|patch) tools?"
    r"|physically cannot"
    r"|not available to me"
    r"|read-only(?: in this session| toolset))",
    re.IGNORECASE,
)
#: How much of a preamble counts as "where the plan should be".
_REFUSAL_WINDOW = 400



def _asks_the_developer(plan: str) -> bool:
    """Whether the Planner asked for a decision instead of producing a plan.

    The Planner is told it may ask up to four clarifying questions, and it does.
    Until now a reply made entirely of them still went to the Coder, because
    ``_count_steps`` counts numbered lines and a numbered list of questions is
    numbered. This is the sibling of ``_restated_the_plan`` — the same handoff
    failing at the other end — and it is the one that cost nineteen turns: the
    Coder had no step to execute, so it produced prose; ``_verify`` ran the gate
    on a workspace nothing had touched; it failed on damage that was there
    before the session started; the Verifier reported it; the ladder sent it
    back to the Coder. Round that went until the escalation budget ran out, with
    four unanswered questions still on screen and the run reported as
    ``unverified`` — blaming the gate for a handoff that should never have
    happened.

    Two signals, and both are required. The Planner's own instruction says every
    real step carries an ``Accepts:`` line, so a reply with none is not a plan
    by our own definition — but that alone would also catch the terse one-line
    plans, so it has to be paired with the questions actually being present. A
    genuine eight-step plan that happens to ask something keeps its ``Accepts:``
    lines and is unaffected.
    """
    if _ACCEPTS.search(plan):
        return False
    return plan.count("?") >= MIN_QUESTIONS


def _refuses_to_plan(text: str) -> bool:
    """Whether the Planner said it could not do the work.

    ``_count_steps`` counts numbered lines, and a refusal written as a numbered
    list of files to paste by hand is numbered. One reached ``set_plan``, which
    writes into the pinned task layer that sits above the working set and that
    compaction is forbidden to touch -- so every Coder, Verifier and Debugger
    turn for the next sixteen turns read, as its plan, "I can't: I have no
    write tools in this session."

    The belief is also false, which is what makes this worth catching rather
    than reporting. The Planner is read-only by design; the Coder that follows
    it holds ``patch_file`` and ``write_file``. Nothing was blocked -- the run
    talked itself out of starting.

    Read from the preamble only -- the text before the first numbered step. A
    refusal stands *where the plan should be*, so that is the only place worth
    looking; a caveat inside step three ("I cannot run the database locally, so
    this step is checked by the build alone") is part of a plan that exists, and
    reading it as a refusal would throw away good work for honest prose.
    """
    head = _STEP_START.split(text, maxsplit=1)[0]
    return bool(_REFUSES.search(head[:_REFUSAL_WINDOW]))


#: An ``Accepts:`` line — the acceptance criterion the Planner is told to put on
#: every step. It has to start a line, or the word appearing in prose would
#: count, but the indent is generous: the Planner writes it as a sub-bullet
#: under a numbered step, and an eight-space ``- Accepts:`` failing to match
#: cost a real plan the one signal that stops ``_asks_the_developer`` reading it
#: as a bare question.
#:
#: This module used to bind ``_ACCEPTS`` twice. Python took the second, so the
#: first never ran, and the live pattern quietly changed under anyone reading
#: the dead one. The dead binding is gone; this is the only one.
#:
#: Deliberately not widened with ``|``. ``_ACCEPTS`` is the whole reply-side test
#: in ``_is_explanation``, so admitting a markdown table row
#: (``| Accepts: | the criterion |``) would pin an explanation that documents the
#: step format as a plan and hand it to the Coder — the failure this guard exists
#: to prevent, arriving through the guard itself.
_ACCEPTS = re.compile(
    r"^[ \t]{0,12}(?:[-*>+][ \t]*)*(?:\[[ xX]?\][ \t]*)?(?:\d+[.)][ \t]*)?"
    r"\**[ \t]*Accepts[ \t]*:",
    re.MULTILINE | re.IGNORECASE,
)


#: A task that asks to be told something.
#:
#: Widened from a six-word list, every miss of which cost the same thing: the
#: task fell through to "not a question", the Planner's numbered answer was
#: pinned as a plan, and the Coder went looking for work in files nobody had
#: named. "what all have been done in this repo", "give me an overview of the
#: repo", "analyse the objection handler" and "review the bootstrapper" all
#: missed, and all four are read-only requests a developer types every day.
#:
#: Three kinds of asking, and they need different anchoring.
#:
#: *Said in so many words* -- explain, describe, summarise, overview. These
#: turn up anywhere in a sentence ("also tell me how it deviates"), so they are
#: matched anywhere.
#:
#: *Asked as an inspection* -- analyse, review, audit, examine, compare. A
#: review is a read with a report at the end. The Planner is not being asked to
#: change what it is reviewing, and a run that "reviews" by editing has
#: answered a different question.
#:
#: *Asked as a question* -- what, why, how, where, which, when, who. These are
#: anchored to the start of a clause, because they are also ordinary words in
#: this domain: "add a WHERE clause" and "the handler needs a When field" are
#: work, and an interrogative that is not leading a clause is not asking
#: anything.
_ASKS_TO_BE_TOLD = re.compile(
    r"(?:"
    r"\b(?:explain|explanation|describe|description|clarify|"
    r"summar(?:y|ies|ise|ize|ising|izing)|overview|rundown|breakdown|"
    r"walk me through|talk me through|take me through|walk through|"
    r"tell me|show me|remind me|"
    r"analy(?:se|ze|sis|ses|zes|sing|zing)|review|reviewing|audit|assess|"
    r"evaluate|inspect|examine|investigate|compare|critique|"
    r"what all|how come|any idea|anything else)\b"
    r"|(?:^|[.;:!?\n,]|\b(?:and|or|but|also|then|so|please)\b)[ \t]*"
    r"(?:can you |could you |would you |do you know |i wonder )?"
    r"\b(?:what|why|how|where|which|when|who|whose)\b"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

#: Where a command can start.
#:
#: The old ``_ASKS_FOR_WORK`` matched a work word *anywhere*, and that is what
#: made "explain what the build does" a build: ``build`` is a noun here, and so
#: are ``update`` in "the update flow", ``change`` in "the change detection
#: logic", ``port`` in "the port mapping", ``wire`` in "the wire protocol" and
#: ``register`` in "what does the register do". Every one of those is a
#: read-only question the loop sent to the Coder.
#:
#: What separates the verb from the noun is position, not spelling. An
#: imperative opens a clause; a noun sits behind a determiner. So a work word
#: only counts where a *command* could begin: the start of the message, the
#: start of a sentence or line, a bullet, or a word that hands off to a fresh
#: instruction ("and", "then", "please", "can you", "i want you to").
#: A determiner in front of the word is enough to disqualify it, which is
#: exactly the discrimination the old regex had no way to make.
_COMMAND_LEAD = (
    r"(?:^[ \t]*(?:[-*•][ \t]+|\d+[.)][ \t]+)?"
    r"|[.;:!?,\n][ \t]*"
    r"|\b(?:and|then|also|plus|afterwards?|additionally|please|kindly)\b[ \t,]*"
    r"|\b(?:can|could|would|will) you\b[ \t,]*(?:please[ \t,]*)?"
    r"|\bi (?:want|need|would like|'d like)(?: you)? to\b[ \t,]*"
    r"|\byou (?:should|must|need to|have to)\b[ \t,]*"
    r"|\blet'?s\b[ \t,]*"
    r"|\bhelp me\b[ \t,]*"
    r"|\bgo ahead and\b[ \t,]*"
    r"|\bmake sure (?:you |to )?\b[ \t,]*"
    r")"
)

#: The verbs that ask for the code to change.
_WORK_VERB = (
    r"(?:add|create|implement|write|edit|update|modify|change|refactor|rename|"
    r"delete|remove|insert|append|register|wire ?up|wire|scaffold|generate|"
    r"build|fix|migrate|convert|port|move|split|extend|introduce|replace|"
    r"rewrite|patch|hook ?up|set ?up|clean ?up|finish|complete|apply|expose|"
    r"define|declare|stub|mock|bump|upgrade|drop|switch|swap|adjust|tweak|"
    r"correct|repair|resolve|address|make|do)"
)

#: What an imperative is followed by, and the second half of the noun test.
#:
#: An imperative takes an object, and in English that object almost always
#: opens with a determiner, a pronoun, a preposition or a path: "add *a*
#: struct", "migrate *it*", "fix *the* vet failure", "refactor *loop.py*",
#: "wire *up* the handler". A compound noun does not: "the create and update
#: *handlers*", "build *pipeline* is broken", "the register *do*". Requiring
#: the object is what keeps "describe the create and update handlers" a
#: question -- ``and update`` sits at a command lead, and only the word after
#: it says which of the two things it is.
_WORK_OBJECT = (
    r"(?:[ \t]+(?:a|an|the|this|that|these|those|it|them|its|his|her|their|"
    r"our|my|your|all|both|each|every|everything|anything|new|another|more|"
    r"one|two|three|up|out|in|into|over|down|on|off|to|for|from|by|with|"
    r"back|again|here|there)\b"
    r"|[ \t]+[`'\"]?[\w-]*[./][\w./-]*)"
)

#: A task that asks for the code to change. Its presence settles the question on
#: its own: "explain the bootstrapper, then migrate it" is work with a preamble.
_ASKS_FOR_WORK = re.compile(
    _COMMAND_LEAD + r"(?:just |also |now |please |quickly |finally )?"
    + _WORK_VERB + _WORK_OBJECT,
    re.IGNORECASE | re.MULTILINE,
)

#: The follow-up that turns an answer into work.
#:
#: A read-only run now ends with its answer on screen, and the developer's
#: reply to that is one word. It arrives as a pinned directive on the same
#: transcript while ``task_text`` still holds "explain the bootstrapper" -- so
#: without this, "go" is judged against the original question and answered
#: again, forever. Matched only when the *whole* message is a go-ahead:
#: "continue reading the docs and explain" is not one.
_GO_AHEAD = (
    r"(?:ok(?:ay)?|k|yes|yep|yeah|yup|sure|right|good|great|perfect|fine|"
    r"go|go on|go ahead|go for it|do it|do that|do so|proceed|continue|"
    r"carry on|get to it|have at it|let'?s go|make it so|ship it|apply it|"
    r"apply them|please do|start|begin|run it|execute|implement it|write it|"
    r"build it)"
)
_SAYS_GO = re.compile(
    r"^[\s\W]*" + _GO_AHEAD + r"(?:[\s\W]+" + _GO_AHEAD + r")*[\s\W]*$",
    re.IGNORECASE,
)


def _asks_for_work(task: str, directives: Sequence[str] = ()) -> bool:
    """Whether anything the developer has said asks for the code to change.

    The directives matter as much as the task. ``task_text`` is pinned once and
    never replaced -- a follow-up is appended, not re-pinned, because the
    original task is what the conversation is about. So on the turn after an
    answer, the task still reads "explain the bootstrapper" and the only thing
    that says otherwise is the directive that says "go".
    """
    if _ASKS_FOR_WORK.search(task):
        return True
    return any(_SAYS_GO.match(d) or _ASKS_FOR_WORK.search(d) for d in directives)


def _is_read_only_task(task: str, directives: Sequence[str] = ()) -> bool:
    """Whether the developer asked to be told something and not for a change.

    Read-only in the strong sense: whatever the Planner comes back with, this
    run answers and stops. Nothing here is a guess about the reply -- the
    question is what was asked, and the answer to that does not change because
    the model chose to phrase its reply in the imperative.
    """
    return not _asks_for_work(task, directives) and bool(_ASKS_TO_BE_TOLD.search(task))


def _is_explanation(task: str, text: str, directives: Sequence[str] = ()) -> bool:
    """Whether a numbered Planner reply describes something rather than proposing work.

    ``_count_steps`` counts anything shaped like a numbered item, which is the
    right thing for recognising a plan and the wrong thing for telling one from
    an answer. Asked to *explain* a bootstrapper and say how it deviates from the
    template, the Planner produced exactly what was wanted — ten numbered
    paragraphs, ``**1. `Fxvalidator`** — `fx.Invoke(...)`. Runs once at
    startup`` — and the loop read ten steps, pinned the explanation as the plan
    and entered the Coder.

    What the Coder does there is the part that matters. In one transcript it had
    nothing to execute and re-ran the Planner's whole survey, six turns of
    ledger answers to no progress. In another it went looking for work, found a
    ``json\\t:`` typo in a domain model nobody had mentioned, and started editing
    a file the developer had asked only to have explained. The second is the
    worse outcome and the harder one to notice.

    Both halves have to agree, because neither is sufficient alone.

    *The task asked only to be told something.* A request that also asks for a
    change is work with a preamble, and ``_ASKS_FOR_WORK`` settles that on its
    own — "explain the bootstrapper, then migrate it" is a migration.

    *The reply proposes nothing executable.* No step that asks for a change, no
    ``Accepts:`` line — which the Planner's own instruction requires of every
    step — and not the scaffolder's one-liner, which is a plan carrying neither.

    The reply test alone was tried first and is too strong: ``1. Read
    handler/user.go`` is a legitimate one-step plan that reads exactly like a
    description, and ten tests drive the Coder through plans of that shape. What
    tells the two apart is not the answer but the question.

    A false positive costs a run that ends with its answer on screen, which the
    developer continues with "go"; the behaviour without it costs unrequested
    edits to files nobody mentioned. That asymmetry is why this errs toward
    executing, and why both halves must agree before it does not.
    """
    if not _ASKS_TO_BE_TOLD.search(task) or _ASKS_FOR_WORK.search(task):
        return False
    return not (
        _PLAN_EDITS.search(text) or _ACCEPTS.search(text) or _is_scaffold_plan(text)
    )


def _is_scaffold_plan(plan: str) -> bool:
    """Whether the plan is the 10-step resource recipe.

    Keyed off the tool the plan names rather than off prose. A plan that says
    "create a new resource" in English but never mentions the scaffolder is a
    plan to write seven files by hand, and routing it to the Scaffolder would
    hand it a mode whose only tools are the ones it did not ask for.
    """
    lowered = plan.lower()
    return "resource_scaffold" in lowered or "project_scaffold" in lowered
