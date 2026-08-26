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

from dakcoder_shared.envelope import Event, EventType, ToolResult
from dakcoder_shared.llm import LLMClient, ToolCall

from .context import ContextManager, Message, OverBudgetError, Recap
from .gate import GateReport, full_gate, inner_loop
from .llm import TurnResult, complete, reasoning_leaked
from .modes import Mode
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
    plan: str = ""
    last_gate: GateReport | None = None
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
        steer: Callable[[], list[str]] = list,
        cancelled: Callable[[], bool] = lambda: False,
        winding_down: Callable[[], bool] = lambda: False,
        max_turns: int = 40,
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
        self.state = _State()
        self.result: RunResult | None = None

    # -- the run ----------------------------------------------------------

    def run(
        self,
        task: str,
        *,
        acceptance: Sequence[str] = (),
        start: Mode = Mode.PLANNER,
    ) -> Iterator[Event]:
        """Drive the run, yielding events as they happen."""
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
            yield Event(EventType.STEER, {"text": correction, "turn": self.context.turn})

        turn = self.context.begin_turn()
        yield Event(
            EventType.TURN_START,
            {"turn": turn, "mode": str(self.state.mode), "attempt": self.state.attempts},
        )

        if self.context.should_compact():
            yield from self._compact()

        try:
            result = complete(
                self.context,
                self.client,
                tools=self.router.schemas_for(self.state.mode),
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
                    tools=self.router.schemas_for(self.state.mode),
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

        yield from self._usage(result)

        if result.chat.content:
            self.context.append_assistant(result.chat.content)
            yield Event(EventType.ASSISTANT, {"text": result.chat.content})

        if result.chat.tool_calls:
            yield from self._tool_calls(result.chat.tool_calls)
            return

        if result.chat.content and self._repeating(result.chat.content):
            self.result = RunResult(
                Outcome.NO_PROGRESS,
                f"the same reply {NO_PROGRESS_REPEATS} turns running, with no tool call",
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
        """Run the gate and act on it. The one thing the model cannot skip."""
        report = full_gate(
            self.router,
            self.router.touched,
            dependencies_changed=self.state.dependencies_changed,
        )
        self.state.last_gate = report
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
            + (f"; blocked at {report.blocked_by.name}" if report and report.blocked_by else ""),
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

    def _switch(self, mode: Mode) -> None:
        if mode is self.state.mode and self.context.turn > 0:
            return
        self.state.mode = mode
        self.state.recent.clear()
        self.context.switch_mode(mode, mode_instruction(mode))

    # -- helpers ----------------------------------------------------------

    def _stuck(self, fingerprint: str) -> bool:
        self.state.recent.append(fingerprint)
        del self.state.recent[:-NO_PROGRESS_REPEATS]
        return (
            len(self.state.recent) == NO_PROGRESS_REPEATS
            and len(set(self.state.recent)) == 1
        )

    def _repeating(self, text: str) -> bool:
        """The same reply, three turns running, with no tool call between them.

        ``_stuck`` judges a turn by the arguments it dispatched, so a turn that
        dispatches nothing is invisible to it. That is the hole a typo fell
        through: the modes kept advancing, each one restated the same refusal,
        and the loop had no way to notice because not one of those turns called
        a tool. Whitespace is normalised because a model that re-wraps the same
        paragraph has not made progress either.
        """
        self.state.said.append(" ".join(text.split()))
        del self.state.said[:-NO_PROGRESS_REPEATS]
        return (
            len(self.state.said) == NO_PROGRESS_REPEATS
            and len(set(self.state.said)) == 1
        )

    def _compact(self) -> Iterator[Event]:
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

        created, modified = self._touched(messages)
        fallback = Recap(
            goal=self.state.plan.splitlines()[0] if self.state.plan else "",
            files_created=created,
            files_modified=modified,
            open_items=("the recap could not be summarised; the tail is preserved below",),
            decisions=(transcript[-2000:],) if transcript else (),
            turns=turns,
        )

        try:
            reply = self.client.chat(
                [{"role": "user", "content": _RECAP_PROMPT + transcript}],
                role="summariser",
                max_tokens=1024,
                enable_thinking=False,
            )
        except Exception:  # noqa: BLE001 - a degraded recap beats ending the run
            return fallback

        parsed = _parse_recap(reply.content or "")
        if parsed is None:
            return fallback
        return Recap(
            goal=parsed.get("goal", "") or fallback.goal,
            plan_step=parsed.get("plan_step", ""),
            files_created=tuple(parsed.get("files_created") or created),
            files_modified=tuple(parsed.get("files_modified") or modified),
            decisions=tuple(parsed.get("decisions") or ()),
            verified=tuple(parsed.get("verified") or ()),
            open_items=tuple(parsed.get("open_items") or ()),
            do_not_retry=tuple(parsed.get("do_not_retry") or ()),
            turns=turns,
        )

    @staticmethod
    def _touched(messages: Sequence[Message]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """File paths recovered from the messages themselves.

        Recovered rather than asked for, so the two list fields are right even
        when the model returns nothing usable.
        """
        seen = [m.path for m in messages if m.path]
        ordered = list(dict.fromkeys(seen))
        return (), tuple(ordered)

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


def _count_steps(plan: str) -> int:
    import re

    return len(re.findall(r"^\s*\d+[.)]\s", plan, flags=re.MULTILINE))


def _is_scaffold_plan(plan: str) -> bool:
    """Whether the plan is the 10-step resource recipe.

    Keyed off the tool the plan names rather than off prose. A plan that says
    "create a new resource" in English but never mentions the scaffolder is a
    plan to write seven files by hand, and routing it to the Scaffolder would
    hand it a mode whose only tools are the ones it did not ask for.
    """
    lowered = plan.lower()
    return "resource_scaffold" in lowered or "project_scaffold" in lowered
