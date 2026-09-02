"""The agent loop.

One loop, one system prompt, three mode overlays, and transitions that come from
typed events rather than from the shape of prose.

This is a rewrite, and the shape of what it replaces is the argument for it. The
old loop was 2,749 lines, 860 of them comments explaining a past incident. Its
``_State`` had 34 fields, almost all counters with their own watermark. It
decided what the developer had asked for by running about 500 lines of regex
over the reply *after* the model had already answered, appended 17 kinds of
fabricated ``role: tool`` message attributed to tools that never ran, deleted
earlier messages out of its own transcript, and ended runs on a text detector
that counted how many times the model had said something idle. Its terminal
conditions were counts of things the model said; its stop condition for a stuck
run was a regex.

Five properties are load-bearing here, and each replaces a failure the report
documents.

**Intent is decided before the first turn, not after.** The panel's Ask/Agent
toggle answers it, or one cheap schema-constrained call does. 17 of 24 realistic
read-only prompts used to be classified as work, and every one of them ran the
full gate on an untouched workspace and entered the escalation ladder.

**Transitions come from tool calls.** ``submit_plan`` and ``ask_developer`` end
the planning phase. Nothing reads prose to find out what happened.

**A tool call the mode requires is forced, not counted.** A mode that must call
a tool and did not is re-asked with ``tool_choice: "required"``. The old loop
counted three such turns and killed the run.

**The gate judges this run's work, or it does not run.** No mutations, no gate --
a run that wrote nothing cannot fail. Findings that were already there before the
run started are reported and do not block.

**Nothing is fabricated and nothing is deleted.** Every message the loop adds is
a real ``role: user`` message or a real tool result with a real ``tool_call_id``.
History is append-only.

**Approval is asked, not assumed.** The loop yields a ``tool_pending`` event and
consults a callback. The default denies, because a runtime that silently
auto-approves is one where the approval layer is decoration.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from dakcoder_shared.envelope import DeltaCoalescer, Event, EventType, ToolResult
from dakcoder_shared.llm import LLMClient, Metering, ToolCall
from dakcoder_shared.tokens import estimate_tokens

from .context import ContextManager, Message, OverBudgetError, Recap
from .gate import Baseline, GateReport, full_gate, inner_loop, take_baseline
from .llm import TurnResult, complete, reasoning_leaked
from .modes import Intent, Mode, config_for
from .prompts import mode_instruction, system_prompt
from .tools.control import PlanStep, steps_from_meta
from .tools.router import ApprovalRequest, Router

__all__ = [
    "AgentLoop",
    "Approver",
    "Intent",
    "Outcome",
    "RunResult",
    "deny_all",
    "system_prompt",
]

#: Asked before a mutating call the developer has not pre-approved. Returns True
#: to let it through. Blocking is the caller's choice -- the loop is a generator
#: and will simply wait.
Approver = Callable[[ApprovalRequest], bool]


def deny_all(_request: ApprovalRequest) -> bool:
    """The default. A runtime that silently approves is one with no approval layer."""
    return False


class Outcome:
    DONE = "done"
    #: The developer stopped it.
    ABORTED = "aborted"
    #: The gate never came clean, and the run ran out of ways to make it.
    UNVERIFIED = "unverified"
    #: The run stopped asking for anything it had not already been given.
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
    """Everything the loop tracks that is not in the context manager.

    Twenty fields, where there were thirty-four -- and the count matters less
    than what went. What is gone, and why:

    ``attempts``, ``cycles``, ``blocked_stage``, ``route_mutations`` -- the
    escalation ladder. Coder twice, then Debugger three times, then stop. It
    spent its budget on Verifier turns that had attempted nothing, and its whole
    purpose was to get a second model persona to look at a gate failure that, on
    the legacy corpus, no persona could have fixed.

    ``said``, ``echoes`` -- text detectors that ended runs. "The same reply three
    turns running" is a symptom; the loop now ends runs on decisions about the
    work.

    ``idle``, ``idle_mutations``, ``planner_idle``, ``planner_research``,
    ``planner_nudged``, ``executing_research``, ``executing_nudged``,
    ``research_mutations``, ``unfinished_nudges`` -- nine counters, each with its
    own watermark, each existing because a mode had no stopping condition. A
    mode that must call a tool is now made to; a mode that must not is over when
    it stops.

    ``scaffolded`` -- the Scaffolder's hand-off. There is no Scaffolder.

    ``dup_results``, ``intercepts`` -- ledgers of messages to delete from the
    transcript later. History is append-only.
    """

    mode: Mode = Mode.ASK
    #: What the developer asked for. Fixed before the first turn.
    intent: Intent = Intent.AUTO
    #: The plan, as ``submit_plan`` typed it. Empty in ASK.
    plan: tuple[PlanStep, ...] = ()
    plan_summary: str = ""
    #: How many times each exact call has been asked this run. Cleared when a
    #: mutation lands, because repeating a call after an edit is re-checking
    #: work rather than looping.
    seen_calls: dict[str, int] = field(default_factory=dict)
    #: ``router.mutations`` as of the last call, so the ledgers can be cleared
    #: when something actually changed.
    mutations_seen: int = 0
    #: What each fingerprinted call last returned, so a repeat is answered with
    #: the result rather than run again.
    last_results: dict[str, str] = field(default_factory=dict)
    #: Calls the tools themselves declared can never succeed as asked.
    #: fingerprint -> the tool's one-line reason.
    dead_ends: dict[str, str] = field(default_factory=dict)
    #: Fingerprint -> the ``max``/``limit`` that produced a *truncated* answer,
    #: so raising the cap genuinely asks for something the ledger does not hold.
    truncated_at: dict[str, int] = field(default_factory=dict)
    #: Consecutive tool-calling turns that added nothing new -- every call in
    #: them answered from a ledger rather than dispatched.
    stalled_turns: int = 0
    #: How many times each path has been read, and at what ranges.
    reads: dict[str, list[str]] = field(default_factory=dict)
    #: What each ``search_docs`` query returned, as section citations.
    retrievals: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    retrieval_repeats: int = 0
    #: The turn each compaction fired on, for the thrash detector.
    compactions: list[int] = field(default_factory=list)
    #: What was already broken when the run started. See ``_take_baseline``.
    baseline: Baseline = field(default_factory=Baseline)
    last_gate: GateReport | None = None
    #: The workspace state the last full gate ran against.
    gate_key: tuple[int, tuple[str, ...]] | None = None
    #: Failing gates in a row with no new edit between them.
    gate_failures: int = 0
    dependencies_changed: bool = False
    #: Whether this turn has already been re-asked with ``tool_choice:
    #: required``. One force per turn: the second refusal is information, not a
    #: reason to keep asking.
    forced: bool = False


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

#: The JSON schema the recap is asked for. Structured output, rather than
#: "reply with JSON only" and a tolerant parser hoping for the best.
_RECAP_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "recap",
        "schema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "plan_step": {"type": "string"},
                "files_created": {"type": "array", "items": {"type": "string"}},
                "files_modified": {"type": "array", "items": {"type": "string"}},
                "decisions": {"type": "array", "items": {"type": "string"}},
                "verified": {"type": "array", "items": {"type": "string"}},
                "open_items": {"type": "array", "items": {"type": "string"}},
                "do_not_retry": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["goal"],
        },
    },
}

#: What the intent classifier is asked, and the shape it must answer in.
#:
#: This is the whole of Track A item 2. Two words of output, one small call,
#: made once before any tool is offered -- against ~500 lines of regex that ran
#: after the model had already answered and got 17 of 24 read-only prompts
#: wrong. Anthropic's own guidance is that a routing step is worth having "where
#: classification can be handled accurately", and then via a cheap
#: structured-output call; that is exactly this.
_INTENT_PROMPT = """Decide what this developer wants from a Go backend agent.

"question" -- they want to be told something about the code: an explanation, a
review, a list, an opinion, a yes/no. Answering it changes no files.

"change" -- they want the code changed: a feature, a fix, a migration, a
refactor, a scaffold. Also "change" when they are approving work that was just
described to them ("go", "do it", "yes please"), or when they ask a question and
then ask for the work as well ("explain the handler, then migrate it").

Answer with the JSON object only.

CONVERSATION SO FAR:
{conversation}

LATEST MESSAGE FROM THE DEVELOPER:
{task}
"""

_INTENT_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "intent",
        "schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["question", "change"]},
                "why": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
}


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a reply, tolerating fences and stray prose.

    Still tolerant even though ``response_format`` is now sent: the endpoint is
    behind LiteLLM behind vLLM, guided decoding can be off, and a compaction is
    expensive enough that it should not be spent twice over a markdown fence.
    """
    if not text.strip():
        return None
    import re

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


#: How many consecutive tool-calling turns may add nothing new -- every call in
#: them a verbatim repeat or a known dead end, answered from a ledger rather
#: than dispatched -- before the run is ended as making no progress.
#:
#: This is the one text-free stop condition worth keeping: it counts *turns in
#: which the model asked for nothing it had not already been given*, which is a
#: fact about the work rather than about the phrasing.
MAX_STALLED_TURNS = 6

#: How many failing gates in a row, with nothing edited between them, end the
#: run.
#:
#: This replaces the escalation ladder entirely -- two Coder attempts, three
#: Debugger cycles, `attempts`, `cycles`, `blocked_stage`, `route_mutations`,
#: and the mode switching that produced fourteen switches in fifteen turns. A
#: gate is a function of the files and the toolchain, so a gate that failed and
#: has not been given a new edit will fail identically; sending the same input
#: to a differently-named persona and asking for a different answer is not a
#: strategy. Three, because the first is the model reading the report, the
#: second is it being asked to act with the call forced, and a third adds
#: nothing.
MAX_GATE_FAILURES = 3

#: How many times one path may be read before the loop answers with what it
#: already has instead of dispatching the read again.
MAX_READS = 10

#: How many retrievals in a row may return nothing new before ``search_docs`` is
#: withdrawn for the rest of the run. The corpus does not acquire new sections
#: mid-run, so nothing that follows could change the answer.
MAX_RETRIEVAL_REPEATS = 3

#: How much of a retrieval must be old for it to count as adding nothing.
RETRIEVAL_OVERLAP = 0.5

#: The compaction-thrash window, in turns, and how many compactions inside it
#: mean the run is evicting rather than working.
COMPACTION_WINDOW = 8
MAX_CLOSE_COMPACTIONS = 3

#: The tools that end the planning phase. Handled by name because the loop has
#: to know what happened, not because the router treats them specially.
_PLAN_TOOLS = frozenset({"submit_plan", "ask_developer"})


class AgentLoop:
    """One run, from a message to an answer or a verified change."""

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
        #: can possibly see that id.
        self.on_pending = on_pending
        #: Where transient events go -- the ones that cannot travel by ``yield``.
        #: The model streams its answer while this thread is blocked inside the
        #: completion, and a callback firing there cannot yield.
        self.on_event = on_event
        #: Cleared to False the first time the sink raises. Streaming is a view
        #: of a turn, never the turn itself.
        self._relaying = True
        #: Drained at the top of every turn, so a correction typed at turn 12
        #: arrives before turn 13.
        self.steer = steer
        self.cancelled = cancelled
        #: Checked only *between* turns, so a turn already running is allowed to
        #: finish and leave the workspace coherent.
        self.winding_down = winding_down
        self.max_turns = max_turns
        self.session_id = session_id
        self.state = _State()
        self.result: RunResult | None = None
        #: The background baseline. See ``_take_baseline``.
        self._baseline_thread: threading.Thread | None = None

    # -- the run ----------------------------------------------------------

    def run(
        self,
        task: str,
        *,
        acceptance: Sequence[str] = (),
        intent: Intent | str = Intent.AUTO,
        continued: bool = False,
        start: Mode | str | None = None,
    ) -> Iterator[Event]:
        """Drive the run, yielding events as they happen.

        ``continued`` is a follow-up on a context that already holds an
        exchange. The new message is appended to the working set rather than
        re-pinned as the task: the pinned task layer sits above the working set,
        so re-pinning would put the newest message before the answers to the
        older ones, and the original task is what the conversation is about.

        ``start`` is accepted for callers that still name a mode. It is a
        statement of intent, not a mode any more -- a caller asking for "coder"
        is asking for work to be done, which is what ``Intent.AGENT`` means.
        """
        if continued:
            self.context.append_user(task)
            # And pinned, because the working-set copy is the first thing the
            # next compaction evicts.
            self.context.pin_directive(task)
        else:
            self.context.set_task(task, acceptance=acceptance)

        decided = Intent.coerce(intent)
        if decided is Intent.AUTO and start is not None:
            decided = Intent.coerce(start)
        if decided is Intent.AUTO:
            decided = self._classify(task, continued=continued)
        self.state.intent = decided

        self._switch(Mode.PLANNER if decided is Intent.AGENT else Mode.ASK)
        # Only a run that may write needs to know what was already broken.
        if decided is Intent.AGENT:
            self._take_baseline()

        for _ in range(self.max_turns):
            if self.result is not None:
                break
            if self.cancelled():
                self.result = self._abort()
                break
            yield from self._turn()
            if self.result is None and self.winding_down():
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
                f"stopped after {self.context.turn} turns without finishing. "
                "Nothing is lost: the edits are in the workspace and the session "
                "is resumable -- Resume continues on this same transcript with a "
                "fresh turn budget. For a task this size, raise dakcoder.maxTurns",
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )

        yield Event(EventType.FINISH, self.result.as_dict())
        yield Event(EventType.END, self.result.as_dict())

    # -- intent -----------------------------------------------------------

    def _classify(self, task: str, *, continued: bool) -> Intent:
        """Ask the model, once, what kind of request this is.

        One call, ``role="fast"``, a two-key schema and a handful of output
        tokens. The conversation so far is included because a follow-up cannot be
        classified without it: "go" is a question about nothing and an
        instruction about whatever was just described.

        **Falls back to ASK.** The asymmetry is what makes that the right
        default, and it is the one piece of reasoning worth keeping from the
        regex era: a wrong "question" costs the developer one word -- the answer
        is on screen and their next message starts the work -- while a wrong
        "change" costs unrequested edits to files nobody mentioned, found later
        in a diff. So an unavailable or unparseable classifier answers with the
        cheap mistake.
        """
        conversation = "\n".join(
            f"- {line}" for line in self.context.directives[-4:]
        ) or "(this is the first message)"
        if continued and self.context.task_text:
            conversation = f"- {self.context.task_text}\n{conversation}"

        try:
            reply = self.client.chat(
                [
                    {
                        "role": "user",
                        "content": _INTENT_PROMPT.format(
                            conversation=conversation, task=task.strip()
                        ),
                    }
                ],
                role="fast",
                max_tokens=64,
                enable_thinking=False,
                response_format=_INTENT_SCHEMA,
                metering=Metering(
                    session_id=self.session_id,
                    turn=0,
                    mode="classifier",
                    estimated_tokens=estimate_tokens(task) + 200,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - a classifier is not a precondition
            self.on_event(
                Event(
                    EventType.ERROR,
                    {
                        "where": "classifier",
                        "message": f"the intent could not be classified: {exc}",
                        "effect": "answering read-only; say what you want changed "
                        "and it will be done",
                    },
                )
            )
            return Intent.ASK

        parsed = _parse_json_object(reply.content or "")
        kind = str((parsed or {}).get("kind", "")).strip().lower()
        return Intent.AGENT if kind == "change" else Intent.ASK

    # -- the baseline -----------------------------------------------------

    def _take_baseline(self) -> None:
        """Record what was already broken, off the critical path.

        Correctness depends entirely on the timing: taken later, the snapshot
        contains the run's own damage and excuses it. So it is taken now, when
        the workspace is definitely untouched.

        On a background thread, because it is not cheap -- ``go vet`` alone is
        about thirty seconds -- and it is not needed until the first gate, which
        is many turns away. The model reads and plans while this runs. By the
        time ``_verify`` asks for it, it is almost always there; if it is not,
        ``_verify`` waits, and waiting is still cheaper than the alternative,
        which is charging this run for damage it did not do.
        """

        def measure() -> None:
            try:
                self.state.baseline = take_baseline(self.router)
            except Exception as exc:  # noqa: BLE001
                # Announced rather than swallowed. Without a baseline the gate
                # reverts to blaming the run for what it found, which is the
                # behaviour that shipped -- so this is a degradation worth
                # seeing in the transcript rather than inferring later.
                self._relay(
                    Event(
                        EventType.ERROR,
                        {
                            "where": "baseline",
                            "message": f"the pre-run baseline could not be taken: {exc}",
                            "effect": "pre-existing failures may be reported as this "
                            "run's",
                        },
                    )
                )

        thread = threading.Thread(target=measure, name="dakcoder-baseline", daemon=True)
        self._baseline_thread = thread
        thread.start()

    def _await_baseline(self) -> None:
        """Block until the baseline is in, if it is not already."""
        thread, self._baseline_thread = self._baseline_thread, None
        if thread is not None:
            thread.join(timeout=180)

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
        self.state.forced = False
        yield Event(
            EventType.TURN_START,
            {
                "turn": turn,
                "mode": str(self.state.mode),
                # Carried on every turn so the panel can say why it is in the
                # mode it is in. This is the decision the whole run turns on and
                # nothing on the wire used to name it.
                "intent": str(self.state.intent),
                "attempt": self.state.gate_failures,
            },
        )

        tools = self._tools()
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

        outcome = yield from self._complete(tools)
        if outcome is None:
            return
        result = outcome

        # A mode that must end with a tool call and did not is re-asked with the
        # call made mandatory.
        #
        # This is the fix the old loop never attempted. A 27B model at
        # temperature 0.1 narrates "Making the edit now" with no tool call; the
        # old loop counted three of those and ended the run `no_progress`, and
        # nine such turns appear in one 38-turn transcript. vLLM supports
        # `tool_choice: "required"`; asking again with it costs one call and
        # deletes `_narrating`, `MAX_IDLE_EXECUTING`, `EXECUTING_RESEARCH_*` and
        # `PLANNER_RESEARCH_*` between them.
        if not result.chat.tool_calls and tools and self._must_call_a_tool():
            self.state.forced = True
            yield Event(
                EventType.GATE,
                {"kind": "forced_tool_call", "mode": str(self.state.mode)},
            )
            forced = yield from self._complete(tools, tool_choice="required")
            if forced is None:
                return
            if forced.chat.tool_calls:
                result = forced

        yield from self._usage(result)

        if result.chat.content:
            yield Event(EventType.ASSISTANT, {"text": result.chat.content})

        # The assistant's own turn goes into context before anything it caused,
        # and its calls travel with it. Recording only the prose leaves every
        # tool result that follows referring to a `tool_call_id` no message on
        # the wire declares -- malformed against a strict endpoint, and worse as
        # a prompt: the model's visible history of itself becomes paragraphs of
        # narration with results appearing beside them unexplained.
        assistant_msg: Message | None = None
        if result.chat.content or result.chat.tool_calls:
            assistant_msg = self.context.append_assistant(
                result.chat.content or "",
                tool_calls=tuple(result.chat.tool_calls),
            )

        if incomplete := result.chat.incomplete_tool_calls():
            yield from self._answer_truncated(result, incomplete)
            return

        if result.chat.tool_calls:
            yield from self._tool_calls(result.chat.tool_calls, assistant_msg)
            return

        # No tool calls: the model has said its piece.
        yield from self._finish_turn(result)

    def _complete(
        self, tools: list[dict[str, Any]], *, tool_choice: str | None = None
    ) -> Iterator[Event]:
        """Dispatch one completion, or set ``self.result`` and return None.

        A generator so it can yield the compaction and error events, and so the
        caller can `yield from` it and read the result off the return value.
        """
        # One coalescer per call, and the tail flushed however it ends.
        deltas = DeltaCoalescer()

        def dispatch() -> TurnResult:
            return complete(
                self.context,
                self.client,
                tools=tools,
                tool_choice=tool_choice,
                session_id=self.session_id,
                on_delta=lambda fragment: self._relay(deltas.feed(fragment)),
            )

        try:
            return dispatch()
        except OverBudgetError as exc:
            # The context manager exists to prevent this, so reaching it means
            # compaction could not free enough. Compacting harder and retrying
            # once is worth a turn; failing the run outright is not.
            yield Event(EventType.GATE, {"kind": "compaction", "reason": "over budget"})
            self.context.compact(self._summarise, retain_pct=0.15)
            try:
                return dispatch()
            except OverBudgetError:
                self.result = RunResult(
                    Outcome.ERROR,
                    f"context cannot be reduced below budget: {exc}",
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return None
        except Exception as exc:  # noqa: BLE001 - the transport failing is not the model's fault
            yield Event(EventType.ERROR, {"message": str(exc)})
            self.result = RunResult(
                Outcome.ERROR, str(exc), self.context.turn, tuple(self.router.touched)
            )
            return None
        finally:
            self._relay(deltas.flush())

    def _tools(self) -> list[dict[str, Any]]:
        """The tool schemas for this turn.

        Two withdrawals, both of them facts about the run rather than nudges.

        ``search_docs`` goes when three retrievals in a row have returned only
        sections the run already has. The corpus does not acquire new sections
        mid-run, so nothing that follows could change the answer -- and telling
        the model was measured not to be enough on its own: a model that has
        decided the answer is in the knowledge base keeps rewording the
        question.

        Nothing else is ever withdrawn. The old loop took the read tools away
        from a Planner at turn 16 and the lookup tools away from a Coder at
        turn 16, in both cases to force a decision the mode had no other way to
        reach; forcing the tool call does that directly and without lying to the
        model about what exists.
        """
        tools = self.router.schemas_for(self.state.mode)
        if self.state.retrieval_repeats >= MAX_RETRIEVAL_REPEATS:
            tools = [s for s in tools if s["function"]["name"] != "search_docs"]
        return tools

    def _must_call_a_tool(self) -> bool:
        """Whether this turn is one the mode cannot legitimately end with prose.

        The Planner ends with ``submit_plan`` or ``ask_developer``; a prose-only
        Planner turn is a turn whose tool call was never emitted.

        The acting mode ends with prose all the time -- that is how it says "I
        am done, run the gate" -- so it is forced only when the gate has already
        come back failing and nothing has been edited since. That is the exact
        shape of the narration the old loop killed runs over, and the only shape
        where prose is definitely not an answer.

        ASK is never forced. Prose *is* its deliverable.
        """
        if self.state.forced:
            return False
        if self.state.mode is Mode.PLANNER:
            return True
        if self.state.mode is Mode.AGENT:
            report = self.state.last_gate
            return (
                report is not None
                and not report.ok
                and self.state.gate_key is not None
                and self.state.gate_key[0] == self.router.mutations
            )
        return False

    def _answer_truncated(
        self, result: TurnResult, incomplete: Sequence[ToolCall]
    ) -> Iterator[Event]:
        """Answer every call in a reply the output budget cut off.

        The arguments are a valid-looking JSON prefix, so dispatching them
        produces "malformed arguments" and the model is told to send valid JSON.
        It did send valid JSON; it was interrupted. Acting on that advice means
        making the same oversized reply and being cut off in the same place.

        Every call the assistant message declared gets an answer, not just the
        cut-off one: a result whose ``tool_call_id`` no assistant message
        declares is malformed, and the poisoned message stays in the working set
        for the rest of the run *and the rest of the session*.
        """
        names = ", ".join(sorted({c.name for c in incomplete}))
        cut = {c.id for c in incomplete}
        for call in result.chat.tool_calls:
            if call.id in cut:
                body = (
                    f"Your call to {call.name} arrived cut off -- the arguments stop "
                    "partway through, so the call was not made. Nothing is wrong with "
                    "your JSON; this is what running into the "
                    f"{config_for(self.state.mode).max_tokens:,}-token output limit "
                    "looks like.\n\n"
                    "Make the next reply shorter: fewer tool calls in one turn, and "
                    "less prose before them. One call is enough."
                )
                said = f"output limit reached mid-call; {names} was not dispatched"
            else:
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

    def _usage(self, result: TurnResult) -> Iterator[Event]:
        usage = self.context.usage()
        payload = {
            "prompt_tokens": result.actual_prompt_tokens,
            "completion_tokens": result.chat.usage.completion_tokens,
            "cached_tokens": result.chat.usage.cached_tokens,
            # The absolute denominator, not just the percentage: two surfaces
            # dividing independently produce two different numbers on screen.
            "budget": usage.budget,
            "budget_used_pct": round(usage.used_pct, 1),
            "reasoning_tokens": result.chat.usage.reasoning_tokens,
            "estimate_error": result.estimate_error,
        }
        if reasoning_leaked(result):
            # Non-zero reasoning in a thinking-off mode means
            # chat_template_kwargs is not reaching the model: ~15x the latency
            # for no quality gain, presenting as the agent simply being slow.
            payload["reasoning_leaked"] = result.chat.usage.reasoning_tokens
        yield Event(EventType.USAGE, payload)

    # -- tools ------------------------------------------------------------

    def _tool_calls(
        self, calls: Sequence[ToolCall], assistant_msg: Message | None = None
    ) -> Iterator[Event]:
        del assistant_msg  # kept for signature stability; nothing is superseded

        # The world changed since the ledgers were written: forget them. A
        # mutation invalidates all of them at once -- a cached search may now be
        # wrong, a missing path may now exist, and a repeat is re-checking work
        # rather than looping.
        if self.router.mutations != self.state.mutations_seen:
            self.state.mutations_seen = self.router.mutations
            self.state.seen_calls.clear()
            self.state.last_results.clear()
            self.state.dead_ends.clear()
            self.state.truncated_at.clear()

        mutated = False
        #: Dispatched calls that told the run something it did not already have.
        informed = 0

        for index, call in enumerate(calls):
            if self.cancelled():
                # Before the call, not after. A batch can hold five writes, and
                # "it stopped but three more files changed" is the report this
                # check exists to prevent. The calls we abandon still get
                # answered: the assistant message declaring all of them is
                # already in the working set, and an aborted session is
                # resumable, so an orphan would be carried into the resume.
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
            args = _safe_args(call)

            if intercepted := self._intercept(call, fingerprint):
                body, said = intercepted
                self.context.append_tool_result(call.name, body, tool_call_id=call.id)
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": True,
                        "intercepted": True,
                        "arguments": args,
                        "content": said,
                    },
                )
                continue

            yield Event(
                EventType.TOOL_CALL,
                {"id": call.id, "name": call.name, "arguments": args},
            )

            outcome = self.router.dispatch(call.name, call.arguments, mode=self.state.mode)

            if isinstance(outcome, ApprovalRequest):
                request = outcome
                # Registered before it is announced, so a client that answers
                # the instant it reads the event cannot arrive before the
                # approval exists.
                self.on_pending(request)
                yield Event(EventType.TOOL_PENDING, request.as_dict())
                if self.approve(request):
                    # Re-dispatched with the *request's* arguments, not the
                    # model's original string: an approver may have corrected
                    # them, and using the original would apply the approval and
                    # discard the correction.
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

            # A tool refused because this mode does not hold it says nothing
            # about the call, only about who is asking. It is not progress and
            # it is never cached: the fingerprint carries no mode, so a refusal
            # earned in one mode would answer the identical call made in the
            # mode that *can* run it.
            refused_by_mode = bool(outcome.meta.get("refused_by_mode"))
            informed += 0 if refused_by_mode else 1
            mutated = mutated or bool(outcome.mutations)
            if call.name == "go_mod":
                self.state.dependencies_changed = True

            self.state.seen_calls[fingerprint] = self.state.seen_calls.get(fingerprint, 0) + 1
            if not refused_by_mode:
                self.state.last_results[fingerprint] = outcome.for_model()[:6000]
            if outcome.truncated:
                self.state.truncated_at[fingerprint] = _volume(call)
            else:
                self.state.truncated_at.pop(fingerprint, None)
            if reason := outcome.meta.get("dead_end"):
                self.state.dead_ends[fingerprint] = str(reason)

            # A file that was just written is worth reading again.
            for mutation in outcome.mutations:
                self.state.reads.pop(mutation.path, None)

            slice_path, slice_range = _slice_path(call, outcome)
            self.context.append_tool_result(
                call.name,
                outcome.for_model(),
                tool_call_id=call.id,
                path=slice_path,
                line_range=slice_range,
            )

            if note := self._retrieval_overlap(call, outcome):
                # As a user message. It carries no `tool_call_id` because no
                # tool produced it, and a `role: tool` message without one is
                # malformed on the wire and a lie in the transcript -- the old
                # loop had 17 such call sites, teaching the model that
                # `go_build` returns paragraphs of instructions.
                self.context.append_user(note)
                # A retrieval that returned only sections the run already had
                # did not inform this turn, so it must not count as progress.
                informed -= 1

            yield Event(
                EventType.TOOL_RESULT,
                {"id": call.id, "name": call.name, **outcome.as_dict()},
            )

            # The planning phase ends on its own tool call, not on its prose.
            if call.name in _PLAN_TOOLS and outcome.ok:
                yield from self._plan_submitted(call.name, outcome)
                return

        # Turn-level progress, judged on the batch rather than on any one call.
        # A batch that dispatched nothing -- every call a verbatim repeat or a
        # known dead end -- moved the run nowhere, however many calls it held.
        if informed > 0 or mutated:
            self.state.stalled_turns = 0
        else:
            self.state.stalled_turns += 1
            if self.state.stalled_turns >= MAX_STALLED_TURNS:
                worst_key, worst_n = max(
                    self.state.seen_calls.items(), key=lambda item: item[1], default=("", 0)
                )
                detail = (
                    f"; {worst_key.split(':', 1)[0]} was asked {worst_n} times"
                    if worst_n > 1
                    else ""
                )
                self.result = RunResult(
                    Outcome.NO_PROGRESS,
                    f"the last {MAX_STALLED_TURNS} tool-calling turns only repeated "
                    f"earlier calls or known dead ends, and added nothing new{detail}",
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                return

        if mutated:
            yield from self._inner_loop()

    def _intercept(self, call: ToolCall, fingerprint: str) -> tuple[str, str] | None:
        """What to answer without dispatching, or None to dispatch.

        Three ledgers, and none of them ends a run. A model being slow to take a
        hint costs a turn; it is not a reason to throw away twenty-five, which
        is what the old detector did on a third read of a file that was not
        there, one turn after being told correctly what to do instead.
        """
        # A known dead end. The tool itself declared this exact call unable to
        # succeed, so asking again cannot change the answer.
        if reason := self.state.dead_ends.get(fingerprint):
            self.state.seen_calls[fingerprint] = self.state.seen_calls.get(fingerprint, 0) + 1
            return (
                f"{call.name} with these arguments cannot succeed: {reason}. That was "
                "established earlier this run and nothing has changed since, so it was "
                "answered from the earlier result rather than run again.\n\n"
                "This is the answer, not a failure. Act on what does exist -- the "
                "alternatives named in the earlier result still stand.",
                f"{call.name}: known dead end; answered without re-running",
            )

        # An exact repeat while nothing has changed. An answer that stopped at
        # its cap has more behind it, so asking again with a bigger one is a
        # question the ledger cannot answer.
        capped = self.state.truncated_at.get(fingerprint)
        wants_more = capped is not None and _volume(call) > capped
        cached = self.state.last_results.get(fingerprint)
        if cached is not None and not wants_more:
            asks = self.state.seen_calls.get(fingerprint, 0) + 1
            self.state.seen_calls[fingerprint] = asks
            # The answer first, the bookkeeping after. This used to open "Not
            # run:" with ok=false, which is how a call that succeeded came to
            # look like a call that failed -- and a model that reads a failure
            # retries it.
            body = (
                f"{call.name} returned:\n\n{cached}\n\n"
                "-- that is the current answer. The call ran earlier, nothing in the "
                "workspace has changed since, so it was answered from that result "
                "rather than dispatched again. Use it and move to the next step; if it "
                "does not tell you what you need, ask something different or say "
                "plainly what is blocking you."
            )
            if asks >= 3:
                body += (
                    f"\n\nThis is ask number {asks} for this exact call, and it will "
                    "keep returning the answer above while the workspace is unchanged. "
                    "Turns that only repeat earlier calls end the run."
                )
            return (
                body,
                f"{call.name} asked again with the same arguments; answered from the "
                "previous result",
            )

        # The eleventh read of one file, at an eleventh set of line numbers.
        # `_fingerprint` covers the whole call, so re-reading one file at four
        # ranges is four different calls and invisible to the ledger above.
        if ranges := self._re_reading(call):
            return (
                f"You have read this file {len(ranges)} times this run, at "
                f"{', '.join(ranges)}, and every one of those reads is still in context "
                "above.\n\nReading it again is not going to show you anything those did "
                "not. Act on what you have, or say plainly what you are looking for and "
                "cannot find.",
                f"already read {len(ranges)} times this run; not re-read",
            )
        return None

    def _plan_submitted(self, tool: str, outcome: ToolResult) -> Iterator[Event]:
        """Act on the tool call that ends the planning phase.

        A typed event, so there is nothing to interpret. ``submit_plan`` pins the
        plan and hands the run to the acting mode; ``ask_developer`` ends the run
        with the questions on screen, where the developer's answer arrives as a
        follow-up on this transcript.
        """
        if tool == "ask_developer":
            self.result = RunResult(
                Outcome.DONE,
                "the planner asked for a decision before it could plan; answer it "
                "and the run continues from here",
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        steps = steps_from_meta(dict(outcome.meta))
        self.state.plan = steps
        self.state.plan_summary = str(outcome.meta.get("summary") or "")
        rendered = "\n".join(step.rendered(i) for i, step in enumerate(steps, 1))
        if self.state.plan_summary:
            rendered = f"{self.state.plan_summary}\n\n{rendered}"
        self.context.set_plan(rendered)
        yield Event(EventType.PLAN, {"text": rendered, "steps": len(steps)})
        self._switch(Mode.AGENT)

    def _inner_loop(self) -> Iterator[Event]:
        """Format and lint what was just written, sub-second.

        The result goes into context as a message rather than being merely
        reported, because its whole purpose is to be in front of the model on the
        next turn while the edit is still what it is thinking about.
        """
        report = inner_loop(self.router, self.router.touched)
        yield Event(EventType.GATE, {"kind": "inner", **report.as_dict()})
        if not report.ok or report.warnings:
            self.context.append_user(
                "The formatter and the contract linter ran on what you just "
                f"changed:\n\n{report.summary()}"
            )

    # -- ending a turn ----------------------------------------------------

    def _finish_turn(self, result: TurnResult) -> Iterator[Event]:
        """What happens after a turn that called no tools."""
        text = (result.chat.content or "").strip()

        if self.state.mode is Mode.ASK:
            # The whole of the read-only path: the model stopped calling tools,
            # so it has answered. One loop, one answer, no gate, no plan, no
            # mode below this one to hand anything to.
            self.result = RunResult(
                Outcome.DONE,
                "answered" if text else "the model ended the turn with nothing to say",
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        if self.state.mode is Mode.PLANNER:
            # Forced once already and still no `submit_plan`. That is a planner
            # with nothing to plan, which is a legitimate answer to a request
            # that turned out not to need a change -- and the honest thing is to
            # say so rather than manufacture a plan and run a gate on it.
            self.result = RunResult(
                Outcome.DONE,
                "no plan was submitted; the reply describes the code rather than "
                "proposing a change, so nothing was executed and nothing was "
                'touched. Say what you want changed and it will be done',
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        yield from self._verify()

    def _verify(self) -> Iterator[Event]:
        """Run the gate and act on it.

        **Never on an empty change set.** This is the invariant the report asks
        for and the single most consequential line in the file: *a run that wrote
        nothing cannot fail*. The gate used to run whenever an acting mode ended
        a turn without a tool call, including the turn where it said "there is
        nothing to do here" -- so an explanation question ran a seventy-second
        gate on an untouched workspace, adopted a pre-existing ``go_vet`` failure
        as its own, and had ``go mod tidy`` rewrite ``go.mod`` on its way past.

        Skipped in one other case, and it is not the model's to invoke: the
        workspace is byte-for-byte what it was when the gate last ran. The gate
        is a function of the files and the toolchain, so re-running it there
        cannot produce a different verdict -- it can only spend another build,
        vet and swagger_check arriving at the report already in context.
        """
        if self.router.mutations == 0:
            self.result = RunResult(
                Outcome.DONE,
                "nothing was changed, so there was nothing to verify. If work was "
                "wanted here, say what should change and it will be done",
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        key = (self.router.mutations, tuple(self.router.touched))
        if self.state.last_gate is not None and key == self.state.gate_key:
            # Only reachable on a failing report: a clean one ends the run below.
            report = self.state.last_gate
            yield Event(EventType.GATE, {"kind": "full", "cached": True, **report.as_dict()})
            yield from self._gate_failed(report, rerun=False)
            return

        self._await_baseline()
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
            self.state.gate_failures = 0
            self.result = RunResult(
                Outcome.DONE,
                self._done_summary(report),
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
            return

        yield from self._gate_failed(report, rerun=True)

    def _gate_failed(self, report: GateReport, *, rerun: bool) -> Iterator[Event]:
        """Hand a failing gate back to the acting mode, or stop.

        As an ordinary user message, in the same mode. There is no Verifier to
        report it, no ladder to escalate through, and no second persona to hand
        it to -- the model that made the change is the one that reads the
        failure, which is how every mature agent does it and how a human does it.

        Bounded by ``MAX_GATE_FAILURES`` failing gates *with nothing edited in
        between*. A gate is a function of the files: given no new edit it will
        fail identically, and asking again is asking the same question.
        """
        self.state.gate_failures += 1
        if self.state.gate_failures > MAX_GATE_FAILURES:
            self.result = RunResult(
                Outcome.UNVERIFIED,
                f"the gate did not come clean after {MAX_GATE_FAILURES} attempts"
                + (f"; blocked at {report.blocked_by.name}" if report.blocked_by else "")
                + self._unfinished(),
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
            yield Event(EventType.ERROR, {"message": self.result.summary})
            return

        if rerun:
            self.context.append_user(
                "The gate ran on your change and it is not clean yet. This is its "
                f"report:\n\n{report.summary()}\n\n"
                "Fix what it found. Anything it marks advisory is either pre-existing "
                "or not about this change, and is not yours to fix."
            )
        else:
            blocker = report.blocked_by.name if report.blocked_by else "the gate"
            self.context.append_user(
                "The gate was not re-run. Nothing in the workspace has changed since "
                f"it last ran, so its verdict cannot have changed: still blocked at "
                f"{blocker}, and the report above is still the whole of it.\n\n"
                "Nothing will move until a file does. Make the edit, or say plainly "
                "what is stopping you from making it."
            )

    def _abort(self) -> RunResult:
        return RunResult(
            Outcome.ABORTED,
            "stopped by the developer"
            + (
                f"; {len(self.router.touched)} file(s) had already changed"
                if self.router.touched
                else " before anything changed"
            ),
            self.context.turn,
            tuple(self.router.touched),
            self.state.last_gate,
        )

    def _relay(self, event: Event | None) -> None:
        """Send a transient event out of band, and never let it cost the run.

        ``None`` is the ordinary case: the coalescer holds a fragment back until
        it has enough text or enough time has passed.

        The sink is a client that may have gone. Streaming is a view of a turn
        and never the turn itself, so a broken sink is switched off rather than
        raised: the answer still arrives in full as the ``assistant`` message at
        the end of the turn.
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
        carries, and on a follow-up they start out disagreeing -- the loop is
        newly built and the context is still wherever the previous run left it.
        """
        if mode is self.state.mode and mode is self.context.mode and self.context.turn > 0:
            return
        self.state.mode = mode
        self.context.switch_mode(mode, mode_instruction(mode))

    # -- helpers ----------------------------------------------------------

    def _unfinished(self) -> str:
        """Files the plan named that were never written.

        Read off ``submit_plan``'s typed steps rather than out of prose. The old
        version matched path-shaped tokens in numbered paragraphs, which reported
        a neighbour named as an example as an unwritten target.
        """
        missing = self._unwritten_targets()
        return ". The plan named files this run never wrote: " + ", ".join(missing) if missing else ""

    def _unwritten_targets(self) -> list[str]:
        """Plan steps whose file no change reached."""
        if not self.state.plan:
            return []
        touched = set(self.router.touched)
        return [s.file for s in self.state.plan if s.file and s.file not in touched]

    def _retrieval_overlap(self, call: ToolCall, outcome: ToolResult) -> str:
        """What to tell a run that keeps asking the corpus the same thing.

        Judged on the answer rather than on the question, because ``search_docs``
        runs BM25 with no floor and its scores do not separate "the corpus
        answers this" from "the corpus contains these words" -- measured against
        the real 92-section corpus, the query a field transcript died on scored
        higher than every question the corpus genuinely answers.

        What is reliable is the sections that come back. Twenty search_docs turns
        in the field returned the same four sections for six different phrasings,
        and nothing said so.
        """
        if call.name != "search_docs" or not outcome.ok:
            return ""
        hits = frozenset(str(h) for h in (outcome.meta.get("hits") or ()))
        if not hits:
            # "nothing matches" is already an explicit answer; counting it as a
            # repeat would punish the one reply that is honest about coming back
            # empty.
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

        if hits - seen:
            self.state.retrieval_repeats = 0
            return ""
        self.state.retrieval_repeats += 1

        if self.state.retrieval_repeats < MAX_RETRIEVAL_REPEATS:
            return (
                f"Those are the same sections {source or 'an earlier search'!r} already "
                "returned -- that search added nothing you had not been given.\n\n"
                "Rewording the question will not reach different sections. Ask about "
                "something else, or work from what is already above."
            )
        return (
            f"That is {self.state.retrieval_repeats} searches in a row returning "
            "sections you already have. The knowledge base does not cover this "
            "question -- that is an answer, not a gap to keep searching for.\n\n"
            "Stop rephrasing it. Follow the pattern in the nearest existing code "
            "instead, and if the step genuinely cannot be done without knowing this, "
            "say which step and what you need, in one line."
        )

    def _re_reading(self, call: ToolCall) -> list[str]:
        """The ranges a path has already been read at, once that is too many.

        Returns empty until the limit is reached, so the ordinary read-edit-read
        cycle is untouched; the ledger is cleared for a path the moment that path
        is written.
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

    def _thrashing(self) -> str:
        """Whether the run is spending its turns on eviction rather than work.

        Compaction is designed to be rare: it retains to a token floor precisely
        so the next turn does not immediately trip the threshold again. When it
        stops being rare, the working set is bigger than the budget can hold, and
        every compaction evicts the file the model then re-reads -- a closed
        circuit that does not open on its own.
        """
        window = [t for t in self.state.compactions if self.context.turn - t < COMPACTION_WINDOW]
        if len(window) < MAX_CLOSE_COMPACTIONS:
            return ""
        return (
            f"{len(window)} compactions in {COMPACTION_WINDOW} turns: the working set "
            f"is larger than the {self.context.budget:,}-token budget can hold, so each "
            "turn is evicting what the last one read. Narrow the task, or work on fewer "
            "files at once"
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
                "turns": getattr(recap, "turns", None),
            },
        )

    def _summarise(self, messages: Sequence[Message]) -> Recap:
        """Summarise the evicted working set into a structured recap.

        Structured rather than prose, because ``do_not_retry`` is what stops the
        post-compaction agent cheerfully repeating the dead end that got it here.
        A prose summary loses exactly the field that earns the compaction. It is
        now asked for with ``response_format`` as well as in words, so a model
        that ignores the instruction is constrained by the schema.

        Falls back to a recap built from the tail if the call fails or comes back
        unparseable: a degraded recap beats ending the run.
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
            goal=self.state.plan_summary or (self.state.plan[0].action if self.state.plan else ""),
            files_modified=modified,
            files_read=read,
            open_items=("the recap could not be summarised; the tail is preserved below",),
            decisions=(transcript[-2000:],) if transcript else (),
            turns=turns,
        )

        try:
            reply = self.client.chat(
                [{"role": "user", "content": _RECAP_PROMPT + transcript}],
                # `fast`, which is what section 6.5 specifies and what
                # `LLMConfig.model_for` actually accepts. This said "summariser"
                # for its whole life, `model_for` rejects that on both
                # deployments, and every compaction in production silently
                # returned the fallback recap.
                role="fast",
                max_tokens=1024,
                enable_thinking=False,
                response_format=_RECAP_SCHEMA,
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
            # Ours, not the endpoint's. A misconfigured role or a bad argument is
            # a permanent failure that degrades every compaction for the life of
            # the process, and a broad `except` hid exactly that for this
            # method's entire history.
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

        parsed = _parse_json_object(reply.content or "")
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
        list is right even when the model returns nothing usable -- and because
        it is a fact about the eviction, which the summariser is not told about.
        These are reads, not writes.
        """
        seen = [m.path for m in messages if m.path]
        return tuple(dict.fromkeys(seen))

    def _done_summary(self, report: GateReport) -> str:
        """What the developer reads when a run ends DONE.

        Read off the report rather than off the outcome, because ``ok`` covers
        two different claims: the stages passed, and the stages did not apply.
        Saying "the gate is clean" for the second is an overclaim.
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
        # The gap goes in the headline, not in a footnote after it.
        if missing := self._unwritten_targets():
            return (
                f"{len(files)} file(s) changed and the gate is clean, but the plan is "
                f"not finished -- it set out to write {', '.join(missing)}, and "
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
        # Named for what it is. Rendered, `git_status {"_raw":"{"}` reads as
        # though the router invented a parameter and passed it on.
        return {"_malformed_arguments": call.arguments[:500]}


#: Parameters that bound how much of an answer comes back, never what the answer
#: is. Excluded from the fingerprint because varying one is not a new question.
#: A tuple, not a set: `_volume` returns the first one present, and a set's
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

    Falls back to the raw string when the arguments do not parse -- a model that
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

    Recorded so compaction can say which files it is about to evict. It no
    longer supersedes anything -- see ``ContextManager.SUPERSEDE_SLICES``.
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
