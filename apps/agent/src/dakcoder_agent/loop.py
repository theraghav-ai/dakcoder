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
    #: Fingerprints of the last few tool calls, for the no-progress detector.
    recent: list[str] = field(default_factory=list)
    #: The last few tool-call-free replies, for the same detector. A separate
    #: ledger on purpose: `_switch` clears `recent` on every mode change, and a
    #: run that repeats itself does so *while* advancing modes — planner, coder,
    #: verifier, debugger, coder — so a shared ledger could never reach three.
    said: list[str] = field(default_factory=list)
    #: How many times each ``(mode, reply)`` has been seen. ``said`` catches
    #: three identical replies in a row; this catches the same reply coming back
    #: every time the loop re-enters a mode, which is what a two-mode ping-pong
    #: looks like and what ``said`` is structurally unable to see — with Coder
    #: and Verifier alternating, ``said`` holds ``[coder, verifier, coder]`` and
    #: never reaches three of anything.
    echoes: dict[tuple[str, str], int] = field(default_factory=dict)
    #: Fingerprint counts for the whole run rather than the current mode, reset
    #: whenever a mutation lands. ``recent`` is cleared by ``_switch``, so a call
    #: repeated once per trip round a mode cycle is invisible to it.
    seen_calls: dict[str, int] = field(default_factory=dict)
    #: ``router.mutations`` as of the last call, so ``seen_calls`` can be cleared
    #: when something actually changed. Repeating a call after an edit is
    #: re-checking work; repeating it after nothing is a loop.
    mutations_seen: int = 0
    #: What each fingerprinted call last returned, so a repeat can be answered
    #: with the result rather than by running it again. Bounded by the number of
    #: distinct calls in a run, which is small.
    last_failure: dict[str, str] = field(default_factory=dict)
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
    #: How many times each path has been read, and the ranges it was read at.
    #: The slice ledger keeps the *context* from growing when a file is read
    #: seven times; it does nothing about the seven turns. Reset for a path when
    #: that path is written, because re-reading what you just changed is the
    #: correct move. See ``_re_reading``.
    reads: dict[str, list[str]] = field(default_factory=dict)
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

#: The modes that can write. ``_narrating`` counts idle turns only in these:
#: the Planner and the Verifier are supposed to end with prose and no tool call,
#: because that is how they hand on.
_EXECUTING = frozenset({Mode.CODER, Mode.SCAFFOLDER, Mode.DEBUGGER})

#: How many times one path may be read before the loop answers with what it
#: already has. Three is enough for read, edit, re-read; the seventh read of one
#: file in eight turns is the Planner going in circles, which is what it did.
MAX_READS = 3


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
                f"stopped after {self.context.turn} turns without a clean gate",
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

        if result.chat.content:
            self.context.append_assistant(result.chat.content)
            yield Event(EventType.ASSISTANT, {"text": result.chat.content})

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
            self.context.append_tool_result(
                incomplete[0].name,
                f"Your call to {names} arrived cut off — the arguments stop partway "
                "through, so the call was not made. Nothing is wrong with your JSON; "
                "this is what running into the "
                f"{config_for(self.state.mode).max_tokens:,}-token output limit looks like.\n\n"
                "Make the next reply shorter: fewer tool calls in one turn, and less "
                "prose before them. One call is enough.",
                tool_call_id=incomplete[0].id,
            )
            yield Event(
                EventType.TOOL_RESULT,
                {
                    "id": incomplete[0].id,
                    "name": incomplete[0].name,
                    "ok": False,
                    "content": f"output limit reached mid-call; {names} was not dispatched",
                },
            )
            return

        if result.chat.tool_calls:
            yield from self._tool_calls(result.chat.tool_calls)
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

    def _tool_calls(self, calls: Sequence[ToolCall]) -> Iterator[Event]:
        mutated = False

        for call in calls:
            if self.cancelled():
                # Before the call, not after. A batch can hold five writes, and
                # "it stopped but three more files changed" is the report this
                # check exists to prevent.
                self.result = self._abort()
                return

            # Fingerprinted from the raw string, not the parsed object. Parsing
            # can raise on malformed arguments, and a model that sends the same
            # malformed arguments three turns running is precisely the case the
            # no-progress detector is for — so it must not be the case that
            # crashes it.
            fingerprint = f"{call.name}:{call.arguments}"
            if self._stuck(fingerprint):
                self.result = RunResult(
                    Outcome.NO_PROGRESS,
                    f"{call.name} called with identical arguments "
                    f"{NO_PROGRESS_REPEATS} turns running",
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                return

            # One warning before the kill.
            #
            # The detector fires on the third identical call and ends the run,
            # which throws away every turn spent so far and hands the developer
            # nothing. On the second, the model has repeated itself once and is
            # still able to act — but only if something tells it so, and until
            # now nothing did: the tool ran again and returned the same result,
            # which is precisely the input that produced the repeat.
            #
            # So the second identical call is intercepted instead of dispatched.
            # The model is told what it did, what came back, and that the same
            # call will not be run again. That converts a fatal loop into a
            # recoverable turn, and it costs one tool call rather than a run.
            if self._repeated_once(fingerprint):
                previous = self.state.last_failure.get(fingerprint, "the same result")
                self.context.append_tool_result(
                    call.name,
                    f"Not run: you have already called {call.name} with exactly these "
                    f"arguments this turn and last, and it returned:\n\n{previous}\n\n"
                    "Repeating it will return the same thing. Do something different — "
                    "a different tool, different arguments, or say what is blocking you.",
                    tool_call_id=call.id,
                )
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": False,
                        "content": f"{call.name} repeated with identical arguments; not re-run",
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
                    f"Not run: you have already read this file {len(ranges)} times this "
                    f"run, at {', '.join(ranges)}. The most recent read is still in "
                    "context above — older ones were collapsed because they were "
                    "superseded, not because they were lost.\n\n"
                    "Re-reading it again will not show you anything new. Either act on "
                    "what you have, or say plainly what you are looking for and cannot "
                    "find.",
                    tool_call_id=call.id,
                )
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": False,
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
            mutated = mutated or bool(outcome.mutations)
            if call.name == "go_mod":
                self.state.dependencies_changed = True

            # Kept so a repeat of this exact call can be answered with what it
            # returned, instead of running it a second time to find out.
            self.state.last_failure[fingerprint] = outcome.for_model()[:600]

            # A file that was just written is worth reading again; the ledger of
            # how often it has been read starts over. Without this, the read
            # limit below would eventually refuse the one re-read that matters.
            for mutation in outcome.mutations:
                self.state.reads.pop(mutation.path, None)

            self.context.append_tool_result(
                call.name,
                outcome.for_model(),
                tool_call_id=call.id,
                path=_slice_path(call, outcome),
            )
            yield Event(
                EventType.TOOL_RESULT,
                {"id": call.id, "name": call.name, **outcome.as_dict()},
            )

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
            if steps == 0 and not _is_scaffold_plan(text):
                # A reply with no steps is not a plan, and the Planner is the one
                # mode positioned to say so: it has read the task and answered it.
                # Greetings, typos and questions land here, and so does a
                # clarifying question the developer has to answer before there is
                # anything to plan. Handing that text to the Coder as if it were a
                # plan is what turned "he how are you doijng" into seventeen turns
                # — the Coder had no step to execute, so every mode below it fired
                # in order against a workspace nothing had touched.
                self.result = RunResult(
                    Outcome.DONE,
                    "answered; no plan was needed and nothing was changed",
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
            blocker = report.blocked_by.name if report.blocked_by else "the gate"
            yield Event(EventType.GATE, {"kind": "full", "cached": True, **report.as_dict()})
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
        )
        self.state.last_gate = report
        self.state.gate_key = key
        yield Event(EventType.GATE, {"kind": "full", **report.as_dict()})

        if report.ok:
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

    def _route_failure(self, verdict: str) -> Iterator[Event]:
        """Coder twice, then the Debugger, then stop.

        The escalation matters more than the numbers. A third identical Coder
        attempt on a gate that has failed twice is not more likely to work — the
        model is applying the same understanding to the same evidence. The
        Debugger has different instructions and a playbook, which is a different
        understanding rather than another try.
        """
        del verdict  # already in context; the routing decision is ours

        if self.state.attempts < MAX_ATTEMPTS:
            self.state.attempts += 1
            self._switch(Mode.CODER)
            return

        if self.state.cycles < MAX_DEBUG_CYCLES:
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
        self.state.recent.clear()
        self.context.switch_mode(mode, mode_instruction(mode))

    # -- helpers ----------------------------------------------------------

    def _stuck(self, fingerprint: str) -> bool:
        """Whether this exact call has stopped being an attempt at anything.

        Two ledgers again, for the same reason ``_repeating`` needs two.

        ``recent`` is the consecutive run within one mode, and ``_switch``
        clears it — which is right for what it measures and blind to a call
        made once per trip round a mode cycle. ``seen_calls`` counts the whole
        run, and is cleared the moment a mutation lands: repeating a call after
        an edit is re-checking work, and repeating it after nothing changed is
        a loop.
        """
        if self.router.mutations != self.state.mutations_seen:
            self.state.mutations_seen = self.router.mutations
            self.state.seen_calls.clear()

        self.state.recent.append(fingerprint)
        del self.state.recent[:-NO_PROGRESS_REPEATS]
        self.state.seen_calls[fingerprint] = self.state.seen_calls.get(fingerprint, 0) + 1

        consecutive = (
            len(self.state.recent) == NO_PROGRESS_REPEATS
            and len(set(self.state.recent)) == 1
        )
        return consecutive or self.state.seen_calls[fingerprint] >= NO_PROGRESS_REPEATS

    def _repeated_once(self, fingerprint: str) -> bool:
        """Whether this exact call was also the previous one.

        Read off the same ledger ``_stuck`` maintains, one repeat earlier. Kept
        as its own predicate rather than a parameter on ``_stuck`` because the
        two answer different questions — "is this run over" and "does this turn
        need an intervention" — and a single function returning a tri-state
        would make both call sites harder to read than either is now.
        """
        tail = self.state.recent[-2:]
        return len(tail) == 2 and tail[0] == tail[1]

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
        if not self.state.plan:
            return ""
        named = {m.group(0) for m in _PLAN_PATH.finditer(self.state.plan)}
        missing = sorted(named - set(self.router.touched))
        if not missing or len(missing) == len(named):
            # All of them missing means the plan named files by some convention
            # this does not read, and reporting every path as unwritten would be
            # noise dressed as a finding.
            return ""
        return (
            ". The plan named files this run never wrote: " + ", ".join(missing)
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


def _slice_path(call: ToolCall, result: ToolResult) -> str | None:
    """The file a tool result is *about*, for the slice ledger.

    Only ``read_file`` supersedes: re-reading a file replaces the older slice
    rather than stacking beside it. A mutation is not a supersede — the record
    that a file was written is not made obsolete by writing it again.
    """
    if call.name != "read_file" or not result.ok:
        return None
    parsed = _safe_args(call)
    path = parsed.get("path") if isinstance(parsed, dict) else None
    return path if isinstance(path, str) else None


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

#: A step in a real plan carries one of these. The Planner is told so.
_ACCEPTS = re.compile(r"^\s*[-*>\s]*\**\s*Accepts\s*:", re.MULTILINE | re.IGNORECASE)


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


def _is_scaffold_plan(plan: str) -> bool:
    """Whether the plan is the 10-step resource recipe.

    Keyed off the tool the plan names rather than off prose. A plan that says
    "create a new resource" in English but never mentions the scaffolder is a
    plan to write seven files by hand, and routing it to the Scaffolder would
    hand it a mode whose only tools are the ones it did not ask for.
    """
    lowered = plan.lower()
    return "resource_scaffold" in lowered or "project_scaffold" in lowered
