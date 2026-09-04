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

import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from dakcoder_shared.envelope import DeltaCoalescer, Event, EventType, ToolResult
from dakcoder_shared.llm import (
    LLMClient,
    Metering,
    ToolCall,
    UnsupportedParameterError,
)
from dakcoder_shared.paths import PathEscape
from dakcoder_shared.tokens import estimate_tokens

from .context import ContextManager, Eviction, Message, OverBudgetError, Recap
from .gate import (
    Baseline,
    GateReport,
    StageResult,
    full_gate,
    inner_loop,
    take_baseline,
)
from .llm import TurnResult, complete, reasoning_leaked
from . import metrics
from .modes import CONTEXT_WINDOW, Intent, Mode, config_for
from .prompts import mode_instruction, system_prompt
from .tools import control
from .tools.control import PlanStep, steps_from_meta
from .tools.router import ApprovalRequest, Router

log = logging.getLogger(__name__)

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
class _ReadLedger:
    """What a run has already been shown of one file.

    Intervals, not a count. The question "have you seen these lines" has an
    exact answer and this holds enough to give it; the flat ten-reads-per-path
    rule this replaces could only answer "have you asked ten times", which on a
    6,571-line handler is a different question with a very different answer.

    Merged as they arrive, so the union stays small however many windows a model
    works through -- a file read front to back in fifty pieces ends up as one
    interval.
    """

    #: Disjoint, sorted, inclusive ``[low, high]`` line ranges.
    covered: list[tuple[int, int]] = field(default_factory=list)
    #: The file's length, once a read has reported it. 0 while unknown.
    lines: int = 0
    #: Dispatched reads of this path, for the backstop ceiling.
    calls: int = 0
    #: The file's modification time when it was last read, so a follow-up can
    #: tell whether what the model saw is still what is there. 0.0 when unknown.
    mtime: float = 0.0

    def add(self, low: int, high: int) -> None:
        if high < low:
            low, high = high, low
        merged: list[tuple[int, int]] = []
        placed = (low, high)
        for span in sorted([*self.covered, placed]):
            if merged and span[0] <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
            else:
                merged.append(span)
        self.covered = merged

    def covers(self, low: int, high: int) -> bool:
        """Whether every line in ``[low, high]`` has already been delivered."""
        if high < low:
            low, high = high, low
        return any(span[0] <= low and high <= span[1] for span in self.covered)

    def covered_lines(self) -> int:
        return sum(high - low + 1 for low, high in self.covered)

    def budget(self) -> int:
        """How many separate reads this file is worth.

        Proportional to its length, because that is what a model working through
        one honestly needs, and bounded at both ends so a small file still gets
        a sensible number of looks and a generated monster cannot buy unbounded
        turns.
        """
        if not self.lines:
            return MIN_READS
        want = -(-self.lines // LINES_PER_READ)  # ceil
        return max(MIN_READS, min(MAX_READS, want))

    def summary(self) -> str:
        shown = ", ".join(f"{low}-{high}" for low, high in self.covered[:4])
        more = "" if len(self.covered) <= 4 else f" and {len(self.covered) - 4} more"
        return f"earlier reads covering lines {shown}{more}"


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
    #: Consecutive tool-calling turns that added nothing new -- every call in
    #: them answered from a ledger rather than dispatched.
    stalled_turns: int = 0
    #: What has already been delivered of each path, as line intervals. See
    #: ``_ReadLedger``: the old form was a list of range *labels* and a count,
    #: which could say how often a file had been asked for and not how much of
    #: it the model had actually seen.
    reads: dict[str, _ReadLedger] = field(default_factory=dict)
    #: What each search returned, as the set of places it pointed at: section
    #: citations for ``search_docs``, ``path:line`` for ``search_repo``. Keyed by
    #: tool, because the question "did that add anything" is the same question
    #: for both and the answer is per-tool.
    #:
    #: This was ``search_docs`` alone, and that was the whole of the loop's
    #: semantic progress detection: one tool out of twenty-three. ``search_repo``
    #: is the tool a run actually loops on -- ``_fingerprint`` is byte-exact, so
    #: `"Handler"`, `"handler"` and `"Handler\\("` are three distinct calls to
    #: every ledger in this file and one question to anybody reading them.
    retrievals: dict[str, list[tuple[str, frozenset[str]]]] = field(default_factory=dict)
    retrieval_repeats: dict[str, int] = field(default_factory=dict)
    #: Digests of every tool result this run has already been shown. A dispatched
    #: call whose answer is byte-identical to an earlier one did not inform the
    #: run, whatever its arguments were.
    seen_digests: set[str] = field(default_factory=set)
    #: What this run has established will not work, newest last. Written by
    #: ``revise_plan``, by an exhausted search, by a dead end and by the replan
    #: path, and re-sent to the model in the state block on every turn.
    #:
    #: This is the field the planner never had. A run could fail the same gate
    #: three times and start over with no record of what it had already tried,
    #: which is what made every "change strategy" move a re-roll.
    ruled_out: list[str] = field(default_factory=list)
    #: How many times the run has been handed back to the Planner after the
    #: acting phase could not clear the gate. Bounded by ``MAX_REPLANS``.
    replans: int = 0
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
    #: Whether a turn has already been re-asked with ``tool_choice: required``.
    #:
    #: Once per **phase**, not once per turn. Forcing on every prose turn reads as
    #: harmless and is not: a Planner that has decided there is nothing to plan
    #: says so, is forced, complies with some call it does not need, says so
    #: again, is forced again -- two model calls a turn to relitigate a decision
    #: it has already made twice. The first refusal might be a turn whose call
    #: was never emitted; the second is an answer, and the loop already knows
    #: what to do with it.
    #:
    #: Reset when a phase ends. Run scope was the wrong reading of that argument:
    #: it is about one mode relitigating one decision, and a Planner that used
    #: the re-ask left the acting mode without one (prior-audit TC-4).
    forced: bool = False
    #: The most recent intercept result per fingerprint, so the next repeat can
    #: supersede it rather than stack beside it. Measured: one such pair in
    #: history and the model moves on; two and it repeats forever. See
    #: ``ContextManager.supersede``.
    echoes: dict[str, Message] = field(default_factory=dict)
    #: Set when a turn asked for nothing it had not already been given, and the
    #: next turn must therefore answer rather than call. Cleared once used.
    must_answer: bool = False
    #: Why the next turn is being made to answer. Two situations reach the same
    #: mechanism and they are not the same thing: a *stall* is the model asking
    #: for what it has already been given; a *refused terminal* is the model
    #: reaching for the exit with arguments the schema would not take. Telling it
    #: "that call has already been answered and asking it again returns the same
    #: thing" after the second is false on every clause, and it teaches the wrong
    #: correction -- the arguments, not the repetition, are the problem
    #: (BUG L-14).
    answer_because: str = ""
    #: ``router.mutations`` as of the last failing gate, so turns that follow it
    #: without editing anything can be counted. See ``_gate_stalled``.
    gate_mutations: int = 0
    #: Turns since a failing gate in which nothing was written.
    idle_since_gate: int = 0
    #: Tool-calling turns in this phase that have not reached a terminal tool.
    #: Reset when a phase ends, because the next one starts its own count.
    research_turns: int = 0
    #: How many times a `finish` that abandoned the plan has been sent back.
    #: Bounded: the model may have decided a step is unnecessary, and this reads
    #: paths out of the plan rather than out of the work, so it is not the
    #: arbiter of who is right.
    finish_refused: int = 0
    #: Terminal calls forced that did not land -- the model was made to call
    #: `submit_plan` and sent arguments the schema refused, say. Bounded,
    #: because forcing the same call again is the loop this exists to escape.
    forced_terminal: int = 0
    #: Consecutive replies the output budget cut off. Reset by any reply that
    #: completes. The per-turn handling of a truncated reply is careful -- every
    #: declared call answered, the cause named accurately -- but nothing counted
    #: the *repetition*, so a model that always overran its output budget spent
    #: the entire turn budget doing it and ended EXHAUSTED without truncation
    #: ever being mentioned (BUG L-13).
    truncated_turns: int = 0
    #: Truncated replies in the whole run, never reset. The streak above can be
    #: dodged by one complete reply between two overruns, which is exactly what
    #: a model does while it hunts for a way to send something too large
    #: (BUG FS-3).
    truncations: int = 0


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

#: How many stalled turns before the next one is dispatched with tool calls
#: forbidden, so prose is the only reply available.
#:
#: Two, because the first is an ordinary re-ask and the second is the start of
#: the loop. Ending a run at six was measured to be far too late and, worse,
#: to be the wrong remedy: a model repeating one call is not out of ideas, it
#: is out of *moves it recognises*, and the move it needs -- stop and say where
#: you are -- is one no message can make it take. A named `tool_choice` on
#: `finish` does, 5/5.
STALLS_BEFORE_ANSWER = 2

#: How many turns a phase may spend calling tools without reaching a terminal
#: one before it is made to reach one.
#:
#: Twelve, and the number is measured rather than chosen. Replaying a real
#: transcript against the live endpoint at increasing depth, the model stays
#: sensible through five consecutive fruitless tool calls -- widening a glob,
#: re-scoping, trying a different pattern -- and at **six** it repeats its last
#: call 5 times out of 5 and never recovers. The trap is a cliff, not a slope.
#:
#: So this is not a budget for *research*: it is a fence around the cliff, and
#: it counts turns rather than failures because a turn that found something
#: resets nothing here -- a Planner nineteen turns into reading a service is in
#: the same place whether or not its reads succeeded. Twelve leaves room for the
#: dozen calls orienting in a large service genuinely takes, and stops the
#: run that spent nineteen turns on `search_docs` and never planned.
#:
#: What happens at the limit is a forced terminal call, not the end of the run:
#: the Planner submits or asks, the acting mode finishes and the gate runs, and
#: each of those is an outcome the developer can act on.
MAX_RESEARCH_TURNS = 12

#: How many refused terminal calls before the run stops trying.
#:
#: The escape hatch needs one of its own. A forced ``submit_plan`` whose
#: arguments the schema refuses would otherwise be forced again next turn, and
#: again -- the exact loop the forcing exists to break, arriving through it. Two:
#: the first refusal switches the target to ``finish``, whose schema is one
#: required string, and a second refusal after that is not an arguments problem.
MAX_FORCED_TERMINAL = 2

#: How many replies in a row may be cut off by the output limit before the run
#: stops asking for another one.
#:
#: Three. The first is information the model did not have; the second says the
#: advice ("make the next reply shorter") did not take; a third says the mode's
#: output budget cannot hold what this turn is trying to say, and no number of
#: further attempts changes that. The run ends naming the limit, which is the
#: one thing a developer can act on -- an EXHAUSTED at turn 40 with no mention
#: of truncation is not.
MAX_TRUNCATED_TURNS = 3

#: The same ceiling, counted over the whole run rather than consecutively.
#:
#: A streak resets on any reply that arrives whole, so a run that alternates —
#: cut off, one ordinary call, cut off again — never reaches three in a row and
#: is bounded by nothing but `max_turns` (BUG FS-3). Six is generous for a run
#: that is making progress and hitting the limit occasionally, and short enough
#: that thrashing ends while a developer is still watching.
MAX_TRUNCATIONS = 6

#: Tools whose oversized argument is a document, and which therefore have a
#: chunked answer: write the first part, then append the rest. Anything else
#: that overruns is told to make a shorter reply, which for a batch of calls is
#: the correct advice. See `AgentLoop._shorter_reply`.
_CHUNKABLE_WRITES = frozenset({"write_file"})

#: How long a gate waits for the baseline before running without it.
#:
#: Three minutes, which is a cold module cache fetching from
#: gitlab.cept.gov.in. Past that the gate runs un-baselined and says so rather
#: than holding the run: a baseline is an excuse for pre-existing damage, and a
#: run that never verifies is worse than one that over-reports.
BASELINE_JOIN_SECONDS = 180

#: How much of a tool result is kept for answering an exact repeat.
#:
#: The context already holds the whole result; this is a second copy kept only so
#: a repeat can be answered without dispatching. Six thousand characters is
#: roughly a large file's worth of head, and a replay that hits the cut says so
#: rather than presenting an extract as the whole answer.
CACHED_RESULT_CHARS = 6_000

#: How many times a `finish` that abandons the plan is sent back.
#:
#: One. The acting mode gained a terminal tool to escape a loop and
#: promptly found it the easiest move in the room: measured live, two runs
#: in three called `finish` on their first acting turn, having read the
#: service and written nothing. One push is enough to distinguish "I forgot
#: to do the work" from "I decided against it" -- and the second `finish`
#: is believed, because this reads paths out of the plan and is not the
#: arbiter of whether a step was still needed.
MAX_FINISH_REFUSALS = 1

#: The same, for a `finish` that abandons the plan and does not say why.
#:
#: Two, and the difference between this and the number above is the whole point.
#: `MAX_FINISH_REFUSALS` is about a model that has *decided* against a step: it
#: put the reason in `blocked`, the loop is not the arbiter of whether the step
#: was still needed, and the second call is believed. This is about a model that
#: has decided nothing and said so -- "I need to read the handler source files,
#: then write the tests" as the run's final answer, `blocked` empty, no file
#: written.
#:
#: Measured on a field transcript: the acting phase stalled on a re-read at turn
#: 18, was forced to `finish` at turn 19, and the run ended on that sentence.
#: Both halves of that are fixed -- the stall now points at the work -- and this
#: is the backstop for the model reaching the exit on its own.
#:
#: Two rather than more, because the second refusal names exactly what is
#: missing and offers two ways out. A third would be the loop insisting, which
#: is what the escalation ladder did.
MAX_UNEXPLAINED_FINISH_REFUSALS = 2

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

#: How much of a file one read is worth, for the purpose of budgeting reads.
#:
#: The old rule was a flat ten reads per path, counted as *calls* and ignoring
#: the ranges entirely -- so a model working through `handler/paogen.go`, which
#: is 6,571 lines, was refused on its eleventh window having been shown about
#: 280 of them. Four per cent of the file, and the loop told it "reading it
#: again is not going to show you anything those did not".
#:
#: What actually needs bounding is asking for lines you have already been given,
#: and that is now checked directly: `_re_reading` refuses a range only when the
#: union of what has already been delivered contains it. The ceiling below is a
#: backstop against a model reading one line at a time forever, and it scales
#: with the file so a large one gets a proportionate budget.
LINES_PER_READ = 150

#: The floor on that budget, so a small file still gets a sensible number of
#: looks, and the ceiling, so a 60,000-line generated file cannot buy unbounded
#: turns.
MIN_READS = 10
MAX_READS = 60

#: How many retrievals in a row may return nothing new before ``search_docs`` is
#: withdrawn for the rest of the run. The corpus does not acquire new sections
#: mid-run, so nothing that follows could change the answer.
#:
#: The same ceiling counts ``search_repo`` repeats, and ``search_repo`` is
#: deliberately **not** withdrawn when it trips: the corpus is fixed and the
#: workspace is not, and taking away the tool a run navigates with would be a
#: worse failure than the one it prevents. What the count does there is stop the
#: turn being scored as progress, which is what lets the stall detector reach it.
MAX_RETRIEVAL_REPEATS = 3

#: How many failing gates before the run is handed back to the Planner, and how
#: many times that may happen.
#:
#: Two, then once. The first failing gate is the model reading a report it has
#: not seen; the second says the report was read and the fix did not work, and
#: that is the earliest point at which "this approach is wrong" is better
#: supported than "try the same thing again". A third attempt at the same plan
#: was measured to be worth nothing: the gate is a function of the files, and
#: the acting mode had already been told twice what the files needed.
#:
#: Bounded at one replan because the second one has no new information to plan
#: from. The run has by then produced two gate reports and one abandoned
#: strategy; a planner given those and still unable to clear the gate is a
#: planner the developer needs to see, not one to give a third budget to.
REPLAN_AFTER_GATE_FAILURES = 2
MAX_REPLANS = 1

#: How many entries the ruled-out ledger carries into the prompt.
#:
#: Six, and it is a prompt-cost bound rather than a memory bound: the block is
#: re-sent on every turn, so an unbounded list is an unbounded per-turn cost.
#: The newest survive, because a run that has ruled six things out is not going
#: to be saved by remembering the first.
MAX_RULED_OUT = 6

#: How much of a retrieval must be old for it to count as adding nothing.
RETRIEVAL_OVERLAP = 0.5

#: The compaction-thrash window, in turns, and how many compactions inside it
#: mean the run is evicting rather than working.
COMPACTION_WINDOW = 8
MAX_CLOSE_COMPACTIONS = 3

#: The tools that end a phase. Handled by name because the loop has to know
#: what happened, not because the router treats them specially.
#:
#: ``finish`` joined them once the live endpoint settled the question. In ``ask``
#: and ``agent``, ending a turn meant *not* calling a tool, and past about six
#: fruitless calls this model cannot produce a non-action -- it repeats its last
#: call, 5/5, and no wording changes that. Giving it a call that means stopping
#: works 5/5. See ``tools/control.py``.
_TERMINAL = frozenset({"submit_plan", "ask_developer", "finish"})

#: What a mode is forced to call when it has stopped asking for anything new.
#:
#: Named, not ``"required"``: ``required`` would let it pick a research tool and
#: carry on. Named choice is the only lever measured to work here -- 5/5 on the
#: live endpoint at the depth where every wording fails.
_FORCE_FINISH: dict[str, Any] = {
    "type": "function",
    "function": {"name": "finish"},
}


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
        #: This run's accounting. Replaced at `run`; initialised here so a
        #: caller driving `_run` directly still has one to finish.
        self._metrics_acc = metrics.Accumulator()

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
        """Drive the run, teeing every event into the run's own accounting.

        A thin wrapper on ``_run`` for one reason: events are yielded from a
        dozen nested generators, and the accounting needs to see all of them.
        Teeing here is the only funnel every event passes through, so a new
        `yield Event(...)` anywhere inside cannot be missed by omission — which
        is exactly how the tool-call invariant came to be a discipline that two
        paths forgot (BUG L-1).

        The accumulator holds counters and path sets, never content, so this
        costs a run bounded memory and no retained transcript.
        """
        self._metrics_acc = metrics.Accumulator(self.session_id or "")
        for event in self._run(
            task,
            acceptance=acceptance,
            intent=intent,
            continued=continued,
            start=start,
        ):
            try:
                self._metrics_acc.feed({"type": str(event.type), "data": event.data})
            except Exception:  # noqa: BLE001 - accounting must never fail a run
                log.warning("run metrics could not read an event", exc_info=True)
            yield event

    def _run(
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
                "is resumable -- Resume continues on this same transcript and the "
                "same context, with a fresh turn budget. For a task this size, "
                "raise dakcoder.maxTurns",
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )

        # The quota moved, and the run is the only thing that knows it has
        # finished moving it. The event type existed and nothing ever emitted
        # one, so the status bar's listener and the quota tree's refresh were
        # both unreachable (BUG EXT-15) and the figure on screen was whatever the
        # 60-second poll had last seen — including for the whole time after a run
        # ended, when the poll has stopped. It carries no data on purpose: the
        # gateway owns the numbers and `GET /v1/quota` is the shape under
        # contract.
        yield Event(EventType.QUOTA, {"reason": "run finished"})
        yield Event(EventType.FINISH, self.result.as_dict())
        yield from self._metrics()
        yield Event(EventType.END, self.result.as_dict())

    def _metrics(self) -> Iterator[Event]:
        """One record of what this run cost and where it ran out of room.

        Emitted before ``end`` so it lands in the transcript rather than after
        it, and built by ``metrics.from_events`` — the same function a report
        uses to rebuild the record from a journal — so the live number and the
        reconstructed one cannot disagree. The loop supplies only the two facts
        the events do not carry: the ceilings this run was measured against.

        Never fails a run. A run that finished is finished; an arithmetic error
        in its accounting must not turn that into an error the developer sees.
        """
        try:
            config = config_for(self.state.mode)
            record = self._metrics_acc.finish()
            record.session_id = record.session_id or (self.session_id or "")
            record.outcome = str(self.result.outcome) if self.result else ""
            record.turns = self.context.turn
            record.output_limit = record.output_limit or config.max_tokens
            record.context_window = CONTEXT_WINDOW
            record.budget = record.budget or config.prompt_budget
            payload = record.as_dict()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            log.warning("run metrics could not be assembled: %s", exc, exc_info=True)
            return

        # One line in the server log as well as the event, because the event
        # lands in a workspace the operator may never look at and the log is
        # the thing they already tail. `scripts/context-report.py` is the
        # detail; this is enough to notice that a run was shaped by the window.
        log.info(
            "run %s %s in %d turn(s): peak prompt %d/%d tokens (%.0f%% of the window), "
            "%d compaction(s) discarding %d tokens, %d truncation(s), "
            "%d file(s) evicted then re-read, %d read(s) refused as already held, "
            "%d bytes of source read",
            record.session_id or "?",
            record.outcome or "?",
            record.turns,
            record.peak_prompt_tokens,
            record.budget,
            record.peak_pct_of_window,
            len(record.compactions),
            sum(c["freed"] for c in record.compactions),
            record.truncations,
            len(record.evicted_paths_reread),
            record.intercepted_re_read,
            record.bytes_read,
        )
        yield Event(EventType.METRICS, payload)

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
        """Block until the baseline is in, if it is not already.

        The reference is *kept* when the join times out. It used to be cleared
        unconditionally, so a slow baseline — a cold module cache is minutes, not
        seconds — landed mid-run and the gates on either side of it disagreed
        about what was already broken: the early ones blamed the run for damage
        it had not done, the later ones excused it, and nothing said which had
        happened (BUG L-16). Keeping the reference means the next gate waits for
        the same thread instead of running un-baselined again, so a run gets one
        answer to "what was already broken" rather than two.
        """
        thread = self._baseline_thread
        if thread is None:
            return
        thread.join(timeout=BASELINE_JOIN_SECONDS)
        if not thread.is_alive():
            self._baseline_thread = None

    # -- one turn ---------------------------------------------------------

    def _turn(self) -> Iterator[Event]:
        for correction in self.steer():
            # Appended as a user message so it lands in the working set the same
            # way the original task did, and the model treats it as instruction
            # rather than as tool output it can weigh against its own plan.
            self.context.append_user(correction)
            self.context.pin_directive(correction)
            yield Event(EventType.STEER, {"text": correction, "turn": self.context.turn})

        if reason := self._gate_stalled():
            self.result = RunResult(
                Outcome.UNVERIFIED,
                reason,
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )
            yield Event(EventType.ERROR, {"message": reason})
            return

        turn = self.context.begin_turn()
        yield Event(
            EventType.TURN_START,
            {
                "turn": turn,
                "mode": str(self.state.mode),
                # Carried on every turn so the panel can say why it is in the
                # mode it is in. This is the decision the whole run turns on and
                # nothing on the wire used to name it.
                "intent": str(self.state.intent),
                # The attempt about to be made, not the number of failures
                # behind it. `gate_failures` is 0 before any gate has failed, so
                # the wire said "attempt 0" while the panel's own default said 1
                # and its grid header counted from 1 — the first column was
                # labelled with a number no other surface used (BUG EXT-7).
                "attempt": self.state.gate_failures + 1,
            },
        )

        # Ground truth, re-asserted before the model is asked anything. Order
        # matters: the sync reads the workspace and the gate, the block renders
        # what the sync established, and both happen after `begin_turn` so the
        # turn number in the block is the turn the model is about to take.
        self._sync_plan()
        self.context.set_state(self._state_block())

        tools = self._tools()
        self.context.observe_tool_schemas(estimate_tokens(json.dumps(tools)))

        # Two reasons a turn is made to reach a terminal tool, and they are
        # exclusive: one message, never both.
        #
        # A *stall* is the model asking for what it has already been given. A
        # *long phase* is it working productively and never stopping. Both end
        # the same way -- a named `tool_choice` on the phase's terminal tool,
        # which is the one lever measured to work here -- but they are different
        # situations and the model is told which one it is in.
        answering, self.state.must_answer = self.state.must_answer, False
        because, self.state.answer_because = self.state.answer_because, ""
        forced_choice: str | None = None
        if answering:
            # Measured live at the depth where the loop forms: the instruction
            # alone breaks the repeat but the model keeps acting (it has no other
            # move); the tool alone is ignored; the two together end the turn
            # 5/5, and the named `tool_choice` makes it 5/5 regardless.
            #
            # The *text* depends on why. A refused terminal call routed through
            # here was told "that call has already been answered and asking it
            # again returns the same thing" -- false on every clause, and it
            # points the model at the wrong correction (BUG L-14).
            outstanding = (
                []
                if because or self.state.mode is not Mode.AGENT
                else self._unwritten_targets()
            )
            if outstanding:
                # BUG L-2's shape, on the path it was never applied to.
                #
                # `MAX_RESEARCH_TURNS` below already asks this question: a phase
                # that has read enough and written nothing is pointed *at the
                # work*, not at the exit. The stall path did not, so a run whose
                # last two turns repeated a read was forced to `finish` -- and
                # complied, honestly and uselessly, having written none of the
                # files its own plan named.
                #
                # A field transcript: turns 17 and 18 re-read one 6,571-line
                # handler, `stalled_turns` reached 2 at exactly the moment it is
                # designed to, and the remedy ended a run that had done no work.
                # The stall detector was right and its answer was wrong.
                #
                # `required` rather than a named choice, for the reason the fence
                # below gives: the right move is `write_file` or `patch_file`,
                # and which one depends on whether the file exists yet.
                forced_choice = "required"
                self.context.append_user(
                    "Stop searching. That call has already been answered and asking "
                    "it again returns the same thing.\n\n"
                    "You are not finished: your plan set out to write "
                    + ", ".join(outstanding)
                    + ", and "
                    + ("none of them have" if len(outstanding) > 1 else "it has not")
                    + " been written. Write "
                    + ("them" if len(outstanding) > 1 else "it")
                    + " now, from what you already have -- a test file you can improve "
                    "later beats a file that does not exist.\n\n"
                    "If a file genuinely cannot be written, say which and why in one "
                    "line and call `finish` with that reason in `blocked`."
                )
            else:
                self.context.append_user(
                    because
                    or (
                        "Stop searching. That call has already been answered and asking it "
                        "again returns the same thing.\n\n"
                        "Give the developer what you have established now, and say what you "
                        "could not find out."
                    )
                )
        elif self.state.research_turns >= MAX_RESEARCH_TURNS:
            # Walked to the fence rather than off the cliff. See
            # MAX_RESEARCH_TURNS: past six consecutive fruitless calls this
            # model repeats itself 5/5 and nothing recovers it.
            #
            # Where it is *pointed* depends on whether there is outstanding
            # work, and getting that wrong wastes the whole run. Measured live:
            # an acting mode twelve turns into reading a service, with a plan
            # saying "write migration.md" and nothing written, was forced to
            # `finish` -- and finished, honestly and uselessly, with "nothing
            # was changed". The bound had fired correctly and pointed at the
            # exit instead of at the work.
            outstanding = (
                self._unwritten_targets() if self.state.mode is Mode.AGENT else []
            )
            answering = True
            if self._gate_wants_an_edit():
                # A failing gate in the same context says "Make the edit, or say
                # plainly what is stopping you". Forcing `finish` on the same
                # turn forbids the first half of that instruction, and the run
                # then burns MAX_FORCED_TERMINAL forced finishes and ends
                # UNVERIFIED with the fix one call away (BUG L-2). `required`
                # keeps a tool call mandatory without naming which.
                forced_choice = "required"
                report = self.state.last_gate
                blocker = (
                    report.blocked_by.name if report and report.blocked_by else "the gate"
                )
                self.context.append_user(
                    f"You have spent {self.state.research_turns} turns in this phase "
                    f"without clearing the gate, which is still blocked at {blocker}.\n\n"
                    "Reading more will not move it — the gate is a function of the "
                    "files. Make the edit it asked for now, or say in one line what is "
                    "stopping you and call `finish`."
                )
            elif outstanding:
                # Not a terminal call: a tool call, any tool call, with the
                # message naming what is missing. `required` rather than a named
                # choice because the right move is `write_file` or `patch_file`
                # and which one depends on whether the file exists yet.
                forced_choice = "required"
                self.context.append_user(
                    f"You have spent {self.state.research_turns} turns reading and "
                    "have written nothing. You have read enough.\n\n"
                    "Your plan set out to write " + ", ".join(outstanding) + ". "
                    + ("Write them now" if len(outstanding) > 1 else "Write it now")
                    + ", from what you already have. If a file genuinely cannot be "
                    "written, say which and why in one line and call `finish`."
                )
            else:
                self.context.append_user(
                    f"You have spent {self.state.research_turns} turns calling tools "
                    "in this phase without finishing it. That is enough to act on -- "
                    "reading more will not make the decision easier.\n\n"
                    + (
                        "Submit the plan now, or ask the developer what you cannot "
                        "infer."
                        if self.state.mode is Mode.PLANNER
                        else "Say what you have done and what you found."
                    )
                )

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

        outcome = yield from self._complete(
            tools,
            tool_choice=(
                (forced_choice or self._terminal_choice()) if answering else None
            ),
        )
        yield from self._report_wire_repairs()
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
        if not answering and not result.chat.tool_calls and tools and self._must_call_a_tool():
            self.state.forced = True
            yield Event(
                EventType.GATE,
                {"kind": "forced_tool_call", "mode": str(self.state.mode)},
            )
            narration = result.chat.content
            forced = yield from self._complete(tools, tool_choice="required")
            yield from self._report_wire_repairs()
            if forced is None:
                return
            if forced.chat.tool_calls:
                # The prose the model actually said travels with the forced
                # reply, because it has already been streamed to the panel: the
                # deltas went out as they arrived, and discarding the result they
                # belonged to displayed text the backend then silently dropped
                # (BUG L-15). The model's own turn also vanished from its
                # history, so it could not see that it had narrated and been
                # asked again.
                #
                # Prefixed rather than concatenated blindly: the forced reply
                # usually carries no prose of its own, and where it does, both
                # halves are the model's and both are worth keeping in order.
                if narration and narration not in (forced.chat.content or ""):
                    forced.chat.content = "\n\n".join(
                        part for part in (narration, forced.chat.content) if part
                    )
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

        # A reply that arrived whole clears the streak: the shorter-reply advice
        # took, and the next overrun is a new run of bad luck rather than a
        # continuation of this one.
        self.state.truncated_turns = 0

        if result.chat.tool_calls:
            self.state.research_turns += 1
            yield from self._tool_calls(result.chat.tool_calls, assistant_msg)
            return

        # No tool calls: the model has said its piece.
        yield from self._finish_turn(result)

    def _report_wire_repairs(self) -> Iterator[Event]:
        """Announce a request that had to be repaired to be legal.

        ``ContextManager.wire`` synthesises results for declared-but-unanswered
        calls rather than letting a strict endpoint reject the conversation
        (BUG L-1, L-6). That recovery keeps the run alive, and a silent recovery
        for an invariant violation is how the violation survives to the next
        release: the loop is the component that produced the invalid list, so it
        is the one that has to say so.

        Once the batch paths and the compaction cut are correct this never
        fires. If it does, the repair is the symptom and the loop is the bug.
        """
        for repair in self.context.wire_repairs:
            yield Event(
                EventType.ERROR,
                {
                    "message": f"internal: the assembled request was repaired ({repair}). "
                    "The turn was dispatched; please report this.",
                    "kind": "wire_repair",
                },
            )

    def _complete(
        self, tools: list[dict[str, Any]], *, tool_choice: str | None = None
    ) -> Iterator[Event]:
        """Dispatch one completion, or set ``self.result`` and return None.

        A generator so it can yield the compaction and error events, and so the
        caller can `yield from` it and read the result off the return value.
        """
        # One coalescer per call, and the tail flushed however it ends.
        deltas = DeltaCoalescer()

        # And something to ask it the time (BUG SH-6).
        #
        # `max_interval` exists so that a model pausing mid-sentence does not
        # leave the last few characters buffered — the coalescer's own docstring
        # calls that "the one that matters". But the deadline was only ever
        # evaluated inside `feed`, and nothing calls `feed` while the model is
        # silent, which is exactly when it needs evaluating. So the held text
        # was released by the *next* fragment, whenever that came: the panel
        # stopped mid-word for as long as the model thought, which is the "reads
        # as a hang" the interval was written to prevent.
        #
        # A daemon thread for the length of one streamed call. It costs a
        # wake-up every 40 ms while a call is in flight and nothing at all
        # between calls, and both it and `feed` drain the whole buffer under the
        # coalescer's lock, so they cannot interleave into reordered text.
        ticking = threading.Event()

        def tick() -> None:
            # Half the interval, so a deadline is noticed within one period of
            # passing. Floored, because a coalescer configured with no interval
            # at all would otherwise turn this into a spin.
            period = max(0.01, deltas.max_interval / 2)
            while not ticking.wait(period):
                self._relay(deltas.flush_due())

        ticker = threading.Thread(target=tick, name="dakcoder-deltas", daemon=True)

        def dispatch() -> TurnResult:
            return complete(
                self.context,
                self.client,
                tools=tools,
                tool_choice=tool_choice,
                session_id=self.session_id,
                on_delta=lambda fragment: self._relay(deltas.feed(fragment)),
            )

        ticker.start()
        try:
            return dispatch()
        except UnsupportedParameterError:
            # The endpoint does not take this `tool_choice`. Both uses of it here
            # are recoveries from a run that is otherwise going to loop, so
            # falling back is worth a prefill: "required" degrades to asking
            # again plainly, and a named choice degrades to `required`, which at
            # least keeps a tool call on the table.
            #
            # Deliberately *not* falling back to `tools=[]`. Measured on the live
            # endpoint: with no tools the model emits markup for `Grep` with an
            # `output_mode` parameter -- a tool from another harness, remembered
            # from training -- and the loop would serve that to a developer as an
            # answer. An unconstrained retry is a worse turn; that is a worse
            # product.
            if tool_choice is None:
                raise
            yield Event(
                EventType.GATE,
                {"kind": "tool_choice_unsupported", "value": str(tool_choice)},
            )
            tool_choice = "required" if isinstance(tool_choice, dict) else None
            return dispatch()
        except OverBudgetError as exc:
            # The context manager exists to prevent this, so reaching it means
            # compaction could not free enough. Compacting harder and retrying
            # once is worth a turn; failing the run outright is not.
            yield from self._compact(retain_pct=0.15, reason="over budget")
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
            # Stopped before the tail flush, so the last fragment is emitted
            # once, by the thread that owns the turn.
            ticking.set()
            ticker.join(timeout=1.0)
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
        if self.state.retrieval_repeats.get("search_docs", 0) >= MAX_RETRIEVAL_REPEATS:
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
                and self.state.gate_key[0] == self.router.model_mutations
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
        self.state.truncated_turns += 1
        self.state.truncations += 1
        names = ", ".join(sorted({c.name for c in incomplete}))
        cut = {c.id for c in incomplete}
        # One oversized call is a different failure from five ordinary ones, and
        # the advice for it is different too (BUG FS-2). See `_shorter_reply`.
        alone = len(result.chat.tool_calls) == 1
        for call in result.chat.tool_calls:
            if call.id in cut:
                # Which file, recovered from the prefix that did arrive.
                #
                # The message used to name only the tool, so a run whose third
                # `write_file` was cut off was told "your call to write_file
                # arrived cut off" and had to work out which call that was from
                # a transcript in which it had just said it would write three
                # files. It guessed, and it guessed that the first two had
                # landed and the third had not -- which was true that time and
                # is a coin flip in general. `path` is almost always the first
                # key in the object, so it survives the cut that removed the
                # content after it, and the loop can simply say.
                target = _salvaged(call, "path")
                subject = f"{call.name} ({target})" if target else call.name
                body = (
                    f"Your call to {subject} arrived cut off -- the arguments stop "
                    "partway through, so the call was not made and nothing was "
                    "written. Nothing is wrong with your JSON; this is what running "
                    f"into the {config_for(self.state.mode).max_tokens:,}-token output "
                    "limit looks like.\n\n"
                    "The state block at the end of this prompt lists every file this "
                    "run has actually written. Work from that rather than from what "
                    "you remember saying.\n\n" + self._shorter_reply(call.name, alone)
                )
                said = f"output limit reached mid-call; {subject} was not dispatched"
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
                {
                    "id": call.id,
                    "name": call.name,
                    "ok": False,
                    "content": said,
                    "turn": self.context.turn,
                    # Structured, not only narrated. Counting how often the
                    # output limit was hit used to mean string-matching the
                    # prose above, which is not a thing a report should have to
                    # do about its own event stream.
                    "truncated_by_output_limit": True,
                    "output_limit": config_for(self.state.mode).max_tokens,
                    "dispatched": False,
                },
            )

        # Two bounds, because one of them could be dodged (BUG FS-3).
        #
        # The streak resets on any reply that arrives whole, and a run that
        # alternates — cut off, one ordinary call, cut off again — never reaches
        # three in a row. That is not a hypothetical shape: a refused
        # `run_terminal` between two oversized writes is enough, and it is
        # exactly what a model does when it is casting about for a way to write
        # something too large. The reported transcript thrashed on turns 29 to
        # 33 and the streak never got past one.
        streak = self.state.truncated_turns >= MAX_TRUNCATED_TURNS
        total = self.state.truncations >= MAX_TRUNCATIONS
        if streak or total:
            limit = config_for(self.state.mode).max_tokens
            how = (
                f"{self.state.truncated_turns} replies in a row were"
                if streak
                else f"{self.state.truncations} replies in this run were"
            )
            self.result = RunResult(
                Outcome.UNVERIFIED if self.router.touched else Outcome.NO_PROGRESS,
                f"{how} cut off by the {limit:,}-token output limit for "
                f"{self.state.mode}. The turn the model is trying to make does not "
                "fit; narrow the task, or raise the mode's output budget"
                + self._unfinished(),
                self.context.turn,
                tuple(self.router.touched),
                self.state.last_gate,
            )
            yield Event(EventType.ERROR, {"message": self.result.summary})

    def _shorter_reply(self, tool: str, alone: bool) -> str:
        """What to actually do about a reply that did not fit.

        BUG FS-2. The advice was one paragraph for every overrun: "fewer tool
        calls in one turn, and less prose before them. One call is enough." That
        is right when a batch of five calls was cut off in the fifth. It is
        useless when the reply held *one* call whose single argument is the
        thing that does not fit, because there is nothing left to remove — and
        the reported transcript is four turns of a model following it exactly,
        making one call with no prose, and being cut off in the same place each
        time.

        A content-bearing write is the case worth naming, because the answer is
        a specific tool call rather than a general instruction to be briefer.
        """
        if alone and tool in _CHUNKABLE_WRITES:
            return (
                "One call with no prose is already as short as a reply gets, so "
                "there is nothing left to trim: the content itself is larger than "
                "one reply can carry. Write it in pieces instead. Call "
                f"{tool} with the first part, then call write_file again with "
                "append=true and the next part, and keep going until it is "
                "complete. Aim for a third of the limit per chunk. A chunk may "
                "end mid-line; nothing is inserted between them."
            )
        return (
            "Make the next reply shorter: fewer tool calls in one turn, and "
            "less prose before them. One call is enough."
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
        # One line per call, which is the thing an in-progress run makes
        # visible and the end-of-run summary cannot: a run that is climbing
        # towards the ceiling looks identical to one that is not until it
        # arrives. `estimate_error` is here because the prompt budget is
        # enforced against the estimate, so a drift is a budget being enforced
        # against a number that means less than it says.
        log.info(
            "turn %d %s: prompt %d/%d tokens (%.0f%%), completion %d, cached %d, "
            "estimate x%.3f",
            self.context.turn,
            self.state.mode,
            result.actual_prompt_tokens,
            usage.budget,
            usage.used_pct,
            result.chat.usage.completion_tokens,
            result.chat.usage.cached_tokens or 0,
            result.estimate_error,
        )

        if reasoning_leaked(result):
            # Non-zero reasoning in a thinking-off mode means
            # chat_template_kwargs is not reaching the model: ~15x the latency
            # for no quality gain, presenting as the agent simply being slow.
            payload["reasoning_leaked"] = result.chat.usage.reasoning_tokens
        yield Event(EventType.USAGE, payload)

    # -- tools ------------------------------------------------------------

    def _answer_unrun(self, pending: Sequence[ToolCall], reason: str) -> None:
        """Answer the calls a batch will never dispatch.

        The assistant message declaring every call in the batch is already in
        the working set, and the wire format is not "results for the calls that
        ran" -- it is *one* ``role: "tool"`` message per declared
        ``tool_call_id``, for the rest of the conversation. A declared call left
        unanswered is not a cosmetic gap: a strict OpenAI-compatible endpoint
        rejects the whole message list, so the orphan poisons every later turn
        of the session and every follow-up built on the same context.

        Three paths reach here -- cancellation mid-batch, a terminal tool that
        ended the phase with calls behind it, and the forced-terminal cap -- and
        each used to be its own discipline. One of the three had it; the other
        two returned (BUG L-1). It is one helper now so a fourth path cannot get
        it wrong by omission.
        """
        for call in pending:
            self.context.append_tool_result(
                call.name,
                f"{call.name} was not run: {reason}",
                tool_call_id=call.id,
            )

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
                self._answer_unrun(
                    calls[index:],
                    "the developer stopped the run before this call was dispatched.",
                )
                self.result = self._abort()
                return

            fingerprint = _fingerprint(call)
            args = _safe_args(call)

            if intercepted := self._intercept(call, fingerprint):
                body, said, intercept_kind = intercepted
                # The previous answer to this same question is stubbed out
                # before the new one is appended.
                #
                # Not deleted -- the message keeps its place and its
                # `tool_call_id`, so nothing is orphaned. What is removed is the
                # *pattern*: N identical (call -> "answered from the previous
                # result") pairs sitting in history are a few-shot demonstration
                # of exactly the behaviour the answer is asking the model to
                # stop, and the transcript wins that argument. Measured on the
                # live endpoint: one pair and the model moves on 5/5; two and it
                # repeats forever 5/5. A field run made the same `git_ops
                # commit` call seven times and another the same `search_repo`
                # eight times, each intercepted correctly and each answered into
                # a transcript that told it to do it again.
                if (prior := self.state.echoes.get(fingerprint)) is not None:
                    self.context.supersede(
                        prior,
                        f"[{call.name} was asked again with these arguments and answered "
                        "from the earlier result; the newest answer is below]",
                    )
                self.state.echoes[fingerprint] = self.context.append_tool_result(
                    call.name, body, tool_call_id=call.id
                )
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": True,
                        "turn": self.context.turn,
                        "intercepted": True,
                        # *Which* ledger answered, because they are different
                        # findings. A cached repeat is the model being slow to
                        # move on; a refused re-read is the context window
                        # costing a turn, and only one of those is evidence
                        # about the size of the window.
                        "intercept": intercept_kind,
                        "arguments": args,
                        "content": said,
                    },
                )
                continue

            yield Event(
                EventType.TOOL_CALL,
                # The turn travels with it. Tool events carried no turn id, so a
                # transcript could not be grouped by turn without inferring it
                # from the position of the last `turn_start` — which a reconnect
                # or a dropped frame makes wrong (AUDIT §Observability).
                {
                    "id": call.id,
                    "name": call.name,
                    "arguments": args,
                    "turn": self.context.turn,
                },
            )

            outcome = self.router.dispatch(call.name, call.arguments, mode=self.state.mode)

            if isinstance(outcome, ApprovalRequest):
                request = outcome
                # Registered before it is announced, so a client that answers
                # the instant it reads the event cannot arrive before the
                # approval exists.
                self.on_pending(request)
                yield Event(
                    EventType.TOOL_PENDING,
                    {**request.as_dict(), "turn": self.context.turn},
                )
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
            mutated = mutated or bool(outcome.mutations)
            if call.name == "go_mod":
                self.state.dependencies_changed = True

            self.state.seen_calls[fingerprint] = self.state.seen_calls.get(fingerprint, 0) + 1
            # A terminal call is a transition, not a question, so it is never
            # answered from the ledger.
            #
            # It was, and the effect was silent: a model that called `finish`
            # twice with the same arguments -- which is exactly what a model
            # repeating an excuse does -- had the second one answered with
            # "answered from the previous result; use it and move to the next
            # step". The phase did not end, `_phase_ended` never ran, and the
            # refusal counter that decides whether to believe an abandonment
            # never advanced. Caching the answer to "are we done?" cannot be
            # right: the answer depends on the workspace, not on the arguments.
            if not refused_by_mode and call.name not in _TERMINAL:
                whole = outcome.for_model()
                self.state.last_results[fingerprint] = whole[:CACHED_RESULT_CHARS]
                # Remembered, so the replay can say so. A cache cut at 6,000
                # characters and replayed as "that is the current answer" told
                # the model it had the whole result when it had a third of one,
                # and the reasonable response to an answer that seems to be
                # missing something is to ask again (BUG L-17).
                if len(whole) > CACHED_RESULT_CHARS:
                    self.state.partial_results[fingerprint] = len(whole)
                else:
                    self.state.partial_results.pop(fingerprint, None)
            if outcome.truncated:
                self.state.truncated_at[fingerprint] = _volume(call)
            else:
                self.state.truncated_at.pop(fingerprint, None)
            if reason := outcome.meta.get("dead_end"):
                self.state.dead_ends[fingerprint] = str(reason)
                self._rule_out(f"{call.name}: {reason}")

            # A voluntary change of strategy. Not a phase transition: the acting
            # mode replaces the rest of its plan and carries straight on, still
            # holding the write tools. The loop's own replan -- back to the
            # Planner after a second failing gate -- is the involuntary version
            # for a model that has not noticed it needs one.
            if outcome.ok and outcome.meta.get("control") == "revise":
                yield from self._plan_revised(outcome)

            # A file that was just written is worth reading again.
            for mutation in outcome.mutations:
                self.state.reads.pop(mutation.path, None)

            slice_path, slice_range = _slice_path(call, outcome)
            appended = self.context.append_tool_result(
                call.name,
                outcome.for_model(),
                tool_call_id=call.id,
                path=slice_path,
                line_range=slice_range,
            )
            if slice_path is not None:
                # Recorded from what is *in context*, not from what the tool
                # returned and not from what the model asked for. Three
                # different numbers when a large file meets the 48k insertion
                # cap, and the ledger's only job is to answer "has the model
                # seen these lines" — so it is written from the message that
                # holds them (BUG L-8). `append_tool_result` reports the span
                # that survived the cap; `None` means none of it did, and the
                # read is recorded as having delivered nothing.
                self._record_read(
                    slice_path,
                    appended.line_range,
                    int(outcome.meta.get("lines") or 0),
                    delivered=appended.line_range is not None or slice_range is None,
                )

            # Did that tell the run anything it did not already have?
            #
            # This is the question `informed` was named for and never asked. It
            # used to be "was it dispatched and not mode-refused", so a search
            # that found nothing, a search that found what an earlier search had
            # already found, and a report identical to one three turns back all
            # counted as progress -- and `stalled_turns`, which gates the whole
            # `must_answer` rescue, reset on every one of them. The rescue was
            # wired to a sensor that read OK whenever the model typed something
            # new.
            added, note = self._novelty(call, outcome, refused_by_mode=refused_by_mode)
            informed += 1 if added else 0
            if note:
                # As a user message. It carries no `tool_call_id` because no
                # tool produced it, and a `role: tool` message without one is
                # malformed on the wire and a lie in the transcript -- the old
                # loop had 17 such call sites, teaching the model that
                # `go_build` returns paragraphs of instructions.
                self.context.append_user(note)

            yield Event(
                EventType.TOOL_RESULT,
                {
                    "id": call.id,
                    "name": call.name,
                    "turn": self.context.turn,
                    **outcome.as_dict(),
                },
            )

            # A phase ends on its own tool call, not on its prose.
            if call.name in _TERMINAL:
                if outcome.ok:
                    # The phase is over, so nothing behind this call will run --
                    # but every one of them was declared in the same assistant
                    # message and every one of them still needs a result.
                    self._answer_unrun(
                        calls[index + 1 :],
                        f"the {call.name} call in the same reply ended the phase.",
                    )
                    yield from self._phase_ended(call.name, outcome)
                    return
                # It reached for the exit and missed -- arguments the schema
                # refused. Counted, so the next force picks a tool that cannot
                # be got wrong, and so a run cannot spend its budget being made
                # to call something it keeps failing to call.
                self.state.forced_terminal += 1
                if self.state.forced_terminal >= MAX_FORCED_TERMINAL:
                    self._answer_unrun(
                        calls[index + 1 :],
                        f"the run ended when {call.name} was refused for the "
                        f"{self.state.forced_terminal}th time.",
                    )
                    self.result = RunResult(
                        Outcome.NO_PROGRESS,
                        f"asked {self.state.forced_terminal} times to end the phase "
                        f"with {call.name} and the arguments were refused each time: "
                        f"{outcome.for_model()[:200]}",
                        self.context.turn,
                        tuple(self.router.touched),
                        self.state.last_gate,
                    )
                    return
                self.state.must_answer = True
                self.state.answer_because = (
                    f"Your `{call.name}` call was refused: {outcome.for_model()[:300]}"
                    + (f"\n\n{outcome.fix}" if outcome.fix else "")
                    + "\n\nSend it again with arguments the schema accepts. Nothing "
                    "else about the run has changed."
                )

        # Turn-level progress, judged on the batch rather than on any one call.
        # A batch that dispatched nothing -- every call a verbatim repeat or a
        # known dead end -- moved the run nowhere, however many calls it held.
        if informed > 0 or mutated:
            self.state.stalled_turns = 0
        else:
            self.state.stalled_turns += 1
            # Made to answer before it is killed.
            #
            # The old ending was six stalled turns and `no_progress`, and two
            # field runs reached it with the work already done: one had written
            # nine files and committed them and then asked `git_ops commit`
            # seven times; the other had read what it needed and asked one
            # `search_repo` eight times. Every repeat was intercepted correctly
            # and every answer said "move to the next step", and the model had
            # no next step -- what it needed was to stop calling tools, which is
            # the one thing prose does and the one thing nothing asked it for.
            #
            # So the turn after a stalled one is dispatched with tool calls
            # forbidden. Prose is then the only reply available, and the loop
            # already knows what prose means: in ASK it is the answer, in AGENT
            # it is "I am done, run the gate". Both are decisions. This is the
            # mirror of the `tool_choice: "required"` re-ask, using the same
            # primitive in the other direction.
            if self.state.stalled_turns >= STALLS_BEFORE_ANSWER:
                self.state.must_answer = True
            if self.state.stalled_turns >= MAX_STALLED_TURNS:
                # A run with a plan and a replan left is not out of moves; it is
                # out of moves *within this plan*. Ending it here reports
                # "no progress" about a strategy rather than about the task,
                # which is the same mis-headline `_stalled` was written to fix
                # one level down.
                if self.state.plan and self.state.replans < MAX_REPLANS:
                    self._rule_out(
                        f"{MAX_STALLED_TURNS} turns of this plan asked for nothing new"
                    )
                    report = self.state.last_gate
                    yield from self._replan(
                        report if report is not None and not report.ok else None
                    )
                    return
                self.result = self._stalled()
                return

        if mutated:
            # Writing is not research. The fence exists to stop a phase spent
            # reading and never deciding; a turn that changed a file has decided,
            # and counting it drove the acting phase into a wall at ~12 turns on
            # a product that advertises 400 (BUG L-2).
            self.state.research_turns = 0
            yield from self._inner_loop()

    def _stalled(self) -> RunResult:
        """End a run that stopped asking for anything new, and say what it did.

        `no_progress` on its own is a report about the *loop*, and in both field
        transcripts it was wrong about the run: one had written nine files,
        passed the build, regenerated the swagger docs and committed, and was
        reported to the developer as having made no progress. What the developer
        needs to know is what is on disk and what the gate said about it.
        """
        worst_key, worst_n = max(
            self.state.seen_calls.items(), key=lambda item: item[1], default=("", 0)
        )
        detail = (
            f"; {worst_key.split(':', 1)[0]} was asked {worst_n} times" if worst_n > 1 else ""
        )
        stuck = (
            f"the last {MAX_STALLED_TURNS} tool-calling turns only repeated earlier "
            f"calls or known dead ends, and added nothing new{detail}"
        )

        report = self.state.last_gate
        if report is not None and not report.ok:
            # The gate is the more useful headline: the run stopped, and there
            # is a named reason it had not finished.
            return RunResult(
                Outcome.UNVERIFIED,
                f"the gate did not come clean and the run stopped making progress"
                + (f"; blocked at {report.blocked_by.name}" if report.blocked_by else "")
                + f". {stuck}"
                + self._unfinished(),
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
        if self.router.touched:
            files = "\n".join(f"  - {p}" for p in self.router.touched)
            verdict = (
                "the gate has not run on them yet"
                if report is None
                else "the gate was clean when it last ran"
            )
            return RunResult(
                Outcome.NO_PROGRESS,
                f"{stuck}. {len(self.router.touched)} file(s) were changed and "
                f"{verdict}:\n{files}",
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
        return RunResult(
            Outcome.NO_PROGRESS,
            stuck,
            self.context.turn,
            tuple(self.router.touched),
            report,
        )

    def _intercept(self, call: ToolCall, fingerprint: str) -> tuple[str, str, str] | None:
        """What to answer without dispatching, or None to dispatch.

        Returns ``(body, said, kind)``. The ``kind`` names *which* ledger
        answered — ``dead_end``, ``cached`` or ``re_read`` — because they are
        different findings and the event stream reported all three as a single
        ``intercepted: true``. Only one of them is evidence about the size of
        the context window: a refused re-read is a turn spent because content
        the model needed had to be kept out of the prompt. The other two are the
        model being slow to move on, which is a different problem.

        Three ledgers, and none of them ends a run. A model being slow to take a
        hint costs a turn; it is not a reason to throw away twenty-five, which
        is what the old detector did on a third read of a file that was not
        there, one turn after being told correctly what to do instead.
        """
        # A transition always dispatches.
        #
        # The invariant, stated where it is enforced rather than only where the
        # ledger is written. `finish`, `submit_plan` and `ask_developer` do not
        # answer a question -- they change what the run is doing -- and their
        # result depends on the workspace rather than on their arguments. A
        # model repeating an excuse sends the same `finish` twice, and the
        # second used to be answered "from the previous result; use it and move
        # to the next step": the phase did not end, `_phase_ended` never ran,
        # and the counter deciding whether to believe an abandonment never
        # advanced.
        if call.name in _TERMINAL:
            return None

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
                "dead_end",
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
            whole = self.state.partial_results.get(fingerprint)
            if whole is None:
                body = (
                    f"{call.name} returned:\n\n{cached}\n\n"
                    "-- that is the current answer. The call ran earlier, nothing in the "
                    "workspace has changed since, so it was answered from that result "
                    "rather than dispatched again. Use it and move to the next step; if it "
                    "does not tell you what you need, ask something different or say "
                    "plainly what is blocking you."
                )
            else:
                body = (
                    f"{call.name} returned (the first {len(cached):,} characters of "
                    f"{whole:,}):\n\n{cached}\n\n"
                    "-- the call ran earlier and nothing in the workspace has changed "
                    "since, so this is the earlier result rather than a fresh dispatch, "
                    "and only its beginning was kept. Asking again returns this same "
                    "extract. If you need the part that is missing, ask something "
                    "narrower -- a line range, a scoped path, a tighter pattern -- so "
                    "the answer fits."
                    # And, for a read, *which* narrower. See `_unseen_hint`.
                    + self._unseen_hint(call)
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
                "cached",
            )

        # A read that asks for lines already delivered. `_fingerprint` covers
        # the whole call, so one file read at four ranges is four different
        # calls and invisible to the ledger above; this asks the question the
        # fingerprint cannot, and asks it about coverage rather than about how
        # many times the file has come up.
        if why := self._re_reading(call):
            return why, "asks for lines already in context; not re-read", "re_read"
        return None

    def _unseen_hint(self, call: ToolCall) -> str:
        """The first line range of this file the run has *not* been given, if any.

        "Ask something narrower -- a line range" is correct advice and not
        actionable: a model that has been shown the first hundred lines of a
        6,571-line handler does not know where the rest of it is, and every
        number it could guess is a guess. So it asks the same question again,
        gets the same extract, and the turn is spent.

        A field transcript died on exactly that -- four `read_file` calls on one
        large handler, two of them answered from this cache, then a forced
        `finish` with no tests written. The loop held the answer the whole time:
        `_ReadLedger` records the intervals actually delivered and the file's
        length, so "you have seen 1-102 of 6,571; ask for 103 onward" is a fact
        it can simply state.

        Read from the *live* coverage rather than the stored ledger, so a span
        compaction evicted is offered again rather than described as seen.
        """
        if call.name != "read_file":
            return ""
        parsed = _safe_args(call)
        path = parsed.get("path") if isinstance(parsed, dict) else None
        if not isinstance(path, str) or not path:
            return ""
        recorded = self.state.reads.get(path)
        if recorded is None or not recorded.lines:
            return ""
        ledger = self._live_reads(path, recorded)
        total = ledger.lines
        if ledger.covers(1, total):
            return ""

        # The first gap, which is where a model working front-to-back wants to
        # go next. Naming one is the point; a list of every gap is the same
        # problem in a longer form.
        start = 1
        for low, high in ledger.covered:
            if low > start:
                break
            start = max(start, high + 1)
        if start > total:
            return ""
        end = min(total, start + LINES_PER_READ * 4 - 1)
        # Live coverage can be empty even though the ledger recorded reads: the
        # spans were evicted. "You have been given 0 lines (earlier reads
        # covering )" is the empty summary rendered anyway, so the two cases get
        # different sentences.
        held = (
            f"you have been given {ledger.covered_lines():,} ({ledger.summary()})"
            if ledger.covered
            else "none of what you read of it is still in context"
        )
        return (
            f"\n\nOf this file's {total:,} lines {held}. The next part you have "
            f"not seen starts at line {start:,} -- call "
            f'`read_file(path="{path}", start={start}, end={end})` to read it. If you '
            "are looking for one function rather than working through the file, "
            "`search_repo` for its name and read the range around the line it reports."
        )

    def _gate_wants_an_edit(self) -> bool:
        """Whether a failing gate is currently asking for a change that has not come.

        The one situation where forcing the phase's terminal tool contradicts
        the context the model is reading: the gate report sits in the transcript
        saying "make the edit", and the request forbids every tool but `finish`.
        """
        report = self.state.last_gate
        return (
            self.state.mode is Mode.AGENT
            and report is not None
            and not report.ok
            and self.state.gate_failures <= MAX_GATE_FAILURES
        )

    def _terminal_choice(self) -> dict[str, Any]:
        """The tool this mode is forced to call when it must stop.

        Named rather than ``"required"``: required would let it pick a research
        tool and carry on, which is the behaviour being escaped.

        The Planner is pointed at ``submit_plan`` first, because that is the
        outcome the developer asked for and a plan submitted under protest is a
        better thing to argue with than nineteen more turns of reading. **Once**:
        if those arguments do not satisfy the schema, the second force is
        ``finish``, whose schema is one required string and which therefore
        cannot fail the same way. Forcing a call that keeps being refused is the
        loop this whole mechanism exists to escape, arriving through the escape.
        """
        if self.state.mode is Mode.PLANNER and not self.state.forced_terminal:
            return {"type": "function", "function": {"name": "submit_plan"}}
        return _FORCE_FINISH

    def _phase_ended(self, tool: str, outcome: ToolResult) -> Iterator[Event]:
        """Act on the tool call that ends a phase.

        A typed event, so there is nothing to interpret.

        ``submit_plan`` pins the plan and hands the run to the acting mode.
        ``ask_developer`` ends the run with the questions on screen, where the
        developer's answer arrives as a follow-up on this transcript.
        ``finish`` is the answer: in ``ask`` it ends the run, and in ``agent`` it
        means "I am done" and hands over to the gate, which is the same thing
        prose used to mean there and is the thing this model can reliably say.
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

        if tool == "finish":
            answer = str(outcome.meta.get("answer") or "").strip()
            blocked = str(outcome.meta.get("blocked") or "").strip()

            # A `finish` that walks away from the plan is sent back, once.
            #
            # Giving the acting mode a terminal tool fixed the loop and opened
            # this: finishing became the easiest move available, and measured
            # live, two runs in three called `finish` on their first acting turn
            # -- "I have gathered all the necessary details to write the
            # migration plan" -- without writing anything. Honest, and useless.
            #
            # Bounded at MAX_FINISH_REFUSALS because this reads paths out of the
            # plan rather than out of the work: the model may have decided a
            # step is unnecessary, and it is entitled to say so and be believed.
            # What it is not entitled to is silence.
            missing = (
                self._unwritten_targets() if self.state.mode is Mode.AGENT else []
            )
            # What "believed" is conditional on.
            #
            # The refusal below makes a specific promise -- say why in `blocked`
            # and it is taken at face value -- and the loop did not keep it. A
            # second `finish` was believed whatever it said, so a model that
            # simply repeated "I need to read the handler source files, then
            # write the tests" ended the run on its own statement of intent,
            # with the reason field empty and the promise unmet.
            #
            # A `blocked` that names an obstacle is a decision, and a decision
            # this loop is not positioned to overrule: the model may well be
            # right that a file should not be written. Silence is not a decision.
            # So an explained abandonment costs one refusal and an unexplained
            # one costs two, and the second refusal says plainly what is missing
            # rather than repeating the first.
            limit = MAX_FINISH_REFUSALS if blocked else MAX_UNEXPLAINED_FINISH_REFUSALS
            if missing and self.state.finish_refused < limit:
                self.state.finish_refused += 1
                if self.state.finish_refused > MAX_FINISH_REFUSALS:
                    self.context.append_user(
                        "Still not written: " + ", ".join(missing) + ".\n\n"
                        "You have now called `finish` twice with `blocked` empty, which "
                        "says the work is done. It is not -- the state block at the end "
                        "of this prompt lists what this run has actually written.\n\n"
                        "Two replies end this properly. Write the "
                        + ("files" if len(missing) > 1 else "file")
                        + " with `write_file`, or call `finish` with the obstacle in "
                        "`blocked` -- one line naming what stopped you. An empty "
                        "`blocked` will not be read as a reason again."
                    )
                    return
                self.context.append_user(
                    "Not yet. Your plan set out to write " + ", ".join(missing)
                    + " and " + ("none of them have" if len(missing) > 1 else "it has not")
                    + " been written.\n\n"
                    + "You have read enough to write "
                    + ("them" if len(missing) > 1 else "it")
                    + " now. Do that. If a file genuinely should not be written after "
                    "all, call `finish` again and say which and why in `blocked` -- "
                    "that will be taken at face value."
                )
                return

            # Emitted as the assistant's own words, because that is what it is:
            # the developer reads this, and a `finish` whose answer only appeared
            # inside a tool result would be an answer nobody was shown.
            if answer:
                yield Event(EventType.ASSISTANT, {"text": answer})
            if self.state.mode is Mode.AGENT:
                # The acting mode saying it is done is the gate's cue, exactly as
                # a tool-free turn was. The gate still cannot be skipped.
                yield from self._verify()
                return
            self.result = RunResult(
                Outcome.DONE,
                (f"answered; blocked on: {blocked}" if blocked else "answered"),
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        self.state.research_turns = 0
        self.state.forced_terminal = 0
        # And the narration re-ask. It is once per *phase*, which is what the
        # reasoning behind it was always about: a Planner that has decided there
        # is nothing to plan should not be forced twice over the same decision.
        # It was scoped to the run, so a Planner that consumed it handed the
        # acting mode a phase with no narration recovery at all — and the acting
        # mode is where narration costs the most, because a "Making the edit
        # now" with no tool call is a turn in which nothing was edited
        # (prior-audit TC-4).
        self.state.forced = False
        self.state.plan = self._normalise_plan(steps_from_meta(dict(outcome.meta)))
        self.state.plan_summary = str(outcome.meta.get("summary") or "")
        # A first plan has nothing done in it; a *replanned* one is submitted
        # into a workspace this run has already written to, and the steps it
        # keeps are already finished. Syncing here rather than waiting for the
        # next turn means the plan the developer is shown, and the plan the
        # PLAN event carries, are the plan as it actually stands.
        self._sync_plan()
        steps = self.state.plan
        rendered = "\n".join(step.rendered(i) for i, step in enumerate(steps, 1))
        if self.state.plan_summary:
            rendered = f"{self.state.plan_summary}\n\n{rendered}"
        self.context.set_plan(rendered)
        # The typed steps travel with the rendered text, not instead of it. The
        # panel used to parse `text` with a regex and show a dash for every
        # step's status, with a footnote conceding that "no field on the wire
        # carries it" and that inferring it "would be a guess presented as a
        # fact". It is carried now, and it is the same tuple the loop reasons
        # from -- so the panel and the model cannot disagree about what is done.
        yield Event(
            EventType.PLAN,
            {"text": rendered, "steps": len(steps), "plan": control.as_meta(steps)},
        )
        self._switch(Mode.AGENT)

    def _plan_revised(self, outcome: ToolResult) -> Iterator[Event]:
        """Adopt a plan the acting mode replaced mid-phase.

        The steps carry their own statuses -- ``steps_from_meta`` round-trips
        them -- so a revision that keeps finished work keeps it finished. The
        next ``_sync_plan`` re-derives everything except ``skipped`` from the
        workspace anyway, so a model that claims a step is done when the file was
        never written is corrected on the following turn rather than believed.

        ``research_turns`` resets. Revising is a decision, and the fence at
        ``MAX_RESEARCH_TURNS`` exists to stop a phase that never decides
        anything; charging a phase for the turn on which it changed course is
        the same mistake as charging it for the turn on which it wrote a file.
        """
        steps = self._normalise_plan(steps_from_meta(dict(outcome.meta)))
        if not steps:
            return
        self.state.plan = steps
        if summary := str(outcome.meta.get("summary") or "").strip():
            self.state.plan_summary = summary
        if ruled := str(outcome.meta.get("ruled_out") or "").strip():
            self._rule_out(ruled)
        self.state.research_turns = 0
        self._sync_plan()
        rendered = "\n".join(step.rendered(i) for i, step in enumerate(self.state.plan, 1))
        self.context.set_plan(rendered)
        yield Event(
            EventType.PLAN,
            {
                "text": rendered,
                "steps": len(self.state.plan),
                "plan": control.as_meta(self.state.plan),
                "revised": True,
                "ruled_out": str(outcome.meta.get("ruled_out") or ""),
            },
        )

    def _replan(self, report: GateReport | None) -> Iterator[Event]:
        """Hand the run back to the Planner when the acting mode has run out of route.

        The move the loop did not have. Every other response to a failing gate
        was a *stop* -- the gate-failure budget, the stall ceiling, a forced
        `finish` -- so a plan that turned out to be wrong had two outcomes: end
        the run, or send "fix what it found" a third time to a mode that had
        already read the report twice. The gate is a function of the files, and
        a model that has twice failed to produce the files it needs is not going
        to be rescued by being asked a third time in the same words.

        What makes this a replan rather than a re-roll is the record. The gate's
        blocker goes into ``ruled_out``, which the state block re-sends on every
        turn, so the Planner is planning against an explicit account of what has
        already failed. Without that it would be the same model, the same
        context and a fresh guess -- and the guess lands back on the approach it
        just abandoned. This is the whole reason ``revise_plan`` requires
        ``ruled_out`` as well.

        The gate-failure budget resets, and that is deliberate: it counts
        attempts at *one* strategy, and this is a different one. The bound that
        does not reset is ``MAX_REPLANS``.
        """
        self.state.replans += 1
        # Two callers, two different findings, and they must not be reported as
        # one. A failing gate has a named blocker and a report worth re-reading;
        # a stall has neither -- it is the model asking for what it already has
        # -- and rendering an empty GateReport at it would print "gate: PASS"
        # into a message explaining why the run is being replanned.
        blocked = report.blocked_by if report is not None else None
        blocker = blocked.name if blocked else ""
        if blocker:
            self._rule_out(
                f"the plan as written could not clear {blocker} "
                f"in {self.state.gate_failures} attempt(s)"
            )
            opening = (
                f"{self.state.gate_failures} attempts at this plan have not cleared "
                f"the gate; it is still blocked at {blocker}.\n\n{report.summary()}"
            )
        else:
            opening = (
                "This plan has stopped going anywhere -- the last "
                f"{self.state.stalled_turns} tool-calling turns asked for nothing "
                "you had not already been given."
            )

        # Everything that counts attempts at the abandoned strategy. Left
        # standing, `_gate_stalled` would end the run on the Planner's first
        # read-only turn -- it counts turns that changed no file, and a planning
        # phase changes no files by construction.
        self.state.gate_failures = 0
        self.state.gate_mutations = self.router.model_mutations
        self.state.idle_since_gate = 0
        self.state.research_turns = 0
        self.state.forced_terminal = 0
        self.state.finish_refused = 0
        self.state.stalled_turns = 0
        self.state.must_answer = False
        self.state.answer_because = ""

        done = [s.file for s in self.state.plan if s.status == control.DONE]
        self.context.append_user(
            f"{opening}\n\nYou are back in the planning phase.\n\n"
            + (
                "Work already on disk and accepted: " + ", ".join(done) + ". Do not "
                "plan to write those again.\n\n"
                if done
                else ""
            )
            + "Plan a different approach to what is left, and submit it. If the task "
            "genuinely cannot be done in this repository, call `ask_developer` with "
            "the decision you need, or `finish` saying what is in the way -- both are "
            "better than another attempt at the same route."
        )
        yield Event(
            EventType.GATE,
            {
                "kind": "replan",
                "attempt": self.state.replans,
                "blocked_by": blocker or "no progress",
            },
        )
        self._switch(Mode.PLANNER)

    def _inner_loop(self) -> Iterator[Event]:
        """Format and lint what was just written, sub-second.

        Its whole purpose is to put a problem the edit *introduced* in front of
        the model while the edit is still what it is thinking about. Anything
        else it says is noise, and noise here is uniquely expensive because this
        runs after every edit batch.

        It was not filtering at all. On a legacy service, one edit to
        `core/domain/objection.go` produced a thousand-token report headlined
        "199 blocking and 480 advisory findings across 49 files", with examples
        from `handler/paogen.go` -- a file the run never opened. The model read
        that as a mountain of work in its own change and set about fixing it,
        which is where "code written in 20 turns, verifier running to 85" comes
        from.

        Two filters, and between them they take the common case to nothing.
        `_render_lint` no longer quotes files outside the change. And the
        run-start baseline is consulted here as well as at the gate: a legacy
        file's 166 pre-existing `domain-tags` violations are not news, and
        repeating them after every edit teaches the model that its own work is
        the problem.
        """
        report = inner_loop(self.router, self.router.touched)
        yield Event(EventType.GATE, {"kind": "inner", **report.as_dict()})
        if report.ok and not report.warnings:
            return

        lint = next((r for r in report.results if r.name == "rules_lint"), None)
        if lint is not None and not lint.ok and self._lint_is_old_news(lint):
            # Everything it found was already being done in this service before
            # the run started. Say nothing: the gate will still report it, once,
            # at the end, where a summary belongs.
            return

        self.context.append_user(
            "The formatter and the contract linter ran on what you just "
            f"changed:\n\n{report.summary()}"
        )

    def _lint_is_old_news(self, result: StageResult) -> bool:
        """Whether an inner-loop lint found only what the service already does.

        The same judgement the gate makes, asked one level down and off the same
        baseline. It leans on the rule classes rather than the exact keys for the
        reason the gate does: an edit moves line numbers and a new file has no
        history, so key comparison alone excuses nothing on the work this agent
        actually does.
        """
        return bool(result.findings) and self.state.baseline.excuses(
            "rules_lint", result.findings
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
            # A planner that will not plan, and two very different runs reach it.
            #
            # On a *first* pass it is a legitimate answer: the request turned out
            # not to need a change, nothing has been written, and the honest
            # thing is to say so rather than manufacture a plan and run a gate on
            # it.
            #
            # After a replan it is none of those things. Files are on disk, a
            # gate has failed on them, and the run has already abandoned one
            # strategy -- so "nothing was executed and nothing was touched" is
            # false on both clauses, and reporting DONE would tell the developer
            # a run that left a failing workspace had succeeded. The replan is
            # what makes this reachable, so it is the replan's job to keep the
            # report honest.
            if self.router.mutations and self.state.replans:
                self.result = RunResult(
                    Outcome.UNVERIFIED,
                    "the acting phase could not clear the gate and the replan "
                    "produced no new plan; the reply describes the problem rather "
                    "than proposing a way round it" + self._unfinished(),
                    self.context.turn,
                    tuple(self.router.touched),
                    self.state.last_gate,
                )
                return
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
            # A run that wrote nothing cannot fail the gate -- but "nothing was
            # changed" and "nothing needed changing" are different claims, and
            # the developer acts on the second. A plan that named files and
            # wrote none of them is an *unstarted* run, and saying so is the
            # difference between a report and a shrug.
            missing = self._unwritten_targets()
            if missing:
                self.result = RunResult(
                    Outcome.NO_PROGRESS,
                    "the plan set out to write " + ", ".join(missing) + " and "
                    + ("none of them were" if len(missing) > 1 else "it was not")
                    + " written, so there was nothing to verify. The run read the "
                    "repository and stopped short of the work",
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return
            self.result = RunResult(
                Outcome.DONE,
                "nothing was changed, so there was nothing to verify. If work was "
                "wanted here, say what should change and it will be done",
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        key = (self.router.model_mutations, tuple(self.router.touched))
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
        # The verdict is new information about the plan, so the plan is resynced
        # here rather than waiting for the next turn to do it. A run that ends on
        # this gate has no next turn, and `_done_summary` and `_unfinished` are
        # both read out of plan state -- so without this the last thing the
        # developer is told is computed from statuses one gate out of date.
        self._sync_plan()
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
        self.state.gate_mutations = self.router.model_mutations
        self.state.idle_since_gate = 0
        # A gate verdict is new information and the work it asks for is a fresh
        # piece of work. Carrying the phase's research count across it is what
        # made a gate failure at turn 13 unfixable (BUG L-2); the run is still
        # bounded, by MAX_GATE_FAILURES and by `_gate_stalled`, both of which
        # count turns that changed nothing.
        self.state.research_turns = 0

        # Before the budget is spent: is a different approach still available?
        #
        # This is the one branch that turns a failing gate into something other
        # than another attempt at the same thing. It fires before
        # MAX_GATE_FAILURES, because arriving at the ceiling means the run is
        # ending, and a strategy change is only worth anything while there are
        # turns left to spend on it.
        if (
            self.state.gate_failures >= REPLAN_AFTER_GATE_FAILURES
            and self.state.replans < MAX_REPLANS
            and self.state.plan
        ):
            yield from self._replan(report)
            return

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
                "Fix what it found. Anything it marks advisory, and anything it lists "
                "under \"Already failing before this run\", was broken before you "
                "touched the workspace: it is not about this change and not yours to "
                "fix, so do not edit those files to clear it."
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

    def _gate_stalled(self) -> str:
        """Why a run standing on a failing gate should stop, or ``""``.

        The hole this closes is narrow and it swallowed a whole run. The
        gate-failure budget lives in `_gate_failed`, which is reached only from
        `_verify`, which is reached only from a turn that called **no** tool. So
        a model that answers a blocked gate by calling tools -- any tools -- is
        never counted against that budget, never re-asked, and never stopped.

        A field transcript did exactly that: the gate blocked at `rules_lint` on
        turn 45 and the model spent turns 46 to 65 calling `go_build`,
        `git_status`, `run_terminal` and finally `git_ops commit` seven times,
        with `gate_failures` stuck at 1 the whole way. It ended `no_progress` at
        the turn cap, which named the wrong thing entirely: the run had done its
        work and was standing in front of a gate nobody had told it it could not
        clear.

        Counted in turns that changed nothing, because the gate is a function of
        the files: given no new edit its verdict cannot move, so a run that is
        not editing is not going to clear it however many tools it calls.
        """
        report = self.state.last_gate
        if report is None or report.ok:
            return ""
        if self.router.model_mutations != self.state.gate_mutations:
            self.state.gate_mutations = self.router.model_mutations
            self.state.idle_since_gate = 0
            return ""
        self.state.idle_since_gate += 1
        if self.state.idle_since_gate <= MAX_GATE_FAILURES:
            return ""
        blocked = f"; blocked at {report.blocked_by.name}" if report.blocked_by else ""
        return (
            f"the gate did not come clean{blocked}, and the last "
            f"{self.state.idle_since_gate - 1} turns changed no file, so its verdict "
            "cannot have moved" + self._unfinished()
        )

    def carry_from(self, previous: "AgentLoop") -> None:
        """Inherit the previous message's ledgers, the way the context is inherited.

        The context manager already carries across a follow-up -- every file
        read, every answer given -- and ``_State`` did not, because it is built
        with the loop and a loop is built per message. So the working set
        remembered a search and the ledger that knows it was exhausted did not.

        A field transcript shows the cost. A run ended on a `search_repo` it had
        asked eight times; the developer typed "where is the plan?"; the new run
        started with empty ledgers, dispatched the identical search, was answered
        from nothing, and reproduced the same loop over the same four turns. The
        transcript makes it look like the agent has no memory. It has memory and
        no *record*.

        Only what remains true between messages. ``last_results`` and
        ``dead_ends`` are deliberately **not** carried: the developer edits files
        between messages and nothing here watches for that, so a cached answer
        could outlive the thing that made it true. What carries is the fact that
        a question has already been asked, which does not go stale -- and the
        read coverage, which the context still holds the reads for.

        **The Router comes too.** It was rebuilt per message while the ledgers
        were carried, and those two facts destroyed each other: a carried
        ``mutations_seen`` of 3 met a Router at 0, the first tool batch of the
        follow-up read that as "the world changed", and every carried ledger was
        wiped (BUG L-5) -- by the very line whose comment says it prevents
        exactly this. The Router is also the only thing that knows which files
        this session has changed, so a fresh one left ``_unwritten_targets``
        comparing a carried plan against an empty change set and the gate
        scoping itself to nothing.

        A conversation is one session, so the change set, the mutation count and
        the undo snapshots are the session's, not the message's.
        """
        self.router = previous.router
        self.state.seen_calls = dict(previous.state.seen_calls)
        self.state.reads = dict(previous.state.reads)
        self.state.retrievals = {k: list(v) for k, v in previous.state.retrievals.items()}
        self.state.retrieval_repeats = dict(previous.state.retrieval_repeats)
        self.state.baseline = previous.state.baseline
        self.state.plan = previous.state.plan
        self.state.plan_summary = previous.state.plan_summary
        # What the conversation has established will not work does not stop
        # being true because the developer typed another message. `last_results`
        # and `dead_ends` are dropped above for the opposite reason -- they are
        # claims about files the developer may have edited in between -- but
        # "this approach was tried and failed" is a fact about the attempt.
        self.state.ruled_out = list(previous.state.ruled_out)
        self.state.replans = previous.state.replans
        # Deliberately NOT carried: `seen_digests`. It answers "have you already
        # been shown this", and the working set it was measured against is the
        # one this message inherits -- but the *files* behind those answers may
        # have moved, exactly as `last_results` may have. A stale digest suppresses
        # a result the model needs and there is nothing in the answer to say so.
        self.state.dependencies_changed = previous.state.dependencies_changed
        # Read off the Router that is now shared, so the two agree by
        # construction rather than by both being copied and hoping.
        self.state.mutations_seen = self.router.mutations
        self.state.gate_mutations = self.router.model_mutations

        # Whatever the developer changed between the two messages, the run has
        # not seen. The context still holds the old text -- nothing can be done
        # about that without rewriting history -- but the ledger stops claiming
        # the model has read the current file, so the re-read it needs is
        # dispatched instead of refused.
        for path in self._drop_stale_reads():
            self.context.append_user(
                f"{path} has changed on disk since you last read it. What is above "
                "is the older version; read it again before you act on it."
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
        if mode is Mode.AGENT:
            # The last moment before anything can be written.
            #
            # The baseline is a picture of the workspace *as the run found it*,
            # and it is taken on a background thread so the planning phase does
            # not wait six seconds for it. That makes "before the first edit" a
            # race, and losing it is silent and backwards: the snapshot picks up
            # the run's own breakage and the gate excuses it. Joining here costs
            # nothing in practice -- the planning phase is many turns and the
            # baseline is seconds -- and it makes the ordering a fact again.
            self._await_baseline()
        self.state.mode = mode
        self.context.switch_mode(mode, mode_instruction(mode))

    # -- helpers ----------------------------------------------------------

    def _sync_plan(self) -> None:
        """Move every plan step to the status the workspace and the gate justify.

        Called once a turn, before the state block is rendered. Three of the four
        statuses are read off ground truth and cannot be argued with:

        * ``done`` -- a mutation has landed on the step's file.
        * ``failed`` -- a mutation has landed on it *and* the current failing
          gate names it. "Written" and "accepted" are different claims and the
          plan used to be able to express only the first.
        * ``pending`` -- neither.

        ``skipped`` is the exception and it is sticky: the model set it through
        ``revise_plan``, saying it decided against the step, and a resync that
        overrode that would be the loop arguing with a judgement it is not
        positioned to make. A skipped step whose file is later written is
        promoted to ``done``, because that is not an argument -- the file exists.
        """
        if not self.state.plan:
            return
        touched = set(self.router.touched)
        blamed = self._gate_blamed()
        blocker = ""
        if (report := self.state.last_gate) is not None and report.blocked_by:
            blocker = report.blocked_by.name

        out: list[PlanStep] = []
        for step in self.state.plan:
            if not step.file:
                out.append(step)
                continue
            written = step.file in touched
            if step.status == control.SKIPPED and not written:
                out.append(step)
                continue
            if written and step.file in blamed:
                out.append(
                    replace(step, status=control.FAILED, note=f"{blocker} rejected this file")
                )
            elif written:
                out.append(replace(step, status=control.DONE, note=""))
            else:
                out.append(replace(step, status=control.PENDING, note=step.note))
        self.state.plan = tuple(out)

    def _gate_blamed(self) -> set[str]:
        """Which of this run's files the current failing gate names.

        Containment of known path strings in the blocking stage's output, not a
        parse of it. The distinction matters: this file is hostile to regexes
        over model prose, and rightly, but ``go build`` reporting
        ``handler/user.go:12:3: undefined: X`` is a compiler naming a path we
        already hold the exact spelling of. Looking a known string up in text is
        a lookup, not an interpretation, and it is wrong only in the direction
        that costs nothing: a path mentioned in passing marks a step ``failed``
        that is merely adjacent to the failure, and the next clean gate clears it.
        """
        report = self.state.last_gate
        if report is None or report.ok:
            return set()
        blocked = report.blocked_by
        if blocked is None:
            return set()
        text = blocked.content or ""
        return {path for path in self.router.touched if path and path in text}

    def _rule_out(self, reason: str) -> None:
        """Record something this run has established will not work.

        Deduplicated and bounded. Deduplicated because the same dead end reached
        twice is one fact, and a ledger that repeats itself is a prompt telling
        the model the same thing twice.
        """
        entry = " ".join(reason.split())[:200]
        if not entry or entry in self.state.ruled_out:
            return
        self.state.ruled_out.append(entry)
        del self.state.ruled_out[:-MAX_RULED_OUT]

    def _state_block(self) -> str:
        """What is true right now, asserted from ground truth, every turn.

        This is the answer to the failure that produced every other one: the loop
        held ``router.touched``, the plan, the gate verdict and the dead ends the
        whole time, and showed the model none of it. Twelve ``append_user`` sites
        and not one carried an inventory; the pinned block carried the task, the
        acceptance criteria, a plan frozen at submission, and the developer's
        directives. Nothing said what had been *done*.

        So the model's only account of its own progress was the transcript, and
        the transcript contains its own intentions in the same voice as its
        results. "I will write migration.md" and "migration.md is written" are
        one message apart and indistinguishable at depth. That is not a model
        being unintelligent; it is a harness withholding the answer.

        Everything below is read from the run, never from the model:

        * the plan, with each step's status from ``_sync_plan``
        * ``router.touched`` -- the files this session has actually changed
        * the last gate verdict and what it blocked at
        * ``ruled_out`` -- what has been established will not work

        Rendered compactly because it is re-sent on every turn. ``build``
        assembles this layer last, which is what makes that affordable: measured
        at 100 turns, an edit here costs 11 tokens of prefill where the same edit
        in the task block cost 75,764.
        """
        lines = [f"# Current state (turn {self.context.turn}, {self.state.mode} phase)"]

        if self.state.plan:
            lines.append("")
            lines.append("Plan:")
            lines.extend(
                "  " + step.rendered(i).replace("\n", "\n  ")
                for i, step in enumerate(self.state.plan, 1)
            )

        lines.append("")
        if written := self.router.touched:
            lines.append(f"Files this run has written ({len(written)}):")
            lines.extend(f"  - {path}" for path in written)
            lines.append(
                "  Nothing else has been written. If a file you meant to write is not "
                "on that list, it does not exist."
            )
        else:
            lines.append(
                "Files this run has written: none. Nothing you have said you would "
                "write has been written yet."
            )

        report = self.state.last_gate
        if report is not None:
            if report.ok:
                lines.append("Last gate: clean.")
            else:
                blocker = report.blocked_by.name if report.blocked_by else "an early stage"
                lines.append(
                    f"Last gate: FAILED at {blocker}. It will not be re-run until a "
                    "file changes."
                )

        if self.state.ruled_out:
            lines.append("")
            lines.append("Already ruled out this run - do not try these again:")
            lines.extend(f"  - {entry}" for entry in self.state.ruled_out)

        return "\n".join(lines)

    def _unfinished(self) -> str:
        """Files the plan named that were never written.

        Read off ``submit_plan``'s typed steps rather than out of prose. The old
        version matched path-shaped tokens in numbered paragraphs, which reported
        a neighbour named as an example as an unwritten target.
        """
        missing = self._unwritten_targets()
        return ". The plan named files this run never wrote: " + ", ".join(missing) if missing else ""

    def _normalise_plan(self, steps: Sequence[PlanStep]) -> tuple[PlanStep, ...]:
        """Put every plan path into the form the change set is recorded in.

        ``router.touched`` holds workspace-relative POSIX paths, because
        ``_confine`` rewrites every path argument before a handler sees it. Plan
        steps came straight off the model's JSON, so `./handler/user.go` and
        `handler\\user.go` compared unequal to the `handler/user.go` the write
        actually produced — and a step named that way was "never written" for the
        life of the run, whatever the run did. It refused the first `finish` and
        mis-headlined the DONE summary (BUG L-19).

        A path that will not resolve is kept verbatim. It is the model's text and
        the developer should see what was planned; it simply will not match, which
        is the same outcome as before and is now the *only* case with that outcome.
        """
        out: list[PlanStep] = []
        for step in steps:
            if not step.file:
                out.append(step)
                continue
            try:
                rel = self.router.workspace.relative(
                    self.router.workspace.resolve(step.file)
                )
            except (PathEscape, ValueError):
                out.append(step)
                continue
            out.append(step if rel == step.file else replace(step, file=rel))
        return tuple(out)

    def _unwritten_targets(self) -> list[str]:
        """Plan steps that are still work the run owes the developer.

        Both sides are workspace-relative POSIX paths: `touched` because
        `_confine` normalises every argument, the plan because `_normalise_plan`
        does the same at `submit_plan`.

        A step the model marked ``skipped`` through ``revise_plan`` is not
        outstanding, and that is the one place this defers to the model. It said
        which step it decided against and why, on the record and in the state
        block; refusing its ``finish`` over a step it has explicitly abandoned
        would be asking it to relitigate a decision it has already justified.
        Everything else -- ``pending`` and ``failed`` alike -- is still owed.
        """
        if not self.state.plan:
            return []
        touched = set(self.router.touched)
        return [
            s.file
            for s in self.state.plan
            if s.file and s.file not in touched and s.status != control.SKIPPED
        ]

    def _novelty(
        self, call: ToolCall, outcome: ToolResult, *, refused_by_mode: bool
    ) -> tuple[bool, str]:
        """Whether a dispatched call told the run anything, and what to say if not.

        Four ways a call can be dispatched and still add nothing, in the order
        they are cheapest to check:

        * **Mode-refused.** Says nothing about the call, only about who asked.
        * **The tool declares it.** ``meta["informed"] is False`` is a tool
          saying "this succeeded and moved you nowhere" -- a zero-match
          ``search_repo`` is the case it was added for. The tool knows; nothing
          else can tell that answer from a useful one without reading it.
        * **The answer is one the run already has.** A digest over the rendered
          body, so a result identical to an earlier one does not count however
          different the arguments were. This is the check ``_fingerprint``
          structurally cannot make: it hashes the *question*, and the loop that
          actually happens in the field is the same question asked three ways.
        * **A search that reached only places already reached.** Set overlap on
          where the answer points, per tool.

        Returns ``(added, note)``. The note is a ``role: user`` message, because
        no tool produced it and a ``role: tool`` message with no ``tool_call_id``
        is malformed on the wire and a lie in the transcript.
        """
        if refused_by_mode:
            return False, ""
        if outcome.meta.get("informed") is False:
            return False, ""

        if note := self._overlap_note(call, outcome):
            return False, note

        # Mutations are always progress and are never digested: two identical
        # `write_file` results are two files written, and the second is not the
        # run standing still.
        if outcome.mutations:
            return True, ""

        digest = _digest(outcome.for_model())
        if not digest:
            # Too short to be distinctive. See `_MIN_DIGEST_CHARS`: two stages
            # both saying "clean" is two facts, not one repeated.
            return True, ""
        if digest in self.state.seen_digests:
            return False, (
                f"{call.name} returned exactly what an earlier call this run has "
                "already been given, word for word. Asking the same question a "
                "different way gets the same answer.\n\n"
                "Use what is above, or ask about something else."
            )
        self.state.seen_digests.add(digest)
        return True, ""

    def _overlap_note(self, call: ToolCall, outcome: ToolResult) -> str:
        """What to tell a run whose searches keep reaching the same places.

        Judged on the answer rather than on the question. For ``search_docs``
        that is forced: it runs BM25 with no floor, and its scores do not
        separate "the corpus answers this" from "the corpus contains these
        words" -- measured against the real 92-section corpus, the query a field
        transcript died on scored higher than every question the corpus
        genuinely answers. What is reliable is the sections that come back.
        Twenty ``search_docs`` turns in the field returned the same four sections
        for six different phrasings, and nothing said so.

        ``search_repo`` was outside this for its whole life, and it is the tool a
        run actually loops on. Its answer is a set of ``path:line`` addresses,
        which is the same shape of evidence as a citation list and admits the
        same question -- so it is asked here too, off ``meta["locations"]``.

        The two differ in what happens at the ceiling, and deliberately.
        ``search_docs`` is withdrawn: the corpus cannot acquire new sections
        mid-run, so nothing that follows could change the answer. ``search_repo``
        is not, because the workspace *does* change and taking away the tool a
        run navigates with is a worse failure than the one it prevents. There,
        the count exists only to stop the turn scoring as progress.
        """
        located = _LOCATED.get(call.name)
        if located is None or not outcome.ok:
            return ""
        key, noun, subject = located

        hits = frozenset(str(h) for h in (outcome.meta.get(key) or ()))
        history = self.state.retrievals.setdefault(call.name, [])
        if not hits:
            # "nothing matches" is already an explicit answer; counting it as a
            # repeat would punish the one reply that is honest about coming back
            # empty. A zero-hit search still does not *inform* -- the tool says
            # so itself with `informed: False` -- but it is not a loop.
            self.state.retrieval_repeats[call.name] = 0
            return ""
        try:
            parsed = call.parsed() or {}
            query = str(parsed.get("query") or parsed.get("pattern") or "")
        except ValueError:
            query = ""

        seen: set[str] = set()
        source = ""
        for earlier_query, earlier in history:
            if len(hits & earlier) / len(hits) >= RETRIEVAL_OVERLAP and not source:
                source = earlier_query
            seen |= earlier
        history.append((query, hits))

        if hits - seen:
            self.state.retrieval_repeats[call.name] = 0
            return ""
        repeats = self.state.retrieval_repeats.get(call.name, 0) + 1
        self.state.retrieval_repeats[call.name] = repeats

        if repeats < MAX_RETRIEVAL_REPEATS:
            return (
                f"Those are the same {noun}s {source or 'an earlier search'!r} already "
                "returned -- that search added nothing you had not been given.\n\n"
                f"Rewording it will not reach different {noun}s. Ask about something "
                "else, or work from what is already above."
            )

        if call.name == "search_docs":
            return (
                f"That is {repeats} searches in a row returning sections you already "
                "have. The knowledge base does not cover this question -- that is an "
                "answer, not a gap to keep searching for.\n\n"
                "Stop rephrasing it. Follow the pattern in the nearest existing code "
                "instead, and if the step genuinely cannot be done without knowing "
                "this, say which step and what you need, in one line."
            )
        return (
            f"That is {repeats} searches in a row returning {subject} you already "
            "have. Rephrasing the pattern is not going to find it.\n\n"
            "If it is not in the matches above, it is not where you are looking: "
            "open one of the files you have already found and read it, or say in one "
            "line what you cannot locate."
        )

    def _re_reading(self, call: ToolCall) -> str:
        """Why this read asks for nothing new, or ``""`` to dispatch it.

        Judged on **coverage**, not on a call count. The old rule allowed ten
        reads of a path and then refused every one after, whatever range it
        asked for -- so a model working through a 6,571-line handler in
        thirty-line windows was cut off having seen about 280 lines, and told
        that reading it again "is not going to show you anything those did not".
        It was going to show it the other ninety-six per cent.

        What is worth refusing is a range already delivered. That is a question
        about intervals and it has an exact answer, so there is no threshold to
        get wrong: a read whose span is inside the union of earlier spans is
        answered from what is already in context, and a read that reaches past
        them is dispatched however many have come before.

        The call-count ceiling that remains is a backstop against a model
        reading one line at a time, and it scales with the file.
        """
        if call.name != "read_file":
            return ""
        parsed = _safe_args(call)
        if not isinstance(parsed, dict):
            return ""
        path = parsed.get("path")
        if not isinstance(path, str) or not path:
            return ""

        recorded = self.state.reads.get(path)
        if recorded is None:
            return ""
        ledger = self._live_reads(path, recorded)

        start, end = _as_line(parsed.get("start")), _as_line(parsed.get("end"))
        if start is None and end is None:
            # A whole-file read. Only redundant once the whole file has been
            # delivered, which `covers` can answer exactly when the length is
            # known and cannot when it is not.
            if ledger.lines and ledger.covers(1, ledger.lines):
                return (
                    f"You have already read all {ledger.lines:,} lines of this file this "
                    "run, and every one of those reads is still in context above.\n\n"
                    "Act on what you have, or say plainly what you are looking for and "
                    "cannot find."
                )
            return ""

        low = start or 1
        if end is None and not ledger.lines:
            # An open-ended read of a file whose length nothing has reported:
            # `read_file(start=400)` means "from 400 to the end", and the end is
            # unknown. Collapsing it to `(400, 400)` made a single covered line
            # answer for the whole tail (BUG L-23). Unknown coverage is not
            # coverage; dispatch it.
            return ""
        high = end or (ledger.lines or low)
        if ledger.covers(low, high):
            return (
                f"Lines {low}-{high} of this file are already in context above, from "
                f"{ledger.summary()}.\n\n"
                "This read was not dispatched because it asks for nothing new. A range "
                "reaching past what you have already been given is read normally, so "
                "widen it or move to a part of the file you have not seen."
            )

        if ledger.calls >= ledger.budget():
            return (
                f"You have read this file {ledger.calls} times this run, covering "
                f"{ledger.covered_lines():,} of its {ledger.lines or '?'} lines, and "
                "that is as many separate reads as one file gets.\n\n"
                "Read a wider range in one call if you need more of it, or act on what "
                "you have."
            )
        return ""

    def _live_reads(self, path: str, recorded: _ReadLedger) -> _ReadLedger:
        """What the model can actually still see of ``path``, right now.

        The context manager is the authority on that, and the loop asks rather
        than remembers. This is the shape the prior audit's root cause (RC-1)
        asks for: the ledger records *how often* a file has been asked for, which
        is a fact about the run; the context holds *which lines are in front of
        the model*, which is a fact about the messages — and the moment those two
        answers came from the same place they could disagree.

        `_forget_evicted` still rebuilds the stored spans on compaction, so the
        persisted ledger stays honest. But the refusal no longer depends on that
        having happened: any eviction, by any path, is visible here immediately.
        """
        live = _ReadLedger(lines=recorded.lines, calls=recorded.calls)
        for low, high in self.context.coverage().get(path, []):
            live.add(low, high)
        return live

    def _record_read(
        self,
        path: str,
        span: tuple[int, int] | None,
        total: int,
        *,
        delivered: bool = True,
    ) -> None:
        """Remember what a dispatched read actually put in front of the model.

        Not the call's arguments: the tool clamps the range to the file, so a
        model asking for lines 1-9999 of a 200-line file has been given the whole
        thing. Not the tool's span either: the insertion cap can elide most of a
        large file on the way into the context, and a ledger written from the
        tool's span then refuses the re-read the elision marker just asked for
        (BUG L-8). The caller passes the span of the message as it exists in
        context.

        ``delivered=False`` means the cap kept none of the file's lines. The call
        still counts against the per-file read budget — it was dispatched and it
        cost a turn — but it covered nothing, so the ledger records no lines.
        """
        ledger = self.state.reads.setdefault(path, _ReadLedger())
        ledger.calls += 1
        if total > 0:
            ledger.lines = total
        ledger.mtime = self._mtime(path)
        if not delivered:
            return
        if span is not None:
            ledger.add(*span)
        elif total > 0:
            ledger.add(1, total)

    def _mtime(self, path: str) -> float:
        """When ``path`` was last written, or 0.0 if that cannot be answered."""
        try:
            return self.router.workspace.resolve(path).stat().st_mtime
        except (OSError, PathEscape, ValueError):
            return 0.0

    def _drop_stale_reads(self) -> list[str]:
        """Forget coverage of files that changed since the run that read them.

        The read ledger carries across a follow-up, and between two messages the
        developer is doing their own work: they read the agent's diff, fix a line
        themselves, and type the next message. The ledger then refused the
        re-read of a file whose contents had moved, and the agent reasoned about
        the version it had been shown rather than the one on disk (BUG L-25).
        The `carry_from` docstring drops `last_results` for exactly this reason
        and kept `reads`.

        By mtime rather than by content hash: this runs once per follow-up over
        every file the conversation has read, and a stat is the cheap question.
        A file whose mtime is unknown on either side is kept — dropping on "we
        cannot tell" would discard the whole ledger on any filesystem that does
        not report one.
        """
        dropped: list[str] = []
        for path, ledger in list(self.state.reads.items()):
            now = self._mtime(path)
            if not ledger.mtime or not now or now == ledger.mtime:
                continue
            del self.state.reads[path]
            dropped.append(path)
        return dropped

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

    def _compact(self, *, retain_pct: float = 0.35, reason: str = "threshold") -> Iterator[Event]:
        """Compact, invalidate the ledgers, and say what it actually freed.

        Every compaction goes through here, including the emergency one in
        ``_complete``. That one used to call ``context.compact`` directly, so it
        was invisible to the thrash detector and to the compaction counter
        (BUG L-24) — and, once ledgers began to be invalidated on eviction, it
        would also have been the one path that evicted content while leaving the
        ledgers claiming the model could still see it.
        """
        self.state.compactions.append(self.context.turn)
        before = self.context.usage().total
        recap = self.context.compact(self._summarise, retain_pct=retain_pct)
        evicted = self.context.last_eviction
        self._forget_evicted(evicted)
        yield Event(
            EventType.GATE,
            {
                "kind": "compaction",
                "reason": reason,
                "before": before,
                "after": self.context.usage().total,
                "turns": getattr(recap, "turns", None),
                # Reported because a compaction that freed nothing used to look
                # identical to one that freed half the context.
                "evicted_messages": evicted.messages,
                "evicted_paths": list(evicted.paths),
            },
        )

    def _forget_evicted(self, evicted: Eviction) -> None:
        """Drop every ledger entry that described content compaction removed.

        This is the second half of the prior audit's central finding: the
        context manager evicts, the loop's ledgers refuse, and nothing connects
        them. The recap tells the model "re-read one only if you need a line
        range you have not seen" while the read intercept answers that same
        re-read with "those lines are already in context above" — two true-
        sounding messages, neither of which can be obeyed, and the model has no
        way to recover the content (BUG L-10).

        The read ledger is *rebuilt* from what the context still holds rather
        than cleared, so a file with surviving reads keeps its coverage and only
        the evicted spans become askable again. The call ledgers
        (``last_results``, ``echoes``, ``truncated_at``) are cleared for evicted
        ids: they exist to answer a repeat with "you already have this", and
        after eviction that claim is simply false.

        ``seen_calls`` and ``dead_ends`` survive deliberately. A dead end is a
        fact about the world, not about the transcript — the arguments are still
        invalid — and the repeat counts are what stop a post-compaction run from
        walking the same circle again.
        """
        coverage = self.context.coverage()
        for path in evicted.paths:
            ledger = self.state.reads.get(path)
            if ledger is None:
                continue
            spans = coverage.get(path, [])
            if not spans:
                # Nothing of this file is left in context. The dispatch count
                # stays -- it is a budget against re-reading, and the reads did
                # happen -- but the run has seen none of it any more.
                ledger.covered = []
                continue
            ledger.covered = []
            for low, high in spans:
                ledger.add(low, high)

        if not evicted.messages:
            return
        # The cached-result ledgers are keyed by fingerprint, not by message, so
        # they cannot be pruned precisely. Any eviction makes "you already have
        # this answer above" unreliable, and answering a repeat from a cache of
        # a message that is gone is exactly the failure this is fixing.
        self.state.last_results.clear()
        self.state.partial_results.clear()
        self.state.truncated_at.clear()
        self.state.echoes.clear()
        # And the result digests, for the same reason and with more force: they
        # exist to answer "you have already been shown this", and after an
        # eviction the model has *not* been shown it. Keeping them would suppress
        # the one re-dispatch that could recover the content compaction removed
        # -- the exact shape of BUG L-10, arriving through a new ledger.
        self.state.seen_digests.clear()

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
        transcript = "\n\n".join(_rendered(m) for m in messages)

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
                # `summariser`, which is what section 6.5 specifies. It said
                # exactly this for its whole life and every compaction in
                # production silently returned the fallback recap, because the
                # role vocabulary was three names long and this was not one of
                # them; it was moved to `fast` to make it work at all. Both
                # halves now share `ROLES`, so the name resolves — and a
                # summariser worth pointing at a small model is one that can be
                # told apart from the intent classifier, which `fast` still is.
                role="summariser",
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


#: How much of a tool call's arguments the summariser is shown. Enough to tell
#: `write_file(handler/user.go)` from `write_file(repo/postgres/user.go)`, not
#: enough for one 40KB write to be the whole recap prompt.
_ARGS_IN_TRANSCRIPT = 200


def _rendered(message: Message) -> str:
    """One message, as the summariser sees it.

    It used to see ``content`` alone. An assistant turn that was purely tool
    calls has an empty ``content``, so the transcript handed to the summariser
    rendered every edit the run made as a blank line — and the histories that
    summarised worst were exactly the write-heavy ones the recap matters most for
    (BUG L-27). The recap then said nothing about what had been done, and the
    post-compaction run had no way to know it had already written the file.

    Arguments are truncated rather than omitted: which file was written is the
    fact worth carrying, and the content of the write is in the workspace.
    """
    where = "" if not message.path else " " + message.path
    head = f"[{message.role}{where}]"
    parts = [f"{head} {message.content}".rstrip()]
    for call in message.tool_calls:
        args = (call.arguments or "").strip()
        if len(args) > _ARGS_IN_TRANSCRIPT:
            args = f"{args[:_ARGS_IN_TRANSCRIPT]}… ({len(call.arguments):,} chars)"
        parts.append(f"{head} called {call.name}({args})")
    return "\n".join(p for p in parts if p.strip() != head)


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


def _as_line(value: Any) -> int | None:
    """A 1-based line number from a tool argument, or None when absent."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None


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


#: Tools whose answer is a set of *places*, and the meta key each reports them
#: under. Overlap between one answer and the run's earlier answers is what makes
#: "that search added nothing" a fact rather than a guess about phrasing.
#:
#: `search_repo` reports `path:line`; `search_docs` reports section citations.
#: The third element is what the ceiling message calls them collectively.
_LOCATED: dict[str, tuple[str, str, str]] = {
    "search_docs": ("hits", "section", "sections"),
    "search_repo": ("locations", "match", "matches"),
}


def _salvaged(call: ToolCall, key: str) -> str:
    """One string argument recovered from arguments the output limit cut short.

    The arguments are a *prefix* of valid JSON -- that is what
    ``_looks_cut_off`` establishes -- so ``json.loads`` cannot help, but a
    complete key/value pair before the cut is still readable, and the one worth
    reading is the path. Deliberately narrow: it matches a quoted key followed by
    a quoted value with no escapes in it, which is every workspace-relative path
    this agent produces and nothing that needs a JSON parser to get right.

    Returns "" when the key did not survive, which the caller treats as "say the
    tool name alone" rather than as an error.
    """
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"\\\n]{{1,200}})"', call.arguments or "")
    return match.group(1) if match else ""


#: How much of an answer there has to be before "you have seen this before" is
#: worth asserting.
#:
#: Not a tuning knob -- it is the false-positive guard. Short tool answers
#: collide honestly and often: two stages both say "clean", two searches both say
#: "no matches", two builds both say "ok". Suppressing the second of those as a
#: repeat would be wrong, because the second "clean" is about a different thing
#: and the model needs it. Two hundred characters is longer than every such
#: answer and far shorter than any real report.
#:
#: The first version keyed the digest by tool name instead, which avoids the same
#: collisions and also throws away the case worth catching: one *question* asked
#: through two tools, or one tool asked twice with different arguments, coming
#: back with the same answer. `_fingerprint` already covers "same tool, same
#: arguments"; the whole reason this exists is to cover what it cannot.
_MIN_DIGEST_CHARS = 200


def _digest(body: str) -> str:
    """A stable key for "the run has already been given exactly this answer", or "".

    Whitespace-normalised, because a report that differs only in padding is the
    same report. Returns "" for an answer too short to be distinctive, which the
    caller treats as "do not judge this one".
    """
    normalised = " ".join(body.split())
    if len(normalised) < _MIN_DIGEST_CHARS:
        return ""
    return hashlib.sha256(normalised.encode("utf-8", "replace")).hexdigest()


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
