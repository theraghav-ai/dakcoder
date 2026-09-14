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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from dakcoder_shared.envelope import (
    DeltaCoalescer,
    Event,
    EventType,
    MutationKind,
    ToolResult,
)
from dakcoder_shared.llm import (
    LLMClient,
    Metering,
    ToolCall,
    UnsupportedParameterError,
)
from dakcoder_shared.paths import PathEscape
from dakcoder_shared.tokens import estimate_tokens

from .debug import DebugLog
from .context import (
    MAX_RECAP_ITEMS,
    ContextManager,
    Eviction,
    Message,
    OverBudgetError,
    Recap,
    Role,
    Visibility,
)
from .gate import (
    ROUTES_BEFORE,
    finding_path,
    GateReport,
    StageResult,
    full_gate,
    inner_loop,
    take_baseline,
)
from .llm import TurnResult, complete, reasoning_leaked
from . import metrics
from .hooks import (
    MAX_PARALLEL_CALLS,
    HookContext,
    Hooks,
    hook_context_block,
    parallel_batch,
)
from .loopstate import CallLedger, GateState, Progress, ReadState, TaskState
from .migration import (
    BASE_BRANCH,
    DEFAULT_BRANCH,
    MIN_PHASES,
    PROGRESS_PATH,
    PROTECTED,
    Phase,
    phases_from_meta,
    plan_objection,
    progress_document,
    steps_for_phase,
)
from .modes import CONTEXT_WINDOW, Intent, Mode, config_for
from .plan import PlanRecord
from .undo import ensure_private
from .prompts import mode_instruction, system_prompt
from .tools import registry
from .tools.control import PlanStep, split_paths, steps_from_meta
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


#: Which group owns each of ``_State``'s field names.
#:
#: The table *is* the decomposition: one line per field saying which question it
#: answers. It exists rather than forty properties because a table can be read
#: end to end -- "what does the gate group hold" is a grep for ``"gate"`` in one
#: place, not a walk through four hundred lines of boilerplate -- and because
#: moving a field between groups is a one-line change here instead of a rename
#: at two hundred and seventy-nine call sites.
_OWNER: dict[str, str] = {
    # What the run was asked for, and what it committed to.
    "mode": "task",
    "intent": "task",
    "intent_source": "task",
    "intent_why": "task",
    "plan": "task",
    "plan_summary": "task",
    "plan_forced": "task",
    "cursor": "task",
    "removed": "task",
    "gone_once": "task",
    "rewritten": "task",
    "churn": "task",
    "routes_saved": "task",
    "routes_before": "task",
    "migration": "task",
    "awaiting": "task",
    # What has been asked and answered. Invalidated as a unit.
    "seen_calls": "calls",
    "mutations_seen": "calls",
    "last_results": "calls",
    "partial_results": "calls",
    "dead_ends": "calls",
    "truncated_at": "calls",
    # What has been read and searched, and how much of each.
    "reads": "reading",
    "retrievals": "reading",
    "retrieval_repeats": "reading",
    "search_hits": "reading",
    "search_repeats": "reading",
    # What the gate has said, and what has happened since.
    "baseline": "gate",
    "last_gate": "gate",
    "gate_key": "gate",
    "gate_failures": "gate",
    "gate_mutations": "gate",
    "idle_since_gate": "gate",
    "gate_turn": "gate",
    "dependencies_changed": "gate",
    # How the turn budget is being spent, and every bound on spending it.
    "stalled_turns": "progress",
    "research_turns": "progress",
    "must_answer": "progress",
    "answer_because": "progress",
    "forced": "progress",
    "forced_terminal": "progress",
    "terminal_forced": "progress",
    "truncated_turns": "progress",
    "truncations": "progress",
    "finish_refused": "progress",
    "preamble_refused": "progress",
    "plan_objections": "progress",
    "degenerate_refused": "progress",
    "replans": "progress",
    "revisions": "progress",
    "compactions": "progress",
    "tried": "progress",
}


@dataclass
class _State:
    """Everything the loop tracks that is not in the context manager.

    Five groups, not thirty-eight fields. Each group is a question -- what was
    asked for, what has been called, what has been read, what the gate said, how
    the budget is being spent -- and each owns the fields and the invalidation
    rules that answer it. ``loopstate.py`` has the groups and the reasoning.

    Every old field name still resolves, through ``_OWNER`` above. That is
    deliberate and it is transitional: the split had to be a refactor rather
    than a rewrite, so the two hundred and seventy-nine existing uses keep
    working and can move to ``state.calls.seen_calls`` group by group, with the
    table saying where each one goes. New code should reach for the group.

    What is gone, and why. ``attempts``, ``cycles``, ``blocked_stage``,
    ``route_mutations`` -- the escalation ladder, which spent its budget on
    Verifier turns that had attempted nothing. ``said``, ``echoes`` -- text
    detectors that ended runs on a symptom. ``idle``, ``planner_idle``,
    ``planner_research``, ``executing_research`` and five more -- one counter
    per mode with no stopping condition; a mode that must call a tool is now
    made to, and a mode that must not is over when it stops. ``scaffolded`` --
    there is no Scaffolder. ``dup_results``, ``intercepts`` -- ledgers of
    messages to delete from the transcript later, which is not a thing that
    happens to an append-only transcript.

    And two that went with the canonical/projection split, because the
    projection answers them from the request instead: ``echoes`` (repeated
    answers collapse in ``projection``) and ``seen_bodies``
    (``context.visible_bodies``). Both were copies of what the model could see,
    and a copy of that is exactly what goes stale.
    """

    task: TaskState = field(default_factory=TaskState)
    calls: CallLedger = field(default_factory=CallLedger)
    reading: ReadState = field(default_factory=ReadState)
    gate: GateState = field(default_factory=GateState)
    progress: Progress = field(default_factory=Progress)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names the dataclass itself does not define, so the
        # five groups above never come through here.
        owner = _OWNER.get(name)
        if owner is None:
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, owner), name)

    def __setattr__(self, name: str, value: Any) -> None:
        owner = _OWNER.get(name)
        if owner is None:
            object.__setattr__(self, name, value)
            return
        setattr(getattr(self, owner), name, value)

    def groups(self) -> dict[str, Any]:
        """The five groups by name, for diagnostics and for ``carry_from``."""
        return {
            "task": self.task,
            "calls": self.calls,
            "reading": self.reading,
            "gate": self.gate,
            "progress": self.progress,
        }


_RECAP_PROMPT = """Summarise this agent transcript for a handover to a fresh context.

Reply with JSON only, no prose around it, using exactly these keys:
  goal            one sentence: what the run is trying to achieve
  plan_step       which step of the plan it is on, if the transcript says
  files_created   list of workspace-relative paths created
  files_modified  list of workspace-relative paths modified
  decisions       list of decisions taken AND the reason for each
  findings        list of facts this run established about the code, each
                  one usable on its own, e.g. "handler/objection.go: all 14
                  handlers take *gin.Context" -- at most 12, best first
  verified        list of things confirmed working (gate stages that passed)
  open_items      list of what is still unresolved
  do_not_retry    list of approaches already tried that did NOT work

`do_not_retry` matters most: without it the next turns repeat the dead end that
made this compaction necessary. Keep every file path exactly as written.

`findings` matters second, and mostly for a run that is answering rather
than editing. Every other key describes what the run DID; on a review or a
validation the reading IS the work, and a recap holding only the names of
the files read throws it away -- so the turns after it re-read those files
to recover it, which is what put the context over the threshold to begin
with. Leave it empty for a run that is changing code.

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
                "findings": {"type": "array", "items": {"type": "string"}},
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

Apply one test: **after a perfect reply, is any file in the repository
different?**

No -- it is a "question". They wanted to be told something: an explanation, a
review, a validation, an audit, a comparison, a list, an opinion, a yes/no.
This holds however work-shaped the subject is; validating a migration plan
changes no files, and neither does auditing a package.

Yes -- it is a "change". A feature, a fix, a migration, a refactor, a scaffold.
Also "change" when they are approving work just described to them ("go", "do
it", "yes please"), or when they ask a question and then ask for the work as
well ("explain the handler, then migrate it").

Then one more question, only when the answer above is "change": is this a
**whole-service migration** -- converting a legacy api-* service to the n-api
template, or being asked to carry on with one already under way? Set
"migration" true for that, and for nothing else. One handler, one bug, one new
endpoint is ordinary work however much it touches, and a *question* about a
migration is not one.

Answer with the JSON object only. Keep "why" to at most eight words.

CONVERSATION SO FAR:
{conversation}

LATEST MESSAGE FROM THE DEVELOPER:
{task}
"""

#: Measured 2026-09-09 against 60 labelled tasks, 3 samples each, after a field
#: run turned "validate this migration plan" into an unrequested migration. The
#: prompt this replaces scored 44/60: it listed the *subjects* of a change --
#: "a feature, a fix, a migration, a refactor" -- and a request to validate a
#: migration plan matched on the noun. Three candidate rewrites all scored
#: 60/60, including on eight verbs none of them named (diagnose, critique,
#: sanity-check, map out), so the enumerating ones were not winning by their
#: enumeration. This one states the test rather than a vocabulary, which is the
#: version that cannot rot as the words people use drift.
_INTENT_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "intent",
        "schema": {
            "type": "object",
            "properties": {
                # `kind` first, so guided decoding settles the answer before
                # it spends any of a 64-token budget explaining it.
                "kind": {"type": "string", "enum": ["question", "change"]},
                "why": {
                    "type": "string",
                    "description": "At most eight words: the word that decided it.",
                    "maxLength": 120,
                },
                # Third, and last, because it is the only one with a default
                # that is right when the model says nothing: an ordinary
                # change. A false negative costs a migration the phased
                # treatment and is visible immediately -- the developer sees an
                # eight-step plan for a forty-file conversion. A false positive
                # costs a bug fix its gate, which is the expensive direction,
                # so the prompt spends its words ruling that out.
                "migration": {
                    "type": "boolean",
                    "description": "True only for a whole-service legacy-to-template conversion.",
                },
            },
            # The first two. `why` was optional and the model simply left it
            # out -- measured live, empty on every classification -- so the
            # field the loop reads back to explain a misroute never arrived.
            # `migration` is not required: it is meaningless on a question, and
            # a required boolean is one more thing a 160-token reply can be cut
            # in the middle of.
            "required": ["kind", "why"],
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

#: How many times the loop itself sends a run back to the Planner.
#:
#: One. The trigger is the second gate failure *after an edit* -- the model
#: changed something, the gate ran again, and it still did not clear. That is
#: the strongest evidence a run produces that its approach is wrong, and the
#: old answer to it was the same instruction a third time and then UNVERIFIED.
#: A cached failure (nothing edited since) is not that evidence; it is a run
#: not acting, and ``_gate_stalled`` already ends those. Once, because a replan
#: against the same facts is a re-roll, and the state block is what makes the
#: facts different the second time.
MAX_REPLANS = 1

#: How many times the model may replace the remaining plan with `revise_plan`.
MAX_REVISIONS = 2

#: How many calls one reply may dispatch. `parallel_tool_calls` is not sent --
#: an endpoint with `drop_params` off would 400 on it -- so the bound is here:
#: identical calls in one batch are answered once, and calls past the cap are
#: answered "not run" with a reason, which keeps the wire coherent.
MAX_CALLS_PER_BATCH = 6

#: How many entries each list in the state block carries.
STATE_ITEMS = 8

#: How many times an `answer` that reads as the opening of something longer is
#: sent back. One, and it is cheap to reject: the message says that re-sending
#: the same text unchanged will be taken as final, so a false positive costs a
#: turn rather than an argument.
MAX_PREAMBLE_REFUSALS = 1

#: How long an answer can be and still be a preamble. Generous: what is being
#: caught is an answer that *announces* findings and then stops, and the two
#: field cases were 110 and 172 characters.
PREAMBLE_CHARS = 400

#: An answer that promises what it does not then deliver.
#:
#: Two field runs, three turns between them, all ending the same way: twenty
#: turns of real work funnelled into one `finish`, and the `answer` holding
#: only its own opening line. "I have validated the migration plan against the
#: actual codebase. Here is my assessment of each step's accuracy:" -- and
#: nothing after the colon. The developer typed "where is the assessment".
#:
#: The model is writing for a chat channel, where prose follows the sentence
#: that introduces it. Here there is no after: `answer` is the whole delivery.
_PROMISES_MORE = re.compile(
    r"(here(?:'s| is| are)\s+(?:what|my|the)|as follows|step by step|"
    r"the following|below\s*[:.]?$|my (?:assessment|findings|analysis|review))",
    re.IGNORECASE,
)


def _is_preamble(answer: str) -> bool:
    """Whether this answer reads as the opening of something longer.

    Two signals, either alone enough, both bounded by length. An answer that
    *ends* on a colon is incomplete by construction. An answer that says
    findings follow -- and is short enough that they plainly did not -- is the
    same failure with different punctuation.
    """
    if not answer or len(answer) > PREAMBLE_CHARS:
        return False
    return answer.rstrip().endswith((":", "：")) or bool(_PROMISES_MORE.search(answer))


#: Below this an answer is too short for a repetition ratio to mean anything,
#: and a short answer that repeats itself is cheap for a developer to read past.
DEGENERATE_CHARS = 2_000

#: The n in the n-gram. Eight words is long enough that ordinary prose does not
#: repeat one by accident -- measured at 0.999 distinct over this repository's
#: own documentation and source -- and short enough to catch a clause-length
#: loop.
_DEGENERATE_N = 8

#: Distinct 8-grams as a fraction of all of them, below which a long answer is
#: a loop rather than a document.
#:
#: Measured, not guessed, against the field answer this exists for and against
#: the legitimate shapes most likely to look like it:
#:
#:     turn-34 field answer      0.012      a clause repeated ~100 times
#:     300-row markdown table    0.612
#:     200-item checklist        0.801
#:     this repo's own docs      0.999
#:     Python source             0.999
#:
#: 0.30 sits in a fifty-fold gap. Compression ratio was tried first and
#: rejected: zlib scores the table at 0.055 and the checklist at 0.044 against
#: the degenerate answer's 0.011, which is not a gap anything can be thresholded
#: on -- structured content is compressible, and that is not the same thing as
#: being a loop.
DEGENERATE_RATIO = 0.30

#: How many times a degenerate `answer` is sent back. One, like a preamble, and
#: for the same reason: re-sending it unchanged is taken as final, so a false
#: positive costs a turn.
MAX_DEGENERATE_REFUSALS = 1


def _is_degenerate(answer: str) -> bool:
    """Whether this answer is the model looping rather than writing.

    The failure this catches, from the field: a `finish` forced at a 119,000-
    token context returned 7,036 tokens of "1 routes file, 1 temporal_instrument
    file, 1 main file, ..." repeated about a hundred times until it hit
    `MAX_ANSWER_CHARS`. The developer was shown 24,000 characters of it. The
    same forced-finish path at a 55,000-token context, thirteen turns earlier,
    produced a coherent plan -- so this is a property of the reply, and the only
    place to catch it is the reply.

    **It has to be caught before it is shown, and before it is kept.** A
    `finish` travels as the assistant's tool-call arguments, so an answer this
    long is appended to the conversation and read back on every turn after it;
    the field run carried those 7,000 tokens of its own looped text for the
    remaining fifteen turns, and spent them repeating a one-line preamble
    verbatim.

    ``_is_preamble`` cannot see this: it only looks at answers under
    `PREAMBLE_CHARS`, and this failure is only possible well above it. The two
    are opposite ends of the same question -- did the model actually say
    anything -- so they sit together.
    """
    words = answer.split()
    if len(answer) < DEGENERATE_CHARS or len(words) <= _DEGENERATE_N:
        return False
    grams = {
        tuple(words[i : i + _DEGENERATE_N])
        for i in range(len(words) - _DEGENERATE_N + 1)
    }
    return len(grams) / (len(words) - _DEGENERATE_N + 1) < DEGENERATE_RATIO


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

#: A tool name as it appears inside a sentence. Tool names are lower_snake_case
#: by the registry's own rule, so this needs no vocabulary of its own.
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")

#: Control tools, which an ``accepts`` criterion would never legitimately name.
#:
#: Excluded from the check below so that a step checked by "submit_plan" -- which
#: no plan has ever said and none should -- is not reported as an unrunnable
#: criterion in a message about verification.
_CONTROL_TOOLS = frozenset({"submit_plan", "revise_plan", "ask_developer", "finish"})


def _unrunnable_checks() -> frozenset[str]:
    """Tools an ``accepts`` may cite that the acting phase cannot call.

    Derived from the registry rather than listed, so a tool moved between modes
    does not leave a stale name here. Gate tools are excluded: the model never
    calls `gofmt` or `swagger_check` and does not need to -- the gate runs them
    on a schedule, so "swagger_check passes" is a criterion the run does apply.

    The failure this answers: a migration plan wrote "legacy_audit reports no
    legacy-lib-generation findings" as the acceptance criterion of all seven of
    its steps. `legacy_audit` is an ask/planner tool. The acting phase called it,
    was refused, and spent the turn discovering that its own plan had given it
    nothing it could check -- on a phase where the criterion was the only thing
    that could have told it a step was finished.
    """
    acting = set(registry.names_for(Mode.AGENT))
    gate = set(registry.gate_tools())
    return frozenset(
        spec.name
        for spec in registry.all_specs()
        if spec.name not in acting and spec.name not in gate and spec.name not in _CONTROL_TOOLS
    )


#: How many times a migration's plan is sent back for not being phased.
#:
#: Two, where the refusals above are one, because this objection is answerable:
#: `plan_objection` names the missing field and the values it will accept, and
#: the fix is re-emitting the same call with one more argument. The first
#: push-back usually gets phases and no `parts`; the second gets both. A third
#: would be arguing.
MAX_PLAN_OBJECTIONS = 2

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
MAX_RETRIEVAL_REPEATS = 3

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
        #: The before/after seam around a tool call. Empty by default, and
        #: cheap when empty -- the loop's fast path is a length check.
        #:
        #: It exists so that a policy, a linter, a redaction pass or an audit
        #: log can be added without editing this file. Everything load-bearing
        #: -- the router's six checks, the intercept ledgers, the gate -- stays
        #: hard-wired, because those are not opinions a deployment gets to
        #: hold. See ``hooks.py``.
        self.hooks = Hooks()
        #: The plan as it will be written to disk. Held here rather than
        #: derived at save time so the revision history accumulates across the
        #: run instead of being one entry deep.
        self._plan_record = PlanRecord()
        #: Whether this run has already recovered from the endpoint refusing a
        #: request as too large. Once per run: a second overflow after a
        #: deterministic compaction to 15% is not a estimate that drifted, it is
        #: a context that cannot be made to fit, and retrying it is a bill.
        self._overflow_recovered = False
        #: Full-fidelity turn recording, or None. See `debug.py`: off by
        #: default, one `is None` check per seam when off, and it hangs off the
        #: four places that already funnel rather than being sprinkled through
        #: the logic.
        self._debug: DebugLog | None = None
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
            # The same funnel, for the same reason it exists: a new `yield
            # Event(...)` anywhere inside cannot be missed by omission. The
            # recorder is `None` unless DAKCODER_DEBUG is set, so this is one
            # identity check per event when it is off.
            if self._debug is not None:
                self._debug.event(str(event.type), event.data)
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

        # What this run is for, in order of how much the loop actually knows.
        #
        # The classifier is **last**, and that is the fix: it used to be the
        # only answer for every message a caller did not label, so a session
        # with work in flight had its intent re-guessed from scratch on every
        # follow-up. Measured on one field transcript, three of five messages
        # in a migration session were guessed "question" -- and a question runs
        # in ASK, which has no write tools and no `submit_plan`, so the model
        # spent forty turns reading a repository it had been told to change.
        #
        # The two rules in front of it are not better guesses. They are facts:
        # an answer to `ask_developer` continues the run that asked, and a
        # follow-up on a plan with open steps continues that plan.
        decided = Intent.coerce(intent)
        source = "given"
        if decided is Intent.AUTO and start is not None:
            decided, source = Intent.coerce(start), "start"
        awaiting, self.state.awaiting = self.state.awaiting, Intent.AUTO
        if decided is Intent.AUTO and continued and awaiting is not Intent.AUTO:
            decided, source = awaiting, "answer"
        if decided is Intent.AUTO and continued and self._work_in_flight():
            decided, source = Intent.AGENT, "session"
        if decided is Intent.AUTO:
            decided, source = self._classify(task, continued=continued), "classified"
        elif (
            decided is Intent.AGENT
            and not continued
            and not self.state.migration.active
        ):
            # The kind is settled and the *shape* is not, and they are different
            # questions.
            #
            # A caller that supplies the intent -- the panel's Agent toggle, the
            # `/migrate` command, any API client -- skips the classifier
            # entirely, and the classifier is the only thing that asks whether
            # this is a whole-service conversion. So the one entry point named
            # after migrating was the one where none of the migration rules
            # engaged: no phased plan, no branch held, no route inventory, and
            # the gate running on a service in the middle of being converted.
            #
            # One call, on the first message of a session that may write, and
            # never when a roadmap already says the answer. `_classify`'s return
            # is discarded here on purpose: the developer has answered that half
            # already and their answer is not the classifier's to revise.
            self._classify(task, continued=continued, kind_known=True)
        self.state.intent = decided
        self.state.intent_source = source

        self._switch(self._opening_mode(decided, continued=continued))
        # Only a run that may write needs to know what was already broken.
        #
        # And not a migration, which needs the opposite. The baseline exists so
        # the gate charges this run only for its own damage; during a conversion
        # the run's own damage *is* the workspace -- a service half on
        # `api-server` and half on `n-api-server` -- so a baseline taken here
        # would be a picture of the previous phase's breakage, handed to the
        # final gate as "already broken, not your fault". The migration's gate
        # runs without one deliberately: a converted service that does not build
        # has not been converted. It also saves about thirty seconds of `go vet`
        # per message on a module that cannot compile.
        if decided is Intent.AGENT and not self.state.migration.defers_gate:
            self._take_baseline()

        for _ in range(self.max_turns):
            if self.result is not None:
                break
            if self.cancelled():
                self.result = self._abort()
                break
            yield from self._turn()
            # The canonical transcript and the compaction sidecar, written at
            # the boundary where the run is already waiting on something slower
            # than a disk. Best-effort, like the journal it goes through: a
            # read-only checkout costs the ability to resume this session
            # exactly, never the work the developer is waiting for.
            self.context.persist()
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

    def _classify(self, task: str, *, continued: bool, kind_known: bool = False) -> Intent:
        """Ask the model, once, what kind of request this is — and what shape.

        ``kind_known`` is the call made for the second answer alone, when the
        caller already supplied the intent. The verdict is still parsed and
        still returned, because the reply carries both; what changes is that
        ``intent_why`` is left alone. That field exists to explain a *misroute*,
        and filling it from a classification the run did not act on would
        attribute the routing to a call that had no part in it.

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
        # And what the agent last said, which is half of what the newest
        # message means. `directives` is the developer's side only, so "do it,
        # and don't run the gates until the end" arrived with no trace that the
        # agent had just put a ten-phase migration on screen -- and was scored
        # on its grammar, "Asking for confirmation of a constraint", rather
        # than on the job it was continuing.
        if continued and (reply := self._last_reply()):
            conversation += f"\n\nTHE AGENT'S LAST REPLY:\n{reply}"

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
                # Room for the verdict *and* its reason. `why` is required
                # now, and left to itself the model writes forty words of it:
                # at 64 tokens the reply was cut mid-JSON, which parses as
                # nothing and falls back to ASK -- the safe direction, but
                # silently wrong on a change, and measured at 2 cases in 60.
                #
                # Two belts. The prompt asks for eight words, which is what the
                # model actually reads and obeys (117 characters average down
                # to 35, worst case 48); this budget is the brace.
                max_tokens=160,
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
        # Kept, not discarded. `why` has been in `_INTENT_SCHEMA` since the
        # classifier was written and the answer went straight in the bin, so the
        # one artefact that could explain a misroute never existed.
        if not kind_known:
            self.state.intent_why = str((parsed or {}).get("why", "") or "").strip()[:300]

        # Only ever set here, never cleared here. A session that has been told
        # once that it is a migration stays one: the later messages of a
        # migration are "carry on", "start phase 3", "yes" -- and a classifier
        # given those in isolation has no reason to say migration, so re-asking
        # would drop the roadmap, the branch rule and the deferred gate exactly
        # when the run is deepest into needing them.
        #
        # Read only off a "change", which is what the prompt asks the question
        # about. A reply that says this changes no files is not evidence that it
        # is a whole-service conversion, whatever it puts in the third field.
        if kind == "change" and bool((parsed or {}).get("migration")):
            self.state.migration.active = True

        if kind != "change":
            # The caller's own answer stands when there is one. This call was
            # made for the shape, and the kind was not its to revise.
            return Intent.AGENT if kind_known else Intent.ASK
        return Intent.AGENT

    def _last_reply(self, limit: int = 600) -> str:
        """The last thing the agent said to the developer, or ``""``.

        Read off the assembled messages rather than remembered, for the reason
        `_live_reads` is: the context is the authority on what the conversation
        contains, and a second copy would eventually disagree with it. Bounded,
        because the classifier gets 160 output tokens and a whole migration plan
        pasted into its prompt would bury the message it is being asked about.
        """
        for message in reversed(self.context.build()):
            if message.role is Role.ASSISTANT and (text := (message.content or "").strip()):
                return text[:limit]
        return ""

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
        steered = False
        for correction in self.steer():
            # Appended as a user message so it lands in the working set the same
            # way the original task did, and the model treats it as instruction
            # rather than as tool output it can weigh against its own plan.
            self.context.append_user(correction)
            self.context.pin_directive(correction)
            yield Event(EventType.STEER, {"text": correction, "turn": self.context.turn})
            steered = True

        if steered:
            # A correction is new input, and the developer typed it expecting a
            # turn. The gate-stall clock counts turns in which nothing changed
            # *and nothing new was said*; a run standing in front of a failing
            # gate used to end on the very turn the developer's "try X instead"
            # arrived, with the message appended to a context nothing would
            # read again. The clock restarts; the bound is unchanged.
            self.state.idle_since_gate = 0

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
        # The task state, from ground truth, at the end of the prompt. The one
        # thing the loop knew and the model was never told.
        self.context.set_state(self._state_block())
        if self._debug is not None:
            self._debug.turn_start(
                turn=turn,
                mode=str(self.state.mode),
                state={
                    name: _describe(group) for name, group in self.state.groups().items()
                },
                context=self.context.inspect(),
                plan=[
                    {"file": s.file, "status": s.status, "action": s.action, "note": s.note}
                    for s in self.state.plan
                ],
            )
        yield Event(
            EventType.TURN_START,
            {
                "turn": turn,
                "mode": str(self.state.mode),
                # Carried on every turn so the panel can say why it is in the
                # mode it is in. This is the decision the whole run turns on and
                # nothing on the wire used to name it.
                "intent": str(self.state.intent),
                # And whether a person said so or a 64-token call guessed. A
                # panel that can render "treating this as work -- switch?" needs
                # to know which, and so does anyone reading a transcript back.
                "intent_source": self.state.intent_source,
                "intent_why": self.state.intent_why,
                # The attempt about to be made, not the number of failures
                # behind it. `gate_failures` is 0 before any gate has failed, so
                # the wire said "attempt 0" while the panel's own default said 1
                # and its grid header counted from 1 — the first column was
                # labelled with a number no other surface used (BUG EXT-7).
                "attempt": self.state.gate_failures + 1,
            },
        )

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
            #
            # And so does the *tool list*, for the same reason one step further
            # down. Ending the phase is the right answer to a stall only when
            # there is nothing left to do; with work the plan still names, a
            # turn cut to `finish` alone (see `_terminal_tools`) is a turn the
            # model can only answer by abandoning that work -- and it answers it
            # accurately: "I am in a read-only phase and cannot write files",
            # which was true of the turn and of nothing else. The refusal in
            # `_phase_ended` then sent it straight back into the same loop.
            #
            # A stall is not a reason to stop. It is a reason to stop *reading*,
            # and `required` over the whole list says exactly that, which is
            # already how the research fence answers the identical situation
            # twenty lines below.
            outstanding = [] if because else self._open_targets()
            if outstanding:
                forced_choice = "required"
                self.context.append_user(
                    "Stop searching. That call has already been answered and asking it "
                    "again returns the same thing.\n\n"
                    "Your plan set out to write " + ", ".join(outstanding) + ". "
                    + ("Write them now" if len(outstanding) > 1 else "Write it now")
                    + ", from what you already have. If a file genuinely cannot be "
                    "written, say which and why in one line and call `finish`."
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
            outstanding = self._open_targets()
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
                    + self._fence_ask()
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

        # What this turn is allowed to call, and how hard.
        #
        # Three cases, and only the last one narrows the tool list. A branch
        # that set `forced_choice` itself wants a *kind* of move -- an edit, or
        # any tool at all -- and the right call there is `write_file` or
        # `patch_file`, so it keeps everything. A turn that is ending the phase
        # gets the terminals and nothing else: that is what makes `required`
        # safe, and what stops a Planner being handed `submit_plan` as its only
        # legal move when the task was a question (BUG L-28).
        tool_choice: str | dict[str, Any] | None = None
        offered = tools
        if answering:
            tool_choice = forced_choice or self._terminal_choice()
            if forced_choice is None:
                offered = self._terminal_tools()
        # Recorded before the call, read after it by `_phase_ended`: a plan that
        # arrives on a turn like this one was not volunteered.
        self.state.terminal_forced = answering and forced_choice is None

        outcome = yield from self._complete(offered, tool_choice=tool_choice)
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

        def dispatch(choice: str | dict[str, Any] | None) -> TurnResult:
            return complete(
                self.context,
                self.client,
                tools=tools,
                tool_choice=choice,
                session_id=self.session_id,
                on_delta=lambda fragment: self._relay(deltas.feed(fragment)),
                debug=self._debug,
            )

        ticker.start()
        try:
            while True:
                try:
                    return dispatch(tool_choice)
                except UnsupportedParameterError:
                    # The endpoint does not take this `tool_choice`. Both uses of
                    # it here are recoveries from a run that is otherwise going
                    # to loop, so falling back is worth a prefill: "required"
                    # degrades to asking again plainly, and a named choice
                    # degrades to `required`, which at least keeps a tool call
                    # on the table.
                    #
                    # Deliberately *not* falling back to `tools=[]`. Measured on
                    # the live endpoint: with no tools the model emits markup for
                    # `Grep` with an `output_mode` parameter -- a tool from
                    # another harness, remembered from training -- and the loop
                    # would serve that to a developer as an answer. An
                    # unconstrained retry is a worse turn; that is a worse
                    # product.
                    #
                    # A loop rather than a retry inside the handler, because a
                    # second refusal raised *inside* an `except` block skipped
                    # every handler below and left `_complete` as an exception
                    # the runtime had to dress up as a crash. Nothing left to
                    # fall back to re-raises into the ordinary error path, which
                    # ends the run ERROR with the endpoint's message on screen.
                    if tool_choice is None:
                        raise
                    yield Event(
                        EventType.GATE,
                        {"kind": "tool_choice_unsupported", "value": str(tool_choice)},
                    )
                    tool_choice = "required" if isinstance(tool_choice, dict) else None
        except OverBudgetError as exc:
            # The context manager exists to prevent this, so reaching it means
            # compaction could not free enough. Compacting harder and retrying
            # once is worth a turn; failing the run outright is not.
            yield from self._compact(retain_pct=0.15, reason="over budget")
            try:
                return dispatch(tool_choice)
            except OverBudgetError:
                self.result = RunResult(
                    Outcome.ERROR,
                    f"context cannot be reduced below budget: {exc}",
                    self.context.turn,
                    tuple(self.router.touched),
                )
                return None
        except Exception as exc:  # noqa: BLE001 - the transport failing is not the model's fault
            # The endpoint refusing the request as too large is the one failure
            # here that is recoverable, and it was being ended as ERROR with the
            # machinery to recover sitting three lines above.
            #
            # It happens because the local estimate and the server's tokeniser
            # disagree: `Calibration` narrows that gap but cannot close it, and
            # the disagreement is largest exactly where it matters, on a context
            # near the budget. `OverBudgetError` is the estimate noticing; this
            # is the server noticing first.
            #
            # Deterministic compaction, not the summariser: a run that has just
            # been refused for sending too much should not answer by sending a
            # summarisation request built from the same context. One retry, and
            # only if the prompt actually got smaller -- retrying an unchanged
            # request is how a recovery becomes a loop with a bill attached.
            if _context_length_error(exc) and not self._overflow_recovered:
                self._overflow_recovered = True
                before = self.context.usage().total
                yield from self._compact(
                    retain_pct=0.15, reason="overflow recovery", strategy="basic"
                )
                after = self.context.usage().total
                yield Event(
                    EventType.GATE,
                    {
                        "kind": "overflow_recovery",
                        "before": before,
                        "after": after,
                        "retrying": after < before,
                    },
                )
                if after < before:
                    try:
                        return dispatch(tool_choice)
                    except Exception as retried:  # noqa: BLE001 - report the second one
                        exc = retried
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
        if self.state.retrieval_repeats >= MAX_RETRIEVAL_REPEATS:
            tools = [s for s in tools if s["function"]["name"] != "search_docs"]
        # And one withheld, which is the same mechanism pointed the other way.
        #
        # `ask_developer` is dispatchable in the acting phase -- the registry
        # says so, and `_phase_ended` has always handled the call from any mode.
        # It is *shown* there only during a migration. The unknowns a conversion
        # runs into do not arrive while planning, which is when the Planner
        # holds this tool: the field type, the route base and the branch to cut
        # surface while the file is being converted, and the acting phase's only
        # answers to one were to guess or to stop. A forty-file conversion can
        # afford neither.
        #
        # Withheld rather than added, because the two locks are not symmetric:
        # `schemas_for` decides what the model is shown and the router decides
        # what it may run. Making this conditional on the schema side keeps
        # ordinary acting turns exactly as they were, while a replayed stale
        # schema list still dispatches correctly instead of being refused.
        if self.state.mode is Mode.AGENT and not self.state.migration.active:
            tools = [t for t in tools if t["function"]["name"] != "ask_developer"]
        return tools

    def _migration_guard(self, call: ToolCall) -> ToolResult | None:
        """Hold a migration's first write until the branch it belongs on exists.

        The SOP's step one, enforced rather than described. A conversion is
        reviewable and revertible as one unit only if it is one branch cut from
        ``development``; started on whatever the developer happened to be
        standing on, it is forty commits interleaved with everybody else's work
        and there is no revert.

        Three things narrow it to the case it is for, because a guard that fires
        anywhere else is a guard that stops ordinary work:

        * only when the run is a migration;
        * only for a tool that writes, and never for ``git_ops`` itself -- the
          one call that can clear the condition must not be the one it blocks;
        * only when the workspace is a git repository at all. "If a git repo is
          provided" is the rule; a service opened as a plain directory has no
          branches to cut and holding its writes would be refusing the task over
          a condition it cannot satisfy.

        The refusal names the whole move, including the confirmation: which
        branch to cut and what to cut it from is a decision about somebody
        else's repository, and ``ask_developer`` is how this loop asks.
        """
        migration = self.state.migration
        if not migration.active:
            return None
        # A branch, *and* one that is not the branch everybody else is on.
        #
        # The recorded branch comes from `git_ops`, which reports whatever it
        # checked out -- including `development` itself, if that is what it was
        # asked for. Treating that as "the migration has a branch" would let the
        # conversion land as forty commits on the shared branch, which is the
        # one outcome step one exists to prevent.
        if migration.branch and migration.branch.lower() not in PROTECTED:
            return None
        spec = registry.get(call.name)
        if spec is None or not spec.mutates or call.name == "git_ops":
            return None
        if not (self.router.workspace.root / ".git").is_dir():
            return None
        self._save_routes()
        on = (
            f" It is on {migration.branch}, which is a shared branch."
            if migration.branch
            else ""
        )
        return ToolResult.failure(
            f"{call.name} was not run: this is a service migration and it is not on "
            f"a migration branch yet.{on}",
            fix=(
                "Do this first, in order. 1) `git_status` — it lists the local "
                "branches, so you can see whether `"
                + BASE_BRANCH
                + "` exists. 2) `ask_developer` — confirm the branch to cut and what "
                "to cut it from, naming what you found: `"
                + BASE_BRANCH
                + "` if it is there, otherwise the branch they want replicated. Do "
                "not assume it. 3) `git_ops` with op=branch, message="
                + DEFAULT_BRANCH
                + " and base=<branch to cut from>, which creates the branch with "
                "that base's code in it. The writes are held until then; nothing "
                "else about the plan changes."
            ),
            meta={"migration": "needs a branch"},
        )

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
                # The file is named when the prefix carries it, and so is what
                # has actually landed on disk: a model that lost a write to the
                # limit has no other way to tell "I wrote it" from "I meant to".
                target = _partial_path(call.arguments)
                landed = (
                    "Written this run so far: " + ", ".join(self.router.touched)
                    if self.router.touched
                    else "Nothing has been written this run yet"
                )
                body = (
                    f"Your call to {call.name}"
                    + (f" for {target}" if target else "")
                    + " arrived cut off -- the arguments stop partway through, so "
                    "the call was not made"
                    + (f" and {target} is unchanged" if target else "")
                    + ". Nothing is wrong with your JSON; this is what running into "
                    f"the {config_for(self.state.mode).max_tokens:,}-token output "
                    "limit looks like.\n\n"
                    + self._shorter_reply(call.name, alone)
                    + f"\n\n{landed}."
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
        # rather than looping. The rule lives on the ledger that owns the
        # fields, so a sixth one added there cannot be missed here.
        self.state.calls.world_changed(self.router.mutations)

        mutated = False
        #: Whether anything this batch changed was a change the run had not
        #: already made and undone. Distinct from ``mutated``, which is about
        #: the disk: a turn that deletes a file it wrote back last turn has
        #: mutated the workspace and moved the run nowhere. See ``churn``.
        productive = False
        #: Dispatched calls that told the run something it did not already have.
        informed = 0
        #: Files this batch deleted that the plan did not ask to have removed.
        #: Reported after the batch, on the turn it happened. See the mutation
        #: handling below.
        removed_open: list[str] = []
        #: Files this batch deleted for at least the second time, having written
        #: them back in between. A different objection from ``removed_open`` and
        #: it has to be, because ``removed_open``'s remedy is what produced it.
        churned: list[str] = []

        # What in this batch will not be dispatched, and why. Three rules, all
        # about the batch rather than any one call: a call repeated verbatim in
        # the same reply is answered once; calls past MAX_CALLS_PER_BATCH are
        # not run; and `finish` sent alongside other calls is refused, because
        # an answer written before the results it asked for arrived is an
        # answer to nothing -- and the model that batches them is the one the
        # output limit cuts off mid-answer.
        skipped: dict[str, str] = {}
        seen_in_batch: set[str] = set()
        others = [c for c in calls if c.name != "finish"]
        for position, call in enumerate(calls):
            fingerprint = _fingerprint(call)
            if fingerprint in seen_in_batch:
                skipped[call.id] = (
                    "it repeats a call made earlier in this same reply; the answer "
                    "above is the answer."
                )
                continue
            seen_in_batch.add(fingerprint)
            if call.name == "finish" and others:
                skipped[call.id] = (
                    "it was sent in the same reply as other calls. Read their "
                    "results first, then call `finish` on its own, in a reply with "
                    "nothing else in it."
                )
                continue
            if position >= MAX_CALLS_PER_BATCH:
                skipped[call.id] = (
                    f"more than {MAX_CALLS_PER_BATCH} calls were sent in one reply; "
                    "ask for it again in a later turn."
                )

        # A batch that is all reads runs at once.
        #
        # The calls used to run one after another on the worker thread, which
        # is right for writes -- two `patch_file` calls against one file must
        # not interleave, and the undo store's first-write-wins rule is about
        # order -- and needlessly slow for six `read_file` calls that touch
        # nothing. `hooks.parallel_safe` is the predicate and it is
        # conservative: nothing that mutates, nothing that needs approval,
        # nothing whose provider serialises its own work anyway.
        #
        # All or nothing. Splitting a batch into a parallel group and a serial
        # one would reorder results relative to the calls that produced them,
        # and a transcript's value depends on a read appearing where the model
        # asked for it. The loop below is unchanged: it consumes a result that
        # is already in hand instead of dispatching one.
        runnable = [c for c in calls if c.id not in skipped]
        prefetched: dict[str, ToolResult] = {}
        prefetched_intercepts: dict[str, tuple[str, str, str]] = {}
        intercepts_ready = False
        if (
            len(runnable) >= 2
            and not self.cancelled()
            and parallel_batch(runnable, registry.get)
        ):
            # The intercept decisions are taken first, once, for the whole
            # batch -- otherwise a call answered from a ledger would still have
            # been dispatched in parallel, and the ledgers would be consulted
            # twice. Safe to hoist *here* and nowhere else: every call in a
            # parallel batch is a read, so none of them can change what the
            # ledgers would have said about the ones after it.
            intercepts_ready = True
            for call in runnable:
                if (answer := self._intercept(call, _fingerprint(call))) is not None:
                    prefetched_intercepts[call.id] = answer
            fresh = [c for c in runnable if c.id not in prefetched_intercepts]
            if len(fresh) >= 2:
                prefetched = self._dispatch_parallel(fresh)

        for index, call in enumerate(calls):
            if call.id in skipped:
                self.context.append_tool_result(
                    call.name,
                    f"{call.name} was not run: {skipped[call.id]}",
                    tool_call_id=call.id,
                )
                yield Event(
                    EventType.TOOL_RESULT,
                    {
                        "id": call.id,
                        "name": call.name,
                        "ok": False,
                        "turn": self.context.turn,
                        "dispatched": False,
                        "content": f"not run: {skipped[call.id]}",
                    },
                )
                continue
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

            intercepted = (
                prefetched_intercepts.get(call.id)
                if intercepts_ready
                else self._intercept(call, fingerprint)
            )
            if intercepted:
                body, said, intercept_kind = intercepted
                # The previous answers to this same question are stubbed out in
                # the projection when the new one is appended.
                #
                # Not deleted -- the record keeps its place and its
                # `tool_call_id`, so nothing is orphaned. What is removed from
                # *what the model reads* is the
                # *pattern*: N identical (call -> "answered from the previous
                # result") pairs sitting in history are a few-shot demonstration
                # of exactly the behaviour the answer is asking the model to
                # stop, and the transcript wins that argument. Measured on the
                # live endpoint: one pair and the model moves on 5/5; two and it
                # repeats forever 5/5. A field run made the same `git_ops
                # commit` call seven times and another the same `search_repo`
                # eight times, each intercepted correctly and each answered into
                # a transcript that told it to do it again.
                #
                # Tagged as an echo of this exact call, and that is the whole
                # of it: the projection keeps the newest answer to each repeated
                # question and stubs the rest, on every turn, without the loop
                # having to remember which message to go back and rewrite --
                # and without anything being rewritten. The earlier answers are
                # still in the canonical transcript exactly as they were sent.
                self.context.append_tool_result(
                    call.name, body, tool_call_id=call.id, echo=fingerprint
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

            hook_notes: list[str] = []
            arguments_to_run: Any = call.arguments
            if self.hooks.any_before:
                context = HookContext(
                    call=call,
                    arguments=args if isinstance(args, dict) else {},
                    mode=self.state.mode,
                    turn=self.context.turn,
                )
                decision = self.hooks.run_before(context)
                if decision.note:
                    hook_notes.append(decision.note)
                if decision.arguments is not None:
                    arguments_to_run = decision.arguments
                if decision.stops:
                    # A hook that stops a call answers it, exactly as a router
                    # refusal does. The alternative -- dropping the call -- is
                    # the malformed shape the coherence pass exists to repair,
                    # manufactured on purpose.
                    stopped = (
                        ToolResult.success(decision.answer)
                        if decision.answer
                        else ToolResult.failure(decision.deny, fix=decision.fix)
                    )
                    yield from self._hooked_result(call, stopped, hook_notes)
                    continue

            if held := self._migration_guard(call):
                yield from self._hooked_result(call, held, hook_notes)
                continue

            outcome = prefetched.get(call.id)
            if outcome is None:
                outcome = self.router.dispatch(
                    call.name, arguments_to_run, mode=self.state.mode
                )

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

            if self.hooks.any_after:
                after = self.hooks.run_after(
                    HookContext(
                        call=call,
                        arguments=args if isinstance(args, dict) else {},
                        mode=self.state.mode,
                        turn=self.context.turn,
                    ),
                    outcome,
                )
                if after.result is not None:
                    outcome = after.result
                if after.note:
                    hook_notes.append(after.note)

            # A tool refused because this mode does not hold it says nothing
            # about the call, only about who is asking. It is not progress and
            # it is never cached: the fingerprint carries no mode, so a refusal
            # earned in one mode would answer the identical call made in the
            # mode that *can* run it.
            refused_by_mode = bool(outcome.meta.get("refused_by_mode"))
            # "Informed" means the run learned something, not that a tool ran.
            # It used to be the second: any dispatched, non-mode-refused call
            # counted, so a search that found nothing, a search that found the
            # same lines under different words, and a build log the model had
            # already read all reset the stall counter -- and the stall counter
            # is what the forced `finish` is wired to. Three tests now: the
            # result body is one the run has not seen (the same search under
            # other words returns the same body), it is not an empty finding,
            # and the overlap test below did not say it repeats.
            digest = _body_digest(call.name, outcome.for_model())
            # Asked of the context, not of a set the loop keeps. "Has this run
            # already been told this" and "can the model read it right now" are
            # the same question, and keeping two answers to it is what made a
            # compaction silently turn news back into old news -- or, worse,
            # leave a body counted as seen after the message carrying it had
            # stopped being visible.
            novel = digest not in self.context.visible_bodies
            if not refused_by_mode and novel and not _empty_finding(outcome):
                informed += 1
            mutated = mutated or bool(outcome.mutations)
            if call.name == "go_mod":
                self.state.dependencies_changed = True
            # Where the migration's branch rule is satisfied, and it is read off
            # the tool that did it rather than off the model's account of it.
            # `git_ops` reports the branch it actually checked out, which is the
            # only statement about the repository that cannot be wrong.
            if call.name == "git_ops" and outcome.ok and outcome.meta.get("branch"):
                self.state.migration.branch = str(outcome.meta["branch"])
                self.state.migration.base = str(outcome.meta.get("base") or "")
                # Written now rather than at the next plan change: the branch is
                # the fact a developer opening the document first wants, and the
                # next change to the plan may be several turns away.
                self._save_progress()

            self.state.calls.asked(fingerprint)
            # Terminal calls are never cached. `_intercept` already declines to
            # answer them from a ledger, so this is belt and braces -- but the
            # entry it used to write is the one that deadlocked the run, and a
            # cache nothing reads is a trap for the next ledger added here.
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

            # A file that was just written is worth reading again.
            for mutation in outcome.mutations:
                self.state.reads.pop(mutation.path, None)
                if mutation.kind is MutationKind.DELETE:
                    # **A delete is not a write**, and treating it as one loses
                    # the file.
                    #
                    # `write_file` refuses to overwrite, so the way to replace a
                    # file is to delete it and write it again -- and a field run
                    # did exactly that to `handler/paogen.go` (6,571 lines),
                    # `handler/publicacct.go`, `handler/transferentry.go` and
                    # `handler/objectionfile.go`, deleting each one, never
                    # writing the replacement, and moving to the next step. Four
                    # files gone. The deletion counted as the step's mutation,
                    # so the step went `written`, the cursor advanced, and
                    # nothing in the run's own state disagreed.
                    #
                    # Unless the step *is* the deletion. `routes.go`, `docs.go`
                    # and the swaggo artefacts are removed by this migration on
                    # purpose, and a step that says so is satisfied by the
                    # delete -- otherwise this becomes a condition no run can
                    # clear, which is the failure this file keeps rebuilding.
                    # `action` is a typed field the model filled in to say what
                    # the step does; reading it is not the same as reading prose
                    # out of a reply.
                    cycles = self._note_delete(mutation.path)
                    if cycles:
                        # Told about whatever the plan says, because a turn that
                        # goes round stops counting as progress whatever the
                        # plan says, and a stall counter ticking with nothing
                        # explaining it is the state this run was already in.
                        churned.append(mutation.path)
                    else:
                        productive = True
                    if self._step_wants_removal(mutation.path):
                        self._mark_steps(mutation.path, "written")
                        continue
                    # `removed` is the ledger of files *lost*, and it is written
                    # here rather than above the branch for a reason a field run
                    # paid for: a planned deletion used to enter it too, and
                    # `_deleted_and_not_replaced` reads it against the disk and
                    # nothing else. So a migration that removed `routes/routes.go`
                    # exactly as its plan said had its first `finish` refused with
                    # "you deleted routes/routes.go ... so that file is simply
                    # gone", and only got past it because MAX_FINISH_REFUSALS is
                    # 1. A deletion the plan asked for is not a loss.
                    self.state.removed.add(mutation.path)
                    self._mark_steps(
                        mutation.path,
                        "pending",
                        "deleted; its replacement has not been written",
                    )
                    # One objection per path, and not this one while it cycles:
                    # "write it back" is the instruction that produced the
                    # second deletion, and repeating it is repeating the loop.
                    if not cycles:
                        removed_open.append(mutation.path)
                    continue
                if not self._note_write(mutation.path):
                    productive = True
                self.state.removed.discard(mutation.path)
                # A mutation on a plan step's file is that step *written* --
                # from the change set, which cannot lie, rather than from the
                # model saying so.
                #
                # Written, not done. This used to be `done`, which meant a step
                # was finished the instant a write landed on it, before anything
                # looked at what had been written: a file written wrongly was a
                # completed step. `_inner_loop`, which runs after this batch and
                # is already scoped to the touched files, is what promotes it.
                self._mark_steps(mutation.path, "written")

            slice_path, slice_range = _slice_path(call, outcome)
            appended = self.context.append_tool_result(
                call.name,
                outcome.for_model(),
                tool_call_id=call.id,
                path=slice_path,
                line_range=slice_range,
                # Recorded on the message so the projection can answer, later
                # and without the loop's help, whether this exact call's result
                # is still readable and whether its body is still news.
                fingerprint=fingerprint,
                body=digest,
                ok=outcome.ok,
                mutation=bool(outcome.mutations),
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

            for note in hook_notes:
                # As a `role: user` message inside a `<hook_context>` block
                # with sanitised attributes. A hook that could emit a
                # `role: tool` message could tell the model that `go_build`
                # passed, and the model has no way to tell the difference.
                self.context.append_user(
                    hook_context_block("hook", call, note), visibility=Visibility.MODEL
                )

            if note := self._overlap(call, outcome):
                # As a user message. It carries no `tool_call_id` because no
                # tool produced it, and a `role: tool` message without one is
                # malformed on the wire and a lie in the transcript -- the old
                # loop had 17 such call sites, teaching the model that
                # `go_build` returns paragraphs of instructions.
                self.context.append_user(note)
                # A search that returned only places the run already had did
                # not inform this turn, so it must not count as progress.
                informed -= 1

            yield Event(
                EventType.TOOL_RESULT,
                {
                    "id": call.id,
                    "name": call.name,
                    "turn": self.context.turn,
                    **outcome.as_dict(),
                },
            )

            if call.name == "revise_plan" and outcome.ok:
                yield from self._revised(outcome)

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
        #
        # ``productive`` rather than ``mutated``, and the difference is one
        # field session: a run that deletes a file, is told to write it back,
        # writes it back and deletes it again has mutated the workspace four
        # times and changed nothing. Every one of those turns reset this
        # counter, so the six-stall bound never came near firing.
        if informed > 0 or productive:
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
                self.result = self._stalled()
                return

        if churned:
            # First, because it is the objection that supersedes the other one.
            # A path in here has already been through `removed_open`'s remedy --
            # "write it back" -- and come out the far side deleted again, so
            # repeating that remedy is repeating the loop.
            #
            # What it asks for instead is a decision, and it names the two
            # things that can be true. Either the plan step really does remove
            # the file, in which case its `file` or its `action` is written in a
            # way `_step_wants_removal` cannot read and `revise_plan` fixes it in
            # one call; or it does not, in which case the file stays. The model
            # is the only party that knows which, and until now nothing asked it.
            names = ", ".join(dict.fromkeys(churned))
            them = "them" if len(churned) > 1 else "it"
            self.context.append_user(
                f"You have deleted {names} again, having written {them} back after "
                "the last time. That is a cycle, and this is the second time round.\n\n"
                "Nothing about the workspace differs between those two turns, so a "
                "third delete lands exactly where this one did. Decide instead:\n\n"
                f"- If your plan step removes {them}, leave {them} deleted and work "
                "the next step. If the step is not closing on the delete, its `file` "
                "names more than one path or its `action` does not say the file is "
                "removed -- `revise_plan` with one step per file, `action` saying it "
                "is deleted, fixes both.\n"
                f"- If the step does not remove {them}, leave {them} on disk and work "
                "the next step.\n\n"
                "Going round again ends the run as stalled."
            )

        if removed_open:
            # Said immediately, on the turn the file went, because that is the
            # only turn on which the model still holds what was in it. One turn
            # later the read is out of context and the replacement has to be
            # written from a file that no longer exists.
            #
            # After the batch rather than inside it: a user message between an
            # assistant's tool calls and their results is the malformed shape
            # the coherence pass exists to repair.
            names = ", ".join(dict.fromkeys(removed_open))
            self.context.append_user(
                f"You deleted {names} and the plan does not say to remove "
                + ("them" if len(removed_open) > 1 else "it")
                + ". Nothing is there now.\n\n"
                "If that was to rewrite the file, write it in this turn -- you are "
                "holding what was in it and the next turn will not be. `write_file` "
                "creates it, and `append=true` adds the rest if it does not fit in "
                "one reply.\n\n"
                "The step has been put back to pending, so it is still open and the "
                "run cannot finish while it is."
            )

        if mutated:
            # Writing is not research. The fence exists to stop a phase spent
            # reading and never deciding; a turn that changed a file has decided,
            # and counting it drove the acting phase into a wall at ~12 turns on
            # a product that advertises 400 (BUG L-2).
            self.state.research_turns = 0
            yield from self._inner_loop()

    def _dispatch_parallel(self, calls: Sequence[ToolCall]) -> dict[str, ToolResult]:
        """Run a batch of read-only calls at once, and return them by call id.

        Bounded by ``MAX_PARALLEL_CALLS`` threads: the work is IO-bound on one
        developer's disk, and the marginal thread past four buys contention.

        Anything that does not come back as a plain ``ToolResult`` is dropped
        from the map rather than used, so the sequential path below dispatches
        it normally. That covers the case ``parallel_safe`` is written to
        exclude and cannot fully guarantee -- a tool that asks for approval --
        and it fails towards the slow, correct behaviour rather than towards a
        blocked thread pool.

        A raised exception is dropped the same way. It will be raised again on
        the sequential dispatch, where the loop's existing error handling can
        see it in the right order.
        """
        out: dict[str, ToolResult] = {}
        if not calls:
            return out
        mode = self.state.mode
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CALLS, len(calls))) as pool:
            futures = {
                pool.submit(self.router.dispatch, call.name, call.arguments, mode=mode): call
                for call in calls
            }
            for future in futures:
                call = futures[future]
                try:
                    outcome = future.result()
                except Exception:  # noqa: BLE001 - re-run in sequence, in order
                    continue
                if isinstance(outcome, ToolResult):
                    out[call.id] = outcome
        return out

    def _hooked_result(
        self, call: ToolCall, outcome: ToolResult, notes: Sequence[str]
    ) -> Iterator[Event]:
        """Record a result a hook produced instead of the tool, and report it.

        Kept separate from the dispatch path because a hook's decision is not a
        tool call: nothing was dispatched, no mutation happened, and no ledger
        should learn anything about the tool from it. What the model gets is the
        same shape it gets from any refusal, which is the whole point -- a
        denied call the model is never told about is a call it will make again.
        """
        self.context.append_tool_result(
            call.name, outcome.for_model(), tool_call_id=call.id, ok=outcome.ok
        )
        for note in notes:
            self.context.append_user(
                hook_context_block("hook", call, note), visibility=Visibility.MODEL
            )
        yield Event(
            EventType.TOOL_RESULT,
            {
                "id": call.id,
                "name": call.name,
                "turn": self.context.turn,
                "dispatched": False,
                "hooked": True,
                **outcome.as_dict(),
            },
        )

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
        # A phase-ending call is a state transition, not a question, and all
        # three ledgers below answer questions. `finish` dispatched once and
        # sent back by `_phase_ended` -- "your plan set out to write this file
        # and it has not been written" -- was cached like any other result, so
        # the retry that refusal asks for was answered from the cache and never
        # reached the check that would let it through. `MAX_FINISH_REFUSALS` is
        # one, `finish_refused` therefore stuck at one forever, and the run
        # spent its whole budget replaying "that is the current answer. Use it
        # and move to the next step" at a model whose next step was the exit it
        # kept being denied. Six turns of it, then NO_PROGRESS (BUG L-26).
        #
        # A repeated terminal call is a *signal* -- the model is trying to stop
        # -- and the bounded refusals in `_phase_ended` are what answer it.
        if call.name in _TERMINAL:
            return None

        # A known dead end. The tool itself declared this exact call unable to
        # succeed, so asking again cannot change the answer.
        if reason := self.state.dead_ends.get(fingerprint):
            self.state.calls.asked(fingerprint)
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
        # ...and only while the model can still read the answer it is being
        # pointed at. This is the exact shape of the failure the canonical
        # transcript exists to prevent: the cache said "you already have this",
        # a compaction had projected a recap over the message carrying it, and
        # the two statements could not both be obeyed (BUG L-10, L-25). The
        # ledger is no longer the authority on that -- the request is, and
        # ``visible_results`` is computed from the request.
        if cached is not None and fingerprint not in self.context.visible_results:
            self.state.calls.forget(fingerprint)
            cached = None
        if cached is not None and not wants_more:
            asks = self.state.calls.asked(fingerprint)
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

    def _fence_ask(self) -> str:
        """What the research fence asks for — and it names only what it allows.

        The Planner used to be told "submit the plan now, or ask the developer
        what you cannot infer" on a turn whose ``tool_choice`` named
        ``submit_plan`` alone, so half the instruction was a move the request
        forbade. The text has to agree with the request -- and now it can agree
        with a wider one, because the turn offers all three terminals.

        Naming the alternatives matters more than it looks. A model twelve turns
        into validating a document, told only "submit the plan", writes a plan:
        it is the one move on offer and the instruction confirms it. Told that
        `finish` is what a review ends with, it can say what it found.
        """
        if self.state.mode is not Mode.PLANNER:
            return "Say what you have done and what you found."
        if self.state.forced_terminal:
            # The second force. `finish` is named on the wire because a schema
            # refused the first attempt; the text says the same thing.
            return (
                "Call `finish` now: say what you established about the task in `answer`, "
                "and what you could not find out in `blocked`."
            )
        return (
            "End this phase now. Re-read what the developer actually asked for, at "
            "the top of this conversation, and pick the call that answers it:\n\n"
            "- Did they ask to be TOLD something -- to validate, review, audit, "
            "check, compare, explain, list? Then call `finish`, with the whole of "
            "what you found in `answer`. That call is the answer they read. A plan "
            "is not what they asked for and they cannot use one.\n"
            "- Did they ask for the code to CHANGE -- a feature, a fix, a migration, "
            "a refactor? Then call `submit_plan`, naming the files you would change.\n"
            "- `ask_developer` only if one unknown blocks everything else."
        )

    def _why_not_done(self) -> str:
        """Why this run is not finished, or ``""`` when nothing says otherwise.

        One predicate over what were three separate checks, in three places,
        each with its own bound and its own idea of what "done" means:
        ``_open_targets`` (the plan names files nothing has written),
        ``_gate_wants_an_edit`` (a failing gate is asking for a change), and the
        ``finish_refused`` counter that decides whether either may speak again.

        Folding them is Cline's ``completionGuard`` -- a predicate that returns
        the reason the run is not done, or nothing -- and it is worth taking for
        a reason their version does not have: here the three checks *interact*.
        A plan with unwritten targets and a failing gate is one situation, and
        answering it twice reads to the model as two different objections to the
        same move. A run that has been pushed back on once has had its say.

        The bounds stay where they were and stay measured. What changes is that
        "is this run done" is answerable in one place, by callers that used to
        each ask half the question.
        """
        if self.state.finish_refused >= MAX_FINISH_REFUSALS:
            # It has been sent back once. The model may have decided a step is
            # unnecessary, and it is entitled to say so and be believed; what it
            # is not entitled to is silence, and it has now spoken twice.
            return ""
        if gone := self._deleted_and_not_replaced():
            # First, because it is the one objection that names work already
            # *lost* rather than work not yet started. A run that finishes here
            # leaves the developer a repository with four handlers missing.
            return (
                "you deleted "
                + ", ".join(gone)
                + " and "
                + ("none of them have" if len(gone) > 1 else "it has not")
                + " been written again, so "
                + ("those files are" if len(gone) > 1 else "that file is")
                + " simply gone"
            )
        if missing := self._open_targets():
            return (
                "your plan set out to write "
                + ", ".join(missing)
                + (" and none of them have been written" if len(missing) > 1
                   else " and it has not been written")
            )
        if unverified := [s for s in self.state.plan if s.status == "written"]:
            # Written but not clean. A distinct objection from "you never wrote
            # it", and the plan could not state it at all before the `written`
            # status existed -- the step was `done` the moment the write landed,
            # so a file written wrongly closed its step.
            names = ", ".join(s.file for s in unverified[:STATE_ITEMS])
            return (
                f"{names} " + ("have" if len(unverified) > 1 else "has")
                + " been written but the formatter and the contract linter are not "
                "clean on "
                + ("them" if len(unverified) > 1 else "it")
                + " yet"
            )
        if self._gate_wants_an_edit():
            report = self.state.last_gate
            blocker = getattr(report, "blocked_by", None) if report else None
            where = f" at {blocker}" if blocker else ""
            return f"the verification gate is failing{where} and nothing has been edited since"
        return ""

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

    def _terminal_choice(self) -> str | dict[str, Any]:
        """How this mode is made to stop, when it must.

        ``"required"``, paired with a tool list cut down to the terminals by
        `_terminal_tools`. The old fear -- that ``required`` lets the model pick
        a research tool and carry on -- is answered by removing the research
        tools from the turn, which is a fact about the request rather than a
        name the model may or may not honour.

        Naming ``submit_plan`` here was the more expensive mistake. A Planner at
        the fence was given exactly one legal move, so a run that had spent
        twelve turns *validating* a document wrote an eight-step migration
        instead -- the outcome the developer had not asked for, produced under
        duress and then enforced for the rest of the session (BUG L-28). The
        three terminals are three different answers to "what was this task?",
        and only the model knows which one it has been working on.

        **Once**: if the arguments the model sends do not satisfy a schema, the
        second force is ``finish``, whose schema is one required string and
        which therefore cannot fail the same way. Forcing a call that keeps
        being refused is the loop this whole mechanism exists to escape,
        arriving through the escape.
        """
        if self.state.forced_terminal:
            return _FORCE_FINISH
        return "required"

    def _terminal_tools(self) -> list[dict[str, Any]]:
        """Every way this mode can end its phase, and nothing else.

        What makes ``tool_choice: "required"`` safe on a turn that has to stop.
        In ASK and AGENT this is ``finish`` alone, so the turn is as constrained
        as a named choice; in PLANNER it is the three, which is the point.

        Falls back to the full list if a mode somehow holds no terminal: a turn
        offered zero tools with ``required`` is a request no endpoint can
        satisfy, and an unconstrained turn is a far better failure than one the
        gateway rejects.
        """
        tools = [s for s in self._tools() if s["function"]["name"] in _TERMINAL]
        return tools or self._tools()

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
            # Held for the answer. This is the only follow-up whose intent the
            # loop knows rather than guesses, and it is the one the classifier
            # was measured getting wrong: a Planner's four questions about a
            # migration came back answered and were read as "asking for
            # validation, not code changes".
            self.state.awaiting = self.state.intent
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
            missing = self._open_targets()
            if reason := self._why_not_done():
                self.state.finish_refused += 1
                plural = len(missing) > 1
                self.context.append_user(
                    f"Not yet. {reason[0].upper()}{reason[1:]}.\n\n"
                    + (
                        "You have read enough to write "
                        + ("them" if plural else "it")
                        + " now. Do that. "
                        if missing
                        else "Make the change the gate is asking for. "
                    )
                    + "If "
                    + ("a file" if missing else "that")
                    + " genuinely should not be "
                    + ("written" if missing else "done")
                    + " after all, call `finish` again and say which and why in "
                    "`blocked` -- that will be taken at face value.\n\n"
                    # The third move, and the one the model does not find on its
                    # own. A field run diagnosed its own plan correctly in the
                    # prose of a `finish` -- "the go.mod cleanup should be the
                    # last step, not the first" -- and never called
                    # `revise_plan`, because nothing had ever named it as the
                    # answer to "the plan is wrong". It had two advertised
                    # exits, both of which were "stop".
                    "If the *plan* is what is wrong -- the steps are in the "
                    "wrong order, or one of them turned out to be unnecessary "
                    "-- call `revise_plan` with the corrected steps and say why."
                )
                return

            # An answer that is the model looping is sent back, once.
            #
            # Before the plan check reads it and before the developer sees it,
            # because a degenerate answer is not a claim about the work at all
            # -- refusing it for the wrong reason would teach the model to fix
            # something that is not broken. See `_is_degenerate`.
            #
            # Unconditional on `blocked` and on the change set, unlike the
            # preamble check below: an answer that has stopped being language
            # is worthless however much was written, and a `blocked` field
            # attached to it is not a reason to keep it.
            if (
                _is_degenerate(answer)
                and self.state.degenerate_refused < MAX_DEGENERATE_REFUSALS
            ):
                self.state.degenerate_refused += 1
                self.context.append_user(
                    "Your `answer` repeated itself instead of ending: the same phrase "
                    "runs over and over for thousands of characters. That is not what "
                    "you meant to send, and the developer would have read all of it.\n\n"
                    "Call `finish` again and keep the answer short -- a few hundred "
                    "words. Say what you did, what you found, and what is left. If "
                    "there is a long list to give, give its length and its first few "
                    "entries rather than all of it."
                )
                return

            # An answer that is only its own opening line is sent back, once.
            #
            # The other way twenty turns of work reaches nobody. `answer` is
            # the whole delivery -- there is no prose after it, the way there
            # is in a chat -- and the model writes as if there were: "Here is
            # my assessment of each step's accuracy:" and nothing after the
            # colon. Two field runs ended exactly there, and in one of them the
            # developer's next message was "where is the assessment".
            #
            # Cheap to reject on purpose. Re-sending the same text unchanged is
            # accepted as final, so a wrong guess costs one turn rather than an
            # argument with a model that is right.
            if (
                not blocked
                and not self.router.touched
                and _is_preamble(answer)
                and self.state.preamble_refused < MAX_PREAMBLE_REFUSALS
            ):
                self.state.preamble_refused += 1
                self.context.append_user(
                    "Your `answer` reads as the opening of something longer: it says "
                    "what is coming and then stops.\n\n"
                    "The developer sees `answer` and nothing else -- there is no reply "
                    "after it. Whatever you were about to write next has to be inside "
                    "it. Call `finish` again with the whole thing.\n\n"
                    "If that really was the whole answer, send it again unchanged and "
                    "it will be taken as final."
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
            # Answered -- and, if the session had committed work it did not
            # touch, that too.
            #
            # `_open_targets` is silent outside AGENT on purpose: this phase has
            # no write tools, so pushing back on the `finish` would be asking
            # for a move the turn forbids, which is the permanently
            # unsatisfiable condition the rest of this file is at pains to
            # avoid. But a *report* costs nothing and is the difference between
            # a developer reading "answered" and reading that seven planned
            # files are still untouched. One field run ended exactly here, with
            # "the migration.md file is written and ready for execution" and a
            # migration nobody had started.
            self.result = RunResult(
                Outcome.DONE,
                (f"answered; blocked on: {blocked}" if blocked else "answered")
                + self._unfinished(),
                self.context.turn,
                tuple(self.router.touched),
            )
            return

        # Whether this plan was volunteered or extracted. The turn that
        # produced it recorded which; `_open_targets` is what reads it.
        self.state.plan_forced = self.state.terminal_forced
        self.state.forced_terminal = 0
        # The research fence and the narration re-ask are both once per
        # *phase*, which is what the reasoning behind them was always about: a
        # Planner that has decided there is nothing to plan should not be
        # forced twice over the same decision. `forced` was scoped to the run,
        # so a Planner that consumed it handed the acting mode a phase with no
        # narration recovery at all — and the acting mode is where narration
        # costs the most, because a "Making the edit now" with no tool call is
        # a turn in which nothing was edited (prior-audit TC-4).
        self.state.progress.phase_ended()
        steps = self._normalise_plan(steps_from_meta(dict(outcome.meta)))
        phases = phases_from_meta(dict(outcome.meta))
        # A roadmap is itself the evidence. A plan that arrives with phases is a
        # phased plan whatever the 160-token classifier decided, and the
        # classifier is the weaker witness of the two.
        if phases and len(phases) >= MIN_PHASES:
            self.state.migration.active = True
        if self.state.migration.active and (
            objection := plan_objection(
                self.state.migration, phases, steps, lines=self._line_count
            )
        ):
            # Sent back rather than adopted, and *only* for a migration. The
            # `submit_plan` handler cannot make this check -- it does not know
            # which kind of run it is in, and a tool that demanded phases of
            # every plan in the product would demand them of the three-step bug
            # fixes too.
            #
            # Bounded by the same budget every other push-back is: at
            # MAX_PLAN_OBJECTIONS the plan is taken as it stands. A condition
            # the model cannot satisfy that never stops asking is the failure
            # this file keeps rebuilding by accident, and a migration planned in
            # two phases is worse than one planned in seven but much better than
            # a run that spends its budget arguing about the shape of the plan.
            if self.state.plan_objections < MAX_PLAN_OBJECTIONS:
                self.state.plan_objections += 1
                self.context.append_user(
                    f"That plan was not adopted: {objection}.\n\n"
                    "Call `submit_plan` again with the whole thing — `phases` for the "
                    "roadmap and `steps` for the phase that opens now. The work has "
                    "not started and nothing is lost."
                )
                return
        if phases:
            self.state.migration.adopt(phases)
        deferred: list[PlanStep] = []
        if self.state.migration.active and self.state.migration.phases:
            # Trimmed to the open phase rather than refused. A model that has
            # just thought about seven phases sends the steps for two of them,
            # and that is a good plan in the wrong shape -- see
            # `migration.steps_for_phase`.
            kept, deferred = steps_for_phase(self.state.migration.phases, steps)
            steps = tuple(kept)
        replanned = bool(self.state.plan) and self.state.replans > 0
        yield from self._adopt_plan(steps, str(outcome.meta.get("summary") or ""))
        if deferred:
            # Said, not silently dropped. A plan whose second half vanished
            # without a word is a plan the model will re-send.
            names = ", ".join(dict.fromkeys(s.file for s in deferred if s.file))
            self.context.append_user(
                f"{len(deferred)} step(s) in that plan belong to a later phase and "
                f"were not adopted: {names}. The roadmap has them; they become work "
                "when their phase opens, and the plan for that phase is written then, "
                "against a workspace this one will have changed.\n\n"
                "Work the steps above and nothing else."
            )
        if replanned:
            # A new approach gets the full gate bound. MAX_REPLANS is what keeps
            # this finite: the second failed approach ends the run as before.
            self.state.gate_failures = 0
        self._switch(Mode.AGENT)


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
        # The verification node. A step goes `written` when a mutation lands on
        # it and reaches `done` only here, when the formatter and the contract
        # linter come back clean over what was written. Before this there was no
        # node between REQUIRED MUTATIONS and MARK PHASE COMPLETE at all, so
        # "done" was a synonym for "a write happened".
        #
        # This costs nothing extra: the inner loop already runs after every
        # mutating batch, sub-second, scoped to the touched files. What changes
        # is that its verdict is recorded against the plan instead of being
        # spoken once into the transcript and forgotten.
        self._verify_written(report)
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
            # Forced once already and still no `submit_plan`. That is a planner
            # with nothing to plan, which is a legitimate answer to a request
            # that turned out not to need a change -- and the honest thing is to
            # say so rather than manufacture a plan and run a gate on it.
            #
            # Unless this Planner was *sent back*. A replan that produces no
            # plan is not "nothing needed changing": files were written and a
            # gate failed on them, and DONE would say otherwise.
            if self.state.replans > 0 or self.router.touched:
                report = self.state.last_gate
                self.result = RunResult(
                    Outcome.UNVERIFIED,
                    "a revised plan was asked for after the gate failed, and none "
                    "was submitted. The edits are in the workspace"
                    + (
                        f"; the gate was last blocked at {report.blocked_by.name}"
                        if report is not None and report.blocked_by
                        else ""
                    )
                    + self._unfinished(),
                    self.context.turn,
                    tuple(self.router.touched),
                    report,
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
            missing = self._open_targets()
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

        # A migration that is not finished does not run the gate.
        #
        # Not a softening and not a bypass: the gate is a question about whether
        # this run's work is sound, and mid-conversion it has no true answer to
        # give. Phase two swaps `api-server` for `n-api-server` in `go.mod` and
        # phase five converts the last handler; every build between those two
        # points fails, correctly, on code the plan has not reached yet. Running
        # it anyway costs seventy seconds to produce a failure the loop then
        # treats as this run's -- `_gate_failed` sends it back to the model,
        # which cannot fix it without doing the remaining four phases in one
        # turn, and two rounds of that ends the run.
        #
        # So the phase closes, the developer is told exactly what landed and
        # what is next, and the run stops there. The gate runs when the last
        # phase closes, and `defers_gate` is false from that moment -- a
        # deferral that outlived the migration would be an exemption.
        if self.state.migration.defers_gate:
            yield from self._phase_checkpoint()
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
        self.state.gate_turn = self.context.turn
        yield Event(EventType.GATE, {"kind": "full", **report.as_dict()})

        if report.ok:
            self.state.gate_failures = 0
            # Before the summary is built, because the summary reads the plan.
            self._settle_written()
            self.result = RunResult(
                Outcome.DONE,
                self._done_summary(report),
                self.context.turn,
                tuple(self.router.touched),
                report,
            )
            return

        yield from self._gate_failed(report, rerun=True)

    def _phase_checkpoint(self) -> Iterator[Event]:
        """End a migration run at the phase boundary, with the gate still held.

        The counterpart of `_verify` for a run that must not be verified yet,
        and the place requirement four is enforced: a migration stops at every
        phase and hands back to the developer, rather than rolling on to the
        next one. That is not politeness. A conversion carried out in one
        unbroken run is one nobody reads until it is forty files long, on a
        model whose output budget has been cut off three times by then -- which
        is the shape of the field session this whole area exists because of.

        What the developer gets is what the loop can prove: which phase closed,
        which files the change set actually holds, what is still open, and what
        the next phase is. Nothing here is read out of the model's prose.
        """
        closed = self._close_phase()
        if not self.state.migration.defers_gate:
            # The last phase just closed, so the conversion is supposed to
            # compile. `_verify` re-entered here runs the gate for real -- and
            # with no baseline, because nothing that is broken now was broken
            # before the migration started.
            yield from self._verify()
            return

        migration = self.state.migration
        here = migration.working(self._plan_phase())
        total = len(migration.phases)
        touched = self.router.touched
        written = ", ".join(touched[:STATE_ITEMS]) + (
            f" and {len(touched) - STATE_ITEMS} more" if len(touched) > STATE_ITEMS else ""
        )

        if closed and here is not None:
            index, nxt = here
            headline = (
                f"phase {index - 1} of {total} — {closed} — is complete. "
                f"Next is phase {index}: {nxt.name}"
                + (f" ({nxt.covers})" if nxt.covers else "")
            )
        elif closed:
            headline = f"phase {closed} is complete and it was the last one"
        elif here is not None:
            index, phase = here
            open_steps = [s.file for s in self.state.plan if s.open][:STATE_ITEMS]
            headline = (
                f"phase {index} of {total} — {phase.name} — is still open"
                + (": " + ", ".join(open_steps) + " not done yet" if open_steps else "")
            )
        else:
            headline = "the migration has no phases recorded yet"

        yield Event(
            EventType.GATE,
            {
                "kind": "phase",
                "deferred": True,
                "closed": closed,
                "phase": here[1].name if here else "",
                "index": here[0] if here else total,
                "phases": total,
            },
        )
        self.result = RunResult(
            Outcome.DONE,
            headline
            + ". Written: "
            + (written or "nothing")
            + f". The branch is {migration.branch or 'not set'}"
            + (
                f", and the {self.state.routes_before} route(s) this service served "
                "before the migration are recorded, so a lost one is caught when the "
                "last phase closes"
                if self.state.routes_before
                else ""
            )
            + f". The plan, the phases and what is done are in {PROGRESS_PATH}, "
            "rewritten on every change — the Migration view reads it, and so does "
            "the next session"
            + ". The verification gate has not run and will not until the last "
            "phase closes — a half-converted service cannot build, so a gate now "
            "would report the conversion's own middle as a failure. Say when to "
            "open the next phase, or say what to change about this one first",
            self.context.turn,
            tuple(touched),
        )

    def _close_phase(self) -> str:
        """Close the open phase when its steps have all settled. Its name, or ``""``.

        From the plan, never from the model saying so -- the same rule
        `_mark_steps` follows, one level up. A step is settled when it is
        ``done``, ``written`` or ``skipped``; ``written`` counts because the
        inner loop is the only verification a migration gets before the end, and
        holding a phase open on a formatter that could not run is the
        permanently-unsatisfiable condition this file keeps rebuilding by
        accident.

        **The fallback matters more than the rule.** When no step carries the
        open phase's name -- the model submitted a roadmap and then plain steps,
        which it will -- the whole plan settling closes the phase. Without that,
        a roadmap with one untagged plan under it is a migration that can never
        reach its last phase and therefore never runs the gate at all.
        """
        migration = self.state.migration
        here = migration.working(self._plan_phase())
        if here is None or not self.state.plan:
            return ""
        _, phase = here
        key = phase.name.strip().lower()
        scoped = [s for s in self.state.plan if s.phase.strip().lower() == key]
        steps = scoped or list(self.state.plan)
        if any(step.open for step in steps):
            return ""
        if not migration.close(phase.name):
            return ""
        self._save_plan()
        return phase.name

    def _deleted_and_not_replaced(self) -> list[str]:
        """Files this session removed that are still not on disk.

        Checked against the filesystem rather than trusted from the ledger, for
        the reason every other check here is: the developer edits the workspace
        between messages, and a file they restored themselves is not this run's
        problem. A path that is back drops out of the ledger as it is read, so
        the objection cannot outlive the thing it is about.
        """
        if not self.state.removed:
            return []
        back: list[str] = []
        gone: list[str] = []
        for path in sorted(self.state.removed):
            try:
                exists = self.router.workspace.resolve(path).exists()
            except (PathEscape, ValueError):
                exists = False
            (back if exists else gone).append(path)
        self.state.removed.difference_update(back)
        return gone[:STATE_ITEMS]

    def _cited_but_unrunnable(self) -> list[str]:
        """Tools the plan's ``accepts`` criteria name that the acting phase lacks."""
        pool = _unrunnable_checks()
        found: list[str] = []
        for step in self.state.plan:
            for word in _IDENTIFIER.findall(step.accepts.lower()):
                if word in pool and word not in found:
                    found.append(word)
        return found

    def _note_unrunnable_criteria(self) -> None:
        """Say so when the plan is checked by tools the phase that works it cannot call.

        A note, not an objection. The plan is adopted either way, and that is the
        point: `MAX_PLAN_OBJECTIONS` is two, the migration shape objections have
        first claim on both, and spending one on the wording of a field would buy
        a re-plan round trip to fix something the model can simply be told. The
        steps themselves are fine -- it is the criterion that cannot be applied.

        Sent once, at adoption, where the model is about to start work and can
        still decide what "done" will look like for each step.
        """
        named = self._cited_but_unrunnable()
        if not named:
            return
        which = ", ".join(f"`{name}`" for name in named[:STATE_ITEMS])
        is_are = "is a tool" if len(named) == 1 else "are tools"
        self.context.append_user(
            f"One note on the plan before you start: {which} {is_are} the acting "
            "phase does not have -- they belong to ask and planner -- and the steps "
            "above are accepted on them. Calling one from here is refused, so those "
            "steps would have no check you can actually apply.\n\n"
            "Check them with what this phase runs: `go_build`, `go_vet`, `go_test`, "
            "`rules_lint`, or a `read_file`/`search_repo` that shows the thing is "
            "true -- \"go build succeeds\", \"no api-* import is left in go.mod\", "
            "\"the file is gone\". The plan stands; this is about how you verify it."
        )

    def _note_delete(self, path: str) -> int:
        """Record a deletion, and return how many cycles this path has been through.

        A cycle is delete, write back, delete again. It is the shape of the loop
        this counter exists to catch, and the reason nothing else caught it is
        that every turn in it was a real mutation: `stalled_turns` resets on
        ``mutated``, so a run oscillating on one file reset its own stall
        counter on every turn of the oscillation and ran until the finish-refusal
        budget happened to end it, eighteen turns later.

        Zero on a first deletion, which is ordinary work -- replacing a file
        means deleting it, and a migration removes files outright.
        """
        cycles = self.state.churn.get(path, 0)
        if path in self.state.rewritten:
            self.state.rewritten.discard(path)
            cycles += 1
            self.state.churn[path] = cycles
        self.state.gone_once.add(path)
        return cycles

    def _note_write(self, path: str) -> int:
        """Record a write, and return the path's cycle count.

        A write to something this session deleted arms the next deletion of it
        as a cycle. Any write does: `write_file`, an `append`, a `patch_file`
        against the replacement. What closes the cycle is the *second delete*,
        not the shape of the restoration.
        """
        if path in self.state.gone_once:
            self.state.rewritten.add(path)
        return self.state.churn.get(path, 0)

    def _step_wants_removal(self, path: str) -> bool:
        """Whether a plan step covering ``path`` asked for it to be deleted.

        Read off ``action`` -- a typed field the model filled in to say what the
        step does -- and only the step or steps that cover this path. A
        migration genuinely removes files: ``routes/routes.go``, ``docs/docs.go``
        and the swaggo artefacts all go, and a step that says so has to be
        satisfiable by the deletion that carries it out. Without this, the guard
        against losing a file becomes a condition no run can clear.

        Narrow on purpose. It is not asking what the reply said; it is asking
        whether *this step's own one-sentence description of itself* is a
        removal.
        """
        verbs = ("delete", "remove", "drop", "retire")
        return any(
            step.covers(path) and any(v in step.action.lower() for v in verbs)
            for step in self.state.plan
        )

    def _plan_phase(self) -> str:
        """The phase the current plan's steps are for, or ``""``.

        The plan is the authority on which phase is being worked -- it is the
        commitment -- and the roadmap is the authority on the order. They differ
        legitimately, which is why this is read rather than assumed: a developer
        whose branch already exists opens the run at phase two while phase one
        is still pending, and closing the roadmap's answer when that work
        settled would close the wrong phase.

        The cursor's phase while there is a cursor, so the state block names the
        phase the next edit belongs to; the first step's otherwise, so it still
        answers once every step has settled -- which is exactly when
        `_close_phase` asks. A migration's plan carries one phase
        (`plan_objection` refuses a submission that spans two), so the two
        readings agree on every plan this is reached with.
        """
        active = self.active_step
        if active is not None and active[1].phase:
            return active[1].phase
        return next((s.phase for s in self.state.plan if s.phase), "")

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
        if rerun:
            # A failure after an edit is an attempt that did not work. Recorded
            # where the state block will show it, and the steps it names are
            # marked failed rather than left looking pending.
            self._note_tried(report)
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

        if rerun and self.state.gate_failures >= 2 and self._can_replan():
            blocker = report.blocked_by.name if report.blocked_by else "the gate"
            yield from self._replan(
                f"the gate has failed {self.state.gate_failures} times at {blocker}, "
                "the last time after an edit meant to fix it"
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

    def _opening_mode(self, intent: Intent, *, continued: bool) -> Mode:
        """Which mode this run starts in.

        A question starts in ASK and work starts in PLANNER, with one exception
        that the field made expensive: **a follow-up on a plan that still has
        open steps starts in AGENT.**

        Every AGENT run used to enter PLANNER unconditionally, and PLANNER has
        no "the plan stands, carry on" exit -- its terminals are `submit_plan`,
        `ask_developer` and `finish`. So a developer typing "complete the
        migration plan you wrote" or "start phase 2" left the model exactly one
        forward move: write the plan again. One field session re-submitted an
        identical eight-step plan three times across four messages, and before
        `_adopt_plan` learned to merge, each re-submission also reset the two
        steps that were genuinely finished.

        Re-planning is still available and is one tool call away: `revise_plan`
        is visible in AGENT, and `_replan` still sends a run back to the Planner
        when the gate fails twice after an edit. What is gone is being *made* to
        re-plan in order to reach the work.

        Deliberately narrow. It needs a plan, open steps *and* a follow-up: a
        first message plans, and a plan whose steps are all settled plans again,
        because a developer asking for more work on a finished plan is asking
        for a new one.
        """
        if intent is not Intent.AGENT:
            return Mode.ASK
        if continued and self._work_in_flight():
            return Mode.AGENT
        return Mode.PLANNER

    def _work_in_flight(self) -> bool:
        """Whether this session has committed work that is not finished.

        The one question both the mode and the intent turn on, so it is asked
        once. ``_opening_mode`` reads it as "carry on rather than re-plan";
        ``_run`` reads it as "this is a change session, do not re-guess".

        **A forced plan does not count.** `plan_forced` records a plan that came
        back because the research fence left the model no other move, and the
        developer may have asked a question the whole time. Treating one as a
        commitment is how a request to *validate* a migration plan turns into
        the migration (BUG L-28) -- and doing it here would be worse than doing
        it in the plan, because it would pin every later message in the session
        to AGENT as well.

        Once anything has been written the distinction lapses, exactly as it
        does in `_open_targets`: a run that has started acting on a plan is
        working to it, whatever produced it.
        """
        if not any(step.open for step in self.state.plan):
            return False
        return not self.state.plan_forced or bool(self.router.touched)

    def restore_plan(self, session_id: str) -> bool:
        """Reload this session's plan from disk. ``False`` when there is none.

        The restart counterpart of ``carry_from``. A plan lived in a tuple in a
        process, so a VS Code window reload at step 4 of 7 left the panel
        showing four steps done and the agent believing it had never planned
        anything -- and the first thing it did was plan again, against a
        workspace its own earlier steps had already changed.

        The statuses come back with the steps. They were derived from the change
        set, and the change set is on disk, so restoring them is restoring a
        conclusion the loop had already reached from evidence that has not
        moved.
        """
        record = PlanRecord.load(self.router.workspace.root, session_id)
        if record is None or not record.steps:
            return False
        self._plan_record = record
        self.state.plan = record.steps
        self.state.plan_summary = record.summary
        self.state.plan_forced = record.forced
        # With the plan, because half of it is useless: steps restored without
        # the roadmap resume a migration into a phase the run cannot name, and a
        # roadmap restored without the steps resumes a phase with no work in it.
        # They are one atomic file for exactly this reason.
        self.state.migration = record.migration
        rendered = "\n".join(step.rendered(i) for i, step in enumerate(record.steps, 1))
        if record.summary:
            rendered = f"{record.summary}\n\n{rendered}"
        self.context.set_plan(rendered)
        return True

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
        self.state.retrievals = list(previous.state.retrievals)
        self.state.retrieval_repeats = previous.state.retrieval_repeats
        # What did not work is still true on the next message; what the results
        # looked like may not be, so the body and search-place ledgers start
        # empty like `last_results` does.
        self.state.tried = list(previous.state.tried)
        self.state.baseline = previous.state.baseline
        self.state.plan = previous.state.plan
        self.state.plan_summary = previous.state.plan_summary
        # With the plan, because it is a fact *about* the plan. A cursor that
        # restarted at each developer message would read "turn 1 on this step"
        # at precisely the moment the run has been stuck on it longest -- the
        # loop this counter exists to break spanned three messages.
        self.state.cursor = previous.state.cursor
        # The question the previous run stopped on. Carried because the whole
        # point of it is to be read by the *next* message, and `_State` is
        # built per message: without this it is set and thrown away in the same
        # breath, and the answer to `ask_developer` is classified from scratch.
        self.state.awaiting = previous.state.awaiting
        # Carried with the plan it is about. A forced plan that survived into a
        # follow-up message is still a forced plan, and the follow-up is where
        # it does the most damage: `finish_refused` starts at 0 in a fresh
        # `_State`, so without this the "you planned to write these" refusal
        # fires again on a plan the developer never asked for.
        self.state.plan_forced = previous.state.plan_forced
        # Still true on the next message: a file deleted and not replaced is
        # deleted and not replaced whatever the developer types next.
        self.state.removed = set(previous.state.removed)
        # And the churn ledger with it. The loop it catches spanned three
        # developer messages in the field -- a counter that restarted at each
        # one would have read "no cycles yet" on every turn the run was most
        # stuck, which is the same mistake `cursor` above exists to avoid.
        self.state.gone_once = set(previous.state.gone_once)
        self.state.rewritten = set(previous.state.rewritten)
        self.state.churn = dict(previous.state.churn)
        # And the route inventory, which is taken once per *migration*, not
        # once per message: retaking it on message four would record a
        # half-converted service as the thing to compare the finished one
        # against, and the comparison would pass by construction.
        self.state.routes_saved = previous.state.routes_saved
        self.state.routes_before = previous.state.routes_before
        # The roadmap, the branch and which phases have closed. A migration is
        # many messages by construction -- the roadmap is agreed on the first
        # and phase five lands on the ninth -- so a `MigrationState` rebuilt per
        # message would re-cut the branch, re-plan phase one, and run the gate
        # on a service in the middle of being converted. Carried as the object,
        # not copied field by field, so a field added to it later is carried
        # without this line having to learn about it.
        self.state.migration = previous.state.migration
        # And its history, so a follow-up that revises the plan appends to the
        # record rather than starting a new one that claims this is the first
        # plan the session has had.
        self._plan_record = previous._plan_record
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

        **And a field naming several files becomes several steps.** ``file`` is
        one path by specification and `PlanStep.covers` compares it as one; a
        field holding ``"go.work, go.work.sum"`` matched nothing, which cost a
        field run its whole first phase. `split_paths` has the case and the
        rule. Splitting happens here, before `plan_objection` sees the plan, so
        the big-file check measures each path rather than a string that names
        three of them and resolves to none.
        """
        out: list[PlanStep] = []
        for step in steps:
            if not step.file:
                out.append(step)
                continue
            for path in self._paths_named_by(step.file):
                out.append(step if path == step.file else replace(step, file=path))
        return tuple(out)

    def _paths_named_by(self, file: str) -> tuple[str, ...]:
        """The normalised workspace-relative paths one ``file`` field names."""
        candidates = split_paths(file)
        if len(candidates) > 1 and self._on_disk(file):
            # A file that is actually there is never a list, whatever is in its
            # name. Checked against the disk rather than argued about, for the
            # reason `_deleted_and_not_replaced` is: the workspace is the only
            # thing that knows, and a repository holding `a, b.go` is allowed to
            # have a plan step for it.
            candidates = (file.strip(),)
        return tuple(dict.fromkeys(self._relative(path) for path in candidates))

    def _relative(self, path: str) -> str:
        """``path`` as the change set spells it, or verbatim when it will not resolve."""
        try:
            return self.router.workspace.relative(self.router.workspace.resolve(path))
        except (PathEscape, ValueError, OSError):
            return path

    def _on_disk(self, path: str) -> bool:
        try:
            return self.router.workspace.resolve(path).exists()
        except (PathEscape, ValueError, OSError):
            return False

    def _unwritten_targets(self) -> list[str]:
        """Plan steps whose file no change reached.

        Both sides are workspace-relative POSIX paths: `touched` because
        `_confine` normalises every argument, the plan because `_normalise_plan`
        does the same at `submit_plan`.
        """
        if not self.state.plan:
            return []
        touched = self.router.touched
        return [
            s.file
            for s in self.state.plan
            if s.file and s.open and not any(s.covers(t) for t in touched)
        ]

    def _is_plan_target(self, path: str) -> bool:
        """Whether ``path`` is a file the current plan sets out to change.

        Compared against the normalised step paths, which is the same form
        ``router.touched`` uses -- `_normalise_plan` put them there so that a
        dot-slash prefix and a Windows separator stop making three names out of
        one file. The read arrives already confined and workspace-relative for
        the same reason, so both sides of this are POSIX and relative.

        Status is not consulted. A step already `done` had its read ledger
        cleared by the mutation that finished it, so the question does not
        arise; a step `skipped` is one the model may still be reading to
        justify skipping. What matters is only that the run was sent here to
        change this file.
        """
        return any(step.covers(path) for step in self.state.plan)

    def _open_targets(self) -> list[str]:
        """Plan targets this run is answerable for not having written.

        `_unwritten_targets` answers "what does the plan still name?". This
        answers the question the three enforcement paths actually ask, which is
        "what is this run in the wrong for?" -- and they are not the same
        question when the plan was never volunteered.

        A plan produced by a forced `submit_plan`, on a run that has written
        nothing since, is not a commitment. It is what came back when the fence
        left the model no other move, and the developer may never have asked
        for work at all.

        All three enforcement paths read this, and the scope was measured
        rather than argued. Restricting it to `_verify` alone -- on the
        reasoning that the fence's "write them now" and `_phase_ended`'s "not
        yet" are recoverable pushes, since each names `finish` as the way to
        decline -- took the field scenario to **0 runs in 5**: every one was
        pushed into writing files for a request to *validate* a document, and
        once anything is written this guard correctly lapses, so all five then
        committed to the migration and exhausted their budget.

        What this cannot do is tell a question from a job. It only knows the
        plan was not volunteered, so it stops the loop *insisting*. A model
        that works a forced plan on its own initiative is not caught here and
        is not meant to be: that is the misroute, and it belongs to the
        classifier.

        The moment anything *is* written, the guard lapses. A run that has
        started acting on a plan is working to it, whatever produced it, and
        half-finished work is what `_verify` exists to catch.
        """
        if self.state.mode is not Mode.AGENT:
            return []
        if self.state.plan_forced and not self.router.touched:
            return []
        return self._unwritten_targets()

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

    # -- the task state -----------------------------------------------------

    def _state_block(self) -> str:
        """Where the task stands, from ground truth, for the end of the prompt.

        The loop had a correct control state machine and no task state machine,
        and the task state it did hold -- the change set, the plan, the last
        gate -- was never shown to the model. So the model's only evidence about
        its own progress was the transcript, including its own statements of
        intent, and "I'll write migration.md" three turns ago read exactly like
        a report that it had. This is that state, rendered every turn.

        Derived, never model-written: the lists come from ``router.touched``,
        the gate report and the loop's own ledgers, none of which the model can
        edit. Empty when there is nothing to say (an ASK run that has read two
        files), so the block is not noise on a question.
        """
        lines = [f"# Current state — turn {self.context.turn}, {self.state.mode} phase"]

        # Above the plan, because it is the frame the plan sits in: the steps
        # below are one phase of it, and a model that reads them without that
        # frame is a model that finishes the plan and reports the migration
        # done. `MigrationState.block` renders a cursor for the same reason
        # `_plan_block` does.
        lines.extend(self.state.migration.block(self._plan_phase()))
        if self.state.migration.active and not self.state.migration.branch:
            # Two different instructions, because the two modes hold two
            # different sets of tools and an instruction a mode cannot obey is
            # the worst thing that can be put in front of a model.
            #
            # This said "confirm one with `ask_developer`, then `git_ops`
            # op=branch" in both. `git_ops` is an acting tool; the Planner was
            # being told, every turn, in the last position of its prompt, to
            # make a call its own request did not offer. A field session did
            # exactly that -- five refused `git_ops` calls, four identical
            # questions to the developer, and no plan -- because the one move it
            # had been told to make was the one it could not make, and the move
            # that would have unblocked it was never named.
            lines.append(
                "Migration: no branch yet. "
                + (
                    "Cut it before anything else: `git_ops` op=branch "
                    "message=<name> base=<branch to cut from>. Every write is "
                    "held until it exists."
                    if self.state.mode is Mode.AGENT
                    else "You cannot cut it here — `git_ops` is an acting tool. "
                    "Settle *which* branch to cut it from (`ask_developer`, if the "
                    "developer has not already said), then `submit_plan`. Cutting "
                    "it is the first step of the first phase, and the acting "
                    "phase does it."
                )
            )

        if self.state.plan:
            lines.extend(self._plan_block())
            if self.state.plan_forced and not self.router.touched:
                lines.append(
                    "  (this plan was written on a turn that accepted no other call. "
                    "If the task was a question, `finish` with the answer is the right "
                    "way to end -- the plan is not a commitment.)"
                )

        touched = self.router.touched
        if touched or self.state.mode is Mode.AGENT:
            shown = ", ".join(touched[:STATE_ITEMS])
            more = len(touched) - STATE_ITEMS
            lines.append(
                "Written this run: "
                + (shown + (f" and {more} more" if more > 0 else "") if touched else "nothing yet")
            )

        report = self.state.last_gate
        if report is not None:
            verdict = (
                "PASS"
                if report.ok
                else f"FAIL at {report.blocked_by.name if report.blocked_by else 'the gate'}"
            )
            lines.append(f"Last gate: {verdict} (turn {self.state.gate_turn})")
        elif self.state.mode is Mode.AGENT and touched:
            lines.append("Last gate: not run yet on these files")

        ruled = self._ruled_out()
        if ruled:
            lines.append("Ruled out:")
            lines.extend(f"  - {item}" for item in ruled[-STATE_ITEMS:])

        return "\n".join(lines) if len(lines) > 1 else ""

    @property
    def active_step(self) -> tuple[int, PlanStep] | None:
        """The step the run is on, as ``(1-based index, step)``, or ``None``.

        The first step that still asks for work, then the first written but
        unverified one. ``None`` means every step is settled, which is the
        condition for the phase being over.

        The order matters. A written-but-unverified step is *nearly* finished
        and the cheapest thing in the plan to close, but a pending step is work
        that has not started -- and a run that jumped to polishing while three
        steps had never been attempted is the shape of a run that finishes
        nothing. Pending first.
        """
        for index, step in enumerate(self.state.plan, 1):
            if step.open:
                return index, step
        for index, step in enumerate(self.state.plan, 1):
            if step.status == "written":
                return index, step
        return None

    def _plan_block(self) -> list[str]:
        """The plan as a cursor, not as a checklist.

        This is the ACTIVE PHASE node, and it is a rendering decision with a
        measured cause. The block used to list every step and its status on
        every turn. A model handed eight pending items and a 16,384-token output
        budget attempts all eight: a field session died with three replies in a
        row cut off mid-tool-call, having written two files out of eight steps,
        and its own prose was an essay about the remaining six.

        One step, named, with the criterion it is measured against. What is
        finished is summarised by number rather than re-listed, because the
        model does not need to re-read work it cannot change; what is next is
        named as one line, because the point of a plan is that the model can see
        it is going somewhere without trying to get there this turn.

        The statuses are still ground truth -- `written` and `done` come from
        the change set and the inner gate, never from the model saying so -- so
        this is a smaller view of the same facts, not a softer one.

        **And the cursor has to move.** The first version of this block did not:
        while step 2 stayed pending it rendered the same bytes every turn, in
        the last position of the prompt, phrased as an instruction. A field
        session opened fifteen consecutive replies with a verbatim restatement
        of it -- "I'll now work on step 2: creating handler/request.go ... Let
        me first check what already exists" -- and restarted the same
        reconnaissance each time, because nothing it read said the
        reconnaissance had already happened. The checklist it replaced was
        static too and did not do this; the difference is that a checklist is
        something to consult and this is something to obey.

        So the active line carries how long the cursor has been on this step and
        what has landed on it. Both are ground truth, both change under the
        model's own feet, and between them they turn "do step 2" into "you have
        been doing step 2 for seven turns and nothing has been written".
        """
        plan = self.state.plan
        settled = [
            f"{i} {s.file}"
            for i, s in enumerate(plan, 1)
            if s.status in ("done", "skipped")
        ]
        active = self.active_step

        lines: list[str] = []
        if active is None:
            report = self.state.last_gate
            if report is not None and not report.ok:
                # Every step settled and the gate still failing. Saying "all
                # settled" alone would be the plan contradicting the line under
                # it, and a plan that declares completion while verification
                # fails is the exact node this work exists to fix -- MARK PHASE
                # COMPLETE must not fire ahead of VERIFICATION.
                #
                # It happens legitimately: `_note_tried` only marks a step
                # failed when the blocking stage's output names its file, and a
                # build error often names a package or another file entirely.
                where = report.blocked_by.name if report.blocked_by else "the gate"
                lines.append(
                    f"Plan: every step is written, but the gate is failing at {where}, "
                    "so the work is not done. Fix that before finishing."
                )
            else:
                lines.append(f"Plan: all {len(plan)} step(s) settled.")
            if settled:
                lines.append("  Done: " + ", ".join(settled[-STATE_ITEMS:]))
            blocked = [f"{i} {s.file}" for i, s in enumerate(plan, 1) if s.status == "failed"]
            if blocked:
                lines.append("  Failed: " + ", ".join(blocked[-STATE_ITEMS:]))
            return lines

        index, step = active
        state = "" if step.status == "pending" else f" [{step.status}]"
        where = f" of phase {step.phase}" if step.phase else ""
        lines.append(
            f"Now: step {index} of {len(plan)}{where}{state} — {step.file} — {step.action}"
        )
        if step.part:
            lines.append(f"  Part: {step.part}")
        if step.accepts:
            lines.append(f"  Accepts: {step.accepts}")
        if step.note:
            lines.append(f"  Note: {step.note}")
        if elapsed := self._cursor_age(step):
            lines.append(f"  {elapsed}")
        if settled:
            lines.append("  Done: " + ", ".join(settled[-STATE_ITEMS:]))
        nxt = next(
            (
                f"step {i} — {s.file}"
                for i, s in enumerate(plan, 1)
                if i > index and (s.open or s.status == "written")
            ),
            "",
        )
        lines.append(f"  Next: {nxt}" if nxt else "  Next: nothing — this is the last step.")
        return lines

    def _cursor_age(self, step: PlanStep) -> str:
        """How long the cursor has sat on this step, as one line, or ``""``.

        The line that makes the block move. Everything in it is read off the
        loop rather than off the model: the turn counter, and whether anything
        in the change set covers the step's file.

        The cursor is reset by the *step* changing, not by the turn advancing,
        so it survives a follow-up message -- which is where it is needed most.
        The loop that this exists to break spanned three developer messages, and
        a counter that restarted at each one would have read "turn 1 on this
        step" every time the model was most stuck.

        Silent on the turn the cursor lands, because "you have been here for no
        turns" is noise, and silent with no plan or no turns yet so the
        rendering tests that build a loop by hand see the block they assert on.

        Writes state from what reads as a rendering path, which is worth one
        line: `_state_block` has a single call site and runs once a turn, and
        this is idempotent within a turn anyway -- a second call in the same
        turn finds the step unchanged and computes an age of zero.
        """
        turn = self.context.turn
        if step.file != self.state.cursor[0]:
            self.state.cursor = (step.file, turn)
            return ""
        since = self.state.cursor[1]
        turns = turn - since
        if turns < 1:
            return ""
        landed = any(step.covers(path) for path in self.router.touched)
        return (
            f"On this step since turn {since} — {turns} turn(s) so far, and "
            + (
                f"{step.file} has been written. What is outstanding is its "
                "verification, not more of the work."
                if landed
                else f"nothing has been written to {step.file} yet. "
                "Reading more is not what closes it."
            )
        )

    def _settle_written(self) -> None:
        """A clean full gate settles every step still sitting at ``written``.

        `_verify_written` is the only other thing that promotes them and it runs
        only from `_inner_loop`, which runs only after a mutating batch. So a
        step whose file the run never touched again was stranded at ``written``
        for the life of the session: ``open`` is false, so `_open_targets` could
        not ask for it and no push-back ever named it as work; meanwhile
        `_why_not_done` objected to it on every `finish` and `active_step` kept
        the cursor pinned to it. Unreachable in both directions at once.

        The gate is a superset of the inner loop's two checks and it has just
        come back clean over the whole change set, so this is the strongest
        verdict available about those files -- stronger than the one that would
        have promoted them. There is nothing left for a ``written`` step to be
        waiting on.
        """
        if not any(step.status == "written" for step in self.state.plan):
            return
        self.state.plan = tuple(
            replace(step, status="done", note="") if step.status == "written" else step
            for step in self.state.plan
        )
        self._save_plan()

    def _ruled_out(self) -> list[str]:
        """What this run has established does not work, one line each."""
        out = list(self.state.tried)
        for fingerprint, reason in list(self.state.dead_ends.items())[-STATE_ITEMS:]:
            out.append(f"{fingerprint.split(':', 1)[0]}: {reason}")
        if self.state.retrieval_repeats >= MAX_RETRIEVAL_REPEATS:
            out.append("search_docs: the knowledge base does not cover the last question asked")
        if self.state.search_repeats >= MAX_RETRIEVAL_REPEATS:
            out.append(
                "search_repo: the last searches returned only places already found; "
                "the pattern is not in the workspace"
            )
        return out

    def _verify_written(self, report: GateReport) -> None:
        """Promote written steps to done, or say what is holding them.

        Called with the inner loop's verdict, which is scoped to the files this
        batch touched. A clean verdict means what was written passes the two
        checks that can be run in under a second — the formatter and the
        contract linter — and that is the most this node can honestly assert. It
        is not a build, and a step reaching ``done`` here is not a claim that
        the run is correct; the gate still has the last word at the end of the
        phase.

        A dirty verdict leaves the step ``written`` with the blocking stage
        named. That is the state the plan could not previously express: the file
        exists, so "you never wrote it" is false, and the work is not finished,
        so "done" is false too.

        Findings that were already in the file before the run started do not
        hold a step back. ``_lint_is_old_news`` is the same test the message to
        the model uses, and a legacy file's pre-existing violations are not this
        step's failure to fix.
        """
        if not self.state.plan:
            return

        # `report.ok` would make this a no-op: the inner loop's own docstring
        # says nothing in it blocks, so `ok` -- "no stage blocked and none was
        # skipped" -- is true of every inner report. Findings arrive as
        # `warnings`, which is what `_inner_loop` itself checks one line below
        # the call.
        #
        # The old-news test is asked only of `rules_lint`, the stage it is
        # about: it consults the baseline's `rules_lint` history, so asking it
        # about a `gofmt` warning would excuse formatting against a
        # contract-lint record.
        unclean = [
            r
            for r in report.warnings
            if not (r.name == "rules_lint" and self._lint_is_old_news(r))
        ]

        promoted = []
        for step in self.state.plan:
            if step.status != "written":
                promoted.append(step)
                continue
            # **Per file, not per report.** A stage holds a step open only when
            # one of its findings names a path that step covers.
            #
            # The alternative -- any warning anywhere holds every written step
            # -- was tried and is worse than not having this node at all. The
            # inner loop runs `go_diagnostics`, which fails outright on a
            # machine with no gopls, so on most developer machines every step
            # would sit at `written` forever. That is defect D2's compounding
            # failure rebuilt in a new place: a permanently unsatisfiable
            # condition that spends the completion guard's single push-back and
            # then silences it.
            #
            # `finding_path` returns "" for a stage that failed without keying
            # a file, which is the honest reading: a tool that could not run
            # objected to nothing about this step's work.
            blockers = [
                r.name
                for r in unclean
                if any(step.covers(finding_path(r.name, k)) for k in r.findings)
            ]
            blocker = ", ".join(dict.fromkeys(blockers))
            promoted.append(
                replace(
                    step,
                    status="written" if blocker else "done",
                    note=f"written; {blocker} is not clean on it yet" if blocker else "",
                )
            )
        self.state.plan = tuple(promoted)
        self._save_plan()

    def _mark_steps(self, path: str, status: str, note: str = "") -> None:
        """Set the status of the plan step(s) ``path`` belongs to.

        One step when several name the same file, and that is what makes a large
        file convertible at all.

        A 6,571-line handler cannot be converted in one reply -- the acting
        phase has 16,384 output tokens and has to read the original as well --
        so its step is really nine steps over the same path, each naming the
        methods it converts. Marking *every* covering step on the first write
        made those nine a single step wearing nine hats: one `patch_file` and
        the whole file was written, done, and never looked at again. A field run
        stopped on exactly this, reporting that the volume "exceeds what can be
        reliably done in a single session" -- which was true, and which the plan
        had no way to express.

        So a write lands on the step being worked: the cursor's, when the cursor
        covers the path, and otherwise the first covering step still open. Only
        when nothing is open does it fall back to marking them all, which is the
        old behaviour and the right one for a re-touch after the work is done.
        """
        if not self.state.plan:
            return
        before = self.state.plan
        covering = [i for i, step in enumerate(self.state.plan) if step.covers(path)]
        target: set[int] = set(covering)
        if len(covering) > 1:
            active = self.active_step
            if active is not None and active[0] - 1 in covering:
                target = {active[0] - 1}
            elif open_here := [i for i in covering if self.state.plan[i].open]:
                target = {open_here[0]}
        self.state.plan = tuple(
            replace(step, status=status, note=note) if i in target else step
            for i, step in enumerate(self.state.plan)
        )
        if self.state.plan != before:
            # A status change is not a new plan, so it updates the record
            # without adding a revision -- the loop sets `done` from the change
            # set on every mutation, and recording each of those would bury the
            # two or three that are actually revisions.
            self._save_plan()

    def _record_plan(self, cause: str, reason: str = "") -> None:
        """Write the plan and the revision that produced it.

        Silent when there is no session: a loop driven directly by a test or a
        CLI has no directory to write to, and inventing one would put files in
        whatever the working directory happened to be.
        """
        if not self.session_id:
            return
        self._plan_record = self._plan_record.record(
            self.state.plan, self.state.plan_summary, cause=cause, reason=reason
        )
        self._plan_record.session_id = self.session_id
        self._plan_record.forced = self.state.plan_forced
        self._plan_record.migration = self.state.migration
        self._plan_record.save(self.router.workspace.root)
        self._save_progress()

    def _line_count(self, path: str) -> int:
        """How many lines are in a workspace file, or 0.

        Read rather than estimated, and cheap enough to do per plan step: eight
        steps at most, once per submission. A path that does not resolve, is a
        directory, is a glob, or is not text answers 0 -- "unknown size" and
        "small" are the same answer to the only question asked of it, which is
        whether one step can finish it.
        """
        try:
            target = self.router.workspace.resolve(path)
        except (PathEscape, ValueError):
            return 0
        try:
            with open(target, "rb") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return 0

    def _save_routes(self) -> None:
        """Record every route the service serves, before the conversion touches it.

        Taken once, on the way past the branch guard, which is the last moment
        at which the answer is still *the legacy service's*. A phase later it
        would be a picture of a half-converted one, and comparing the end of the
        migration against that would pass by construction -- the routes it had
        already lost would not be in the baseline to be missed.

        Silent about its own failure, like the journal and the plan record, with
        one difference that matters: the gate's `routes_check` stage is
        conditioned on the file *existing*, so a save that did not happen skips
        the check rather than failing it. That is the right way round -- the
        alternative is a migration that cannot finish because a check could not
        be set up -- and it is why the checkpoint says whether the inventory was
        taken.
        """
        if self.state.routes_saved or not self.state.migration.active:
            return
        # Marked before the call, not after: a sidecar that is absent raises on
        # every attempt, and retrying it on each of forty write calls would cost
        # a subprocess launch apiece to reach the same answer.
        self.state.routes_saved = True
        try:
            # Through the gate path, because that is what this is: a harness
            # call the model did not make and cannot refuse. `route_inventory`
            # is gate-only, so an ordinary dispatch would correctly refuse it.
            outcome = self.router.run_gate_tool("route_inventory", {"save": ROUTES_BEFORE})
        except Exception:  # noqa: BLE001 - a backup is not a precondition for planning
            return
        if not isinstance(outcome, ToolResult) or not outcome.ok:
            return
        self.state.routes_before = int(outcome.meta.get("routes") or 0)
        self._relay(
            Event(
                EventType.GATE,
                {
                    "kind": "routes",
                    "saved": ROUTES_BEFORE,
                    "routes": self.state.routes_before,
                    "unresolved": int(outcome.meta.get("unresolved") or 0),
                },
            )
        )

    def _save_progress(self) -> None:
        """Write the migration's progress where the next session can read it.

        Best-effort, like the journal and the plan record: a lost progress file
        costs a `read_file` on the next session, never the work.

        It is written on every plan change rather than at the end, because the
        end is the case it is least needed for. A conversion spans sessions --
        ten thousand lines across seven files is more than one context window --
        and the question a resumed session opens with is *where did the last one
        get to*. Before this the answers were the transcript, which compaction
        eats, and the model's own prose, which a field session wrote by hand and
        then read back as evidence of work it had not done.
        """
        if not self.state.migration.active:
            return
        try:
            target = self.router.workspace.root / PROGRESS_PATH
            ensure_private(target.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                progress_document(
                    self.state.migration,
                    self.state.plan,
                    self.router.touched,
                    self.state.routes_before,
                ),
                encoding="utf-8",
            )
        except OSError:
            return

    def _save_plan(self) -> None:
        """Write the current step statuses, without recording a revision."""
        if not self.session_id:
            return
        self._plan_record = self._plan_record.with_steps(self.state.plan)
        self._plan_record.session_id = self.session_id
        self._plan_record.forced = self.state.plan_forced
        self._plan_record.migration = self.state.migration
        self._plan_record.save(self.router.workspace.root)
        self._save_progress()

    def _note_tried(self, report: GateReport) -> None:
        """Record a gate failure after an edit as an attempt that did not work."""
        blocker = report.blocked_by
        if blocker is None:
            return
        first = next((ln.strip() for ln in blocker.content.splitlines() if ln.strip()), "")
        self.state.tried.append(
            f"turn {self.context.turn}: gate failed at {blocker.name}"
            + (f": {first[:120]}" if first else "")
        )
        del self.state.tried[:-STATE_ITEMS * 2]
        for step in self.state.plan:
            if step.file and step.file in blocker.content and step.status != "skipped":
                self._mark_steps(step.file, "failed", f"{blocker.name}, turn {self.context.turn}")

    def _can_replan(self) -> bool:
        return (
            self.state.mode is Mode.AGENT
            and bool(self.state.plan)
            and self.state.replans < MAX_REPLANS
        )

    def _replan(self, reason: str) -> Iterator[Event]:
        """Send the run back to the Planner with what has been tried.

        The one strategy change the loop makes on its own. Every other exit is a
        stop. It arrives with the record -- the failures, the stages they blocked
        at, the revisions already made -- because a replan against the same
        empty state that produced the first plan is a re-roll, not a replan.
        """
        self.state.replans += 1
        self.state.stalled_turns = 0
        self.state.research_turns = 0
        tried = "\n".join(f"- {t}" for t in self.state.tried[-STATE_ITEMS:]) or "- (nothing recorded)"
        self.context.append_user(
            f"That approach has not worked: {reason}.\n\n"
            f"# What has been tried\n{tried}\n\n"
            "Plan again from what is now known. Steps already done stay done; "
            "replace the rest with a different approach, not the same one restated. "
            "Submit it with `submit_plan`, or `ask_developer` if the decision is "
            "theirs to make."
        )
        yield Event(
            EventType.GATE,
            {"kind": "replan", "reason": reason, "tried": list(self.state.tried)},
        )
        self._switch(Mode.PLANNER)

    def _adopt_plan(self, steps: Sequence[PlanStep], summary: str) -> Iterator[Event]:
        """Install a plan, carrying forward everything already settled.

        **Re-planning must never un-do work that is on disk.** It did. The rule
        was "keep a done step only if the incoming plan does not name its file",
        which is exactly backwards: re-submitting the *same* plan -- the common
        case, because a follow-up re-enters the Planner and the Planner writes
        the plan it already wrote -- dropped every finished step and replaced it
        with a pending one. A field session re-submitted an identical eight-step
        plan three times, and each time the two files genuinely written stopped
        counting as done. The phase pointer was erased on every re-plan.

        So a step that is settled -- written, verified, or deliberately skipped
        -- keeps its status, and the incoming step supplies only the wording.
        ``pending`` and ``failed`` do not carry: a step the model has just
        re-stated is one it intends to attempt again, and a stale ``failed``
        would read as a verdict on an attempt that has not happened yet.

        Settled steps the new plan does not mention are kept at the front. They
        are history, and dropping them would make the same claim the old rule
        made: that nothing happened.
        """
        settled = {"done", "written", "skipped"}
        incoming = {s.file for s in steps if s.file}
        prior = {s.file: s for s in self.state.plan if s.file and s.status in settled}
        merged = tuple(
            replace(step, status=prior[step.file].status, note=prior[step.file].note)
            if step.file in prior
            else step
            for step in steps
        )
        kept = tuple(
            s for s in self.state.plan if s.status in settled and s.file not in incoming
        )
        self.state.plan = kept + merged
        if summary:
            self.state.plan_summary = summary
        # Recorded before it is rendered. A plan was a tuple in a process,
        # lost on a daemon restart, with no record of how it reached its
        # current shape -- so a developer who reloads their window at step 4 of
        # 7 came back to an agent holding the edits and none of the plan that
        # produced them. See ``plan.py``.
        self._record_plan("replanned" if self.state.replans else "submitted")
        rendered = "\n".join(step.rendered(i) for i, step in enumerate(self.state.plan, 1))
        if self.state.plan_summary:
            rendered = f"{self.state.plan_summary}\n\n{rendered}"
        self.context.set_plan(rendered)
        self._note_unrunnable_criteria()
        yield Event(
            EventType.PLAN,
            {
                "text": rendered,
                "steps": len(self.state.plan),
                # The typed steps, not just a count and some prose.
                #
                # `submit_plan` has always had these; the event carried
                # `{text, steps: N}` and left the panel to recover them with a
                # regex over the rendered text. That regex matched path-shaped
                # tokens, so a plan step whose *description* mentioned
                # `handler/response.go` listed it under "Files in scope" and a
                # step whose actual file was `MIGRATION_PLAN.md` did not appear
                # at all -- the pattern only knew about Go, SQL and YAML. The
                # panel showed two files the run would never touch and hid the
                # one it would. `_unfinished` fixed exactly this bug on the
                # server side; the UI kept the old version (BUG EXT-19).
                "items": [
                    {
                        "index": i,
                        "file": step.file,
                        "action": step.action,
                        "accepts": step.accepts,
                        "status": step.status,
                        "note": step.note,
                    }
                    for i, step in enumerate(self.state.plan, 1)
                ],
            },
        )

    def _revised(self, outcome: ToolResult) -> Iterator[Event]:
        """Act on a `revise_plan` the router accepted."""
        reason = str(outcome.meta.get("reason") or "").strip()
        self.state.revisions += 1
        if self.state.revisions > MAX_REVISIONS:
            self.context.append_user(
                f"The plan has already been revised {MAX_REVISIONS} times this run and "
                "this revision was not adopted. Carry out the plan as it stands, or "
                "call `finish` and say in `blocked` what cannot be done."
            )
            return
        self.state.tried.append(f"turn {self.context.turn}: plan revised: {reason}")
        del self.state.tried[:-STATE_ITEMS * 2]
        steps = self._normalise_plan(steps_from_meta(dict(outcome.meta)))
        # `revise_plan` is the model's own pivot, so whatever produced the plan
        # it replaces, this one is volunteered.
        self.state.plan_forced = False
        yield from self._adopt_plan(steps, "")
        # Overwrites the revision `_adopt_plan` just recorded with one that
        # names the cause and the model's reason. Two entries for one event
        # would make the history harder to read than no history.
        self._plan_record.revisions = self._plan_record.revisions[:-1]
        self._record_plan("revised", reason)

    def _overlap(self, call: ToolCall, outcome: ToolResult) -> str:
        """What to tell a run whose search returned only places it already had."""
        if call.name == "search_docs":
            return self._retrieval_overlap(call, outcome)
        if call.name == "search_repo":
            return self._search_overlap(call, outcome)
        return ""

    def _search_overlap(self, call: ToolCall, outcome: ToolResult) -> str:
        """``_retrieval_overlap`` for the workspace search.

        The same algorithm, on ``path:line`` keys instead of section citations.
        It was applied to one of twenty-three tools, and the one that loops in
        the field is this one: ``Handler``, ``handler`` and ``Handler\\(`` are
        three fingerprints and one set of places.
        """
        if not outcome.ok:
            return ""
        keys = outcome.meta.get("match_keys")
        if not keys:
            return ""
        hits = frozenset(str(k) for k in keys)
        try:
            pattern = str((call.parsed() or {}).get("pattern", ""))
        except ValueError:
            pattern = ""

        seen: set[str] = set()
        source = ""
        for earlier_pattern, earlier in self.state.search_hits:
            if len(hits & earlier) / len(hits) >= RETRIEVAL_OVERLAP and not source:
                source = earlier_pattern
            seen |= earlier
        self.state.search_hits.append((pattern, hits))
        del self.state.search_hits[:-40]

        if hits - seen:
            self.state.search_repeats = 0
            return ""
        self.state.search_repeats += 1
        return (
            f"That search found only places {source or 'an earlier search'!r} already "
            "returned -- the same lines under different words. Rewording the pattern "
            "will not reach different code. Read one of those places, search for "
            "something else, or work from what is already above."
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

        # A file the plan sets out to change is one the acting mode has to
        # quote back **exactly**: `patch_file` takes an `old` that must match
        # the bytes on disk, and "you have already seen those lines" is not the
        # same thing as having them accurate enough to anchor an edit -- twenty
        # turns and a phase switch back, under a summariser, past a mode
        # instruction. The refusal landed at precisely the moment the model
        # said what it needed it for ("let me check the end of the file to find
        # the right anchor") and gave it nothing, so it never wrote the file
        # and reached for `finish` instead (BUG L-27).
        #
        # Coverage only. The call-count backstop below still applies, so this
        # buys the acting mode at least MIN_READS looks at a file it is
        # supposed to edit, not unbounded turns.
        anchoring = self.state.mode is Mode.AGENT and self._is_plan_target(path)

        start, end = _as_line(parsed.get("start")), _as_line(parsed.get("end"))
        if start is None and end is None:
            # A whole-file read. Only redundant once the whole file has been
            # delivered, which `covers` can answer exactly when the length is
            # known and cannot when it is not.
            if not anchoring and ledger.lines and ledger.covers(1, ledger.lines):
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
        if not anchoring and ledger.covers(low, high):
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

    def _compact(
        self,
        *,
        retain_pct: float = 0.35,
        reason: str = "threshold",
        strategy: str = "agentic",
    ) -> Iterator[Event]:
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
        recap = self.context.compact(
            self._summarise, retain_pct=retain_pct, strategy=strategy
        )
        evicted = self.context.last_eviction
        self._forget_evicted(evicted)
        yield Event(
            EventType.GATE,
            {
                "kind": "compaction",
                "reason": reason,
                "strategy": strategy,
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
        """Re-sync the one ledger that is still a copy, and say what was hidden.

        This used to be a page of invalidation: rebuild the read spans, clear
        five call ledgers outright, reset two counters. It existed because the
        context evicted, the loop's ledgers refused, and nothing connected them
        — the recap told the model "re-read one only if you need a line range
        you have not seen" while the read intercept answered that same re-read
        with "those lines are already in context above", two true-sounding
        messages neither of which could be obeyed (BUG L-10).

        Almost all of it is gone because the disagreement is gone. Compaction no
        longer removes anything; it writes a sidecar, the projection applies it,
        and every "can the model see this" question is answered from the
        projection on the turn it is asked:

        * the read intercept reads ``context.coverage()`` (see ``_live_reads``);
        * the cached-result intercept checks ``context.visible_results``;
        * "has this body been seen" is ``context.visible_bodies``;
        * repeated answers collapse in the projection, so there is no echo
          ledger left to clear.

        What remains is the *stored* read ledger, which is not a claim about the
        context — it is the per-file dispatch budget and the file's length, both
        facts about the run. Its spans are re-derived here anyway so that a
        ledger read directly (by ``carry_from``, or by a future persistence
        path) does not carry coverage the model has lost.

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
            ledger.covered = []
            for low, high in coverage.get(path, []):
                ledger.add(low, high)

        if not evicted.messages:
            return
        # A search's places are worth re-seeing once after the messages naming
        # them stop being visible. Unlike the ledgers above, this one is a
        # record of what a *query* returned rather than of what is in context,
        # so the projection cannot re-derive it.
        self.state.reading.forget_searches()

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
        read = self._read_paths(messages)
        modified = tuple(self.router.touched)
        goal = self.state.plan_summary or (
            self.state.plan[0].action if self.state.plan else ""
        )

        # Bounded per message, then split at message boundaries into pieces one
        # call can take, then summarised oldest-first with each recap folded
        # into the next. The evicted set used to go to the summariser whole,
        # and on a large eviction the call could not fit its own window.
        pieces = _chunked([(m, _rendered(m)) for m in messages], _TRANSCRIPT_CHARS)
        recap: Recap | None = None
        if len(pieces) > _MAX_RECAP_CALLS:
            recap = _digest(pieces[: -_MAX_RECAP_CALLS])
            pieces = pieces[-_MAX_RECAP_CALLS:]

        for piece in pieces:
            transcript = "\n\n".join(text for _, text in piece)
            fallback = Recap(
                goal=goal,
                files_modified=modified,
                open_items=(
                    "part of the recap could not be summarised; its tail is preserved "
                    "below",
                ),
                decisions=(transcript[-2000:],) if transcript else (),
                turns=turns,
            )
            summarised = self._recap_call(transcript) or fallback
            recap = summarised.merge(recap)

        if recap is None:
            recap = Recap(turns=turns)
        return replace(
            recap,
            goal=recap.goal or goal,
            files_modified=recap.files_modified or modified,
            # Not taken from the model. What was evicted is a fact about this
            # compaction, and the loop is the only thing that knows it.
            files_read=read,
            turns=turns,
        )

    def _recap_call(self, transcript: str) -> Recap | None:
        """One summariser call over one piece of the transcript, or ``None``.

        ``None`` on any failure, and every failure is announced. The broad
        ``except`` used to return the fallback in silence for anything that was
        not a programming error, so a summariser that could not fit its window
        — the exact case the chunking above exists for — degraded every
        compaction of a long run without one line saying so.
        """
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
            self._summariser_failed(f"the recap could not be requested: {exc}")
            return None
        except Exception as exc:  # noqa: BLE001 - a degraded recap beats ending the run
            self._summariser_failed(f"the summariser call failed: {exc}")
            return None

        parsed = _parse_json_object(reply.content or "")
        if parsed is None:
            self._summariser_failed("the summariser replied with something that is not a recap")
            return None
        return Recap(
            goal=str(parsed.get("goal", "") or ""),
            plan_step=str(parsed.get("plan_step", "") or ""),
            files_created=tuple(parsed.get("files_created") or ()),
            files_modified=tuple(parsed.get("files_modified") or ()),
            decisions=tuple(parsed.get("decisions") or ()),
            findings=tuple(parsed.get("findings") or ()),
            verified=tuple(parsed.get("verified") or ()),
            open_items=tuple(parsed.get("open_items") or ()),
            do_not_retry=tuple(parsed.get("do_not_retry") or ()),
        )

    def _summariser_failed(self, message: str) -> None:
        """Say that a compaction is degraded, in the transcript, every time."""
        self._relay(
            Event(
                EventType.ERROR,
                {
                    "where": "summariser",
                    "message": message,
                    "effect": "compaction degraded to a fallback recap for part of the "
                    "transcript; dead ends in that part will not be carried across it",
                },
            )
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

#: How much of a tool result the summariser is shown: this much of its head and
#: ``_RESULT_TAIL_IN_TRANSCRIPT`` of its tail, with the cut stated between them.
#:
#: The recap is about *what happened* — which file was read, what the build
#: said, which search came back empty — and the head of a result carries that:
#: `read_file`'s header line, `go_build`'s first errors, `search_repo`'s match
#: count. The body is the thing the compaction is throwing away; handing all of
#: it to the summariser is paying to re-read what is being forgotten. This is
#: the same move Claude Code's "microcompact" makes before any model call —
#: stale tool results are cleared deterministically, and only the residue is
#: summarised.
_RESULT_HEAD_IN_TRANSCRIPT = 1_200
_RESULT_TAIL_IN_TRANSCRIPT = 400

#: How much of a prose message (the model's, or the developer's) is kept.
_PROSE_IN_TRANSCRIPT = 1_500

#: The most transcript one summariser call is handed, in characters.
#:
#: The evicted set used to go to the summariser whole. Three capped reads are
#: ~577,000 characters; on the emergency 15% path the evicted set can be most of
#: an over-budget prompt, which does not fit the summariser's own window, and
#: the call failed into the fallback recap without a word. Forty thousand
#: characters is ~12k tokens on the code ratio: comfortably inside any model the
#: `summariser` role could be pointed at, and large enough that a compaction of
#: an ordinary working set is still one call.
_TRANSCRIPT_CHARS = 40_000

#: How many summariser calls one compaction may make. A transcript that does
#: not fit one call is split at message boundaries and summarised oldest-first,
#: each recap folded into the next (``Recap.merge``) — Aider's recursive
#: ``ChatSummary`` and langmem's running summary, applied to a structured recap.
#: Beyond this many pieces the oldest are digested deterministically instead:
#: a compaction is already a real cost against the developer's quota, and one
#: that spent twenty model calls summarising would be its own budget problem.
_MAX_RECAP_CALLS = 4


def _clipped(message: Message) -> str:
    """The message's content as the summariser sees it: bounded per message.

    A tool result keeps its head and its tail. The head is where the tools here
    put the facts (the path and span, the first compiler errors, the match
    count); the tail is where a truncated build log ends and where a search
    result says it stopped. Prose keeps its head: a reply that needed more than
    this to say what it decided had not decided.
    """
    text = message.content or ""
    if message.role is Role.TOOL:
        limit = _RESULT_HEAD_IN_TRANSCRIPT + _RESULT_TAIL_IN_TRANSCRIPT
        if len(text) <= limit:
            return text
        cut = len(text) - limit
        return (
            f"{text[:_RESULT_HEAD_IN_TRANSCRIPT]}\n[… {cut:,} chars of this result "
            f"omitted from the handover …]\n{text[-_RESULT_TAIL_IN_TRANSCRIPT:]}"
        )
    if len(text) <= _PROSE_IN_TRANSCRIPT:
        return text
    return f"{text[:_PROSE_IN_TRANSCRIPT]}… ({len(text) - _PROSE_IN_TRANSCRIPT:,} more chars)"


def _rendered(message: Message) -> str:
    """One message, as the summariser sees it.

    It used to see ``content`` alone. An assistant turn that was purely tool
    calls has an empty ``content``, so the transcript handed to the summariser
    rendered every edit the run made as a blank line — and the histories that
    summarised worst were exactly the write-heavy ones the recap matters most for
    (BUG L-27). The recap then said nothing about what had been done, and the
    post-compaction run had no way to know it had already written the file.

    Arguments are truncated rather than omitted: which file was written is the
    fact worth carrying, and the content of the write is in the workspace. The
    content is bounded too, by ``_clipped``, so that no single message can make
    the transcript larger than one summariser call takes.
    """
    where = "" if not message.path else " " + message.path
    head = f"[{message.role}{where}]"
    parts = [f"{head} {_clipped(message)}".rstrip()]
    for call in message.tool_calls:
        args = (call.arguments or "").strip()
        if len(args) > _ARGS_IN_TRANSCRIPT:
            args = f"{args[:_ARGS_IN_TRANSCRIPT]}… ({len(call.arguments):,} chars)"
        parts.append(f"{head} called {call.name}({args})")
    return "\n".join(p for p in parts if p.strip() != head)


def _chunked(
    rendered: Sequence[tuple[Message, str]], budget: int
) -> list[list[tuple[Message, str]]]:
    """Split a rendered transcript at message boundaries into pieces under ``budget``.

    A single rendered message is already bounded by ``_clipped`` and
    ``_ARGS_IN_TRANSCRIPT``, so a piece holds at least one message and a message
    is never split — the summariser is never shown half a tool result with no
    idea which half.
    """
    pieces: list[list[tuple[Message, str]]] = []
    current: list[tuple[Message, str]] = []
    size = 0
    for item in rendered:
        cost = len(item[1]) + 2
        if current and size + cost > budget:
            pieces.append(current)
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        pieces.append(current)
    return pieces


def _digest(pieces: Sequence[Sequence[tuple[Message, str]]]) -> Recap:
    """A recap of what the run *did* in pieces the summariser will not be shown.

    Deterministic, and about actions rather than conclusions: which tools were
    called on which files. It carries no ``do_not_retry`` — that is a judgement,
    and this makes none — but it keeps the compaction honest about the shape of
    what it dropped, which is more than the previous silent fallback did.
    """
    seen: dict[str, None] = {}
    for piece in pieces:
        for message, _ in piece:
            for call in message.tool_calls:
                args = _safe_args(call)
                target = None
                if isinstance(args, dict):
                    target = args.get("path") or args.get("pattern") or args.get("query")
                seen[f"{call.name}({target})" if target else call.name] = None
    if not seen:
        return Recap()
    listed = ", ".join(list(seen)[:MAX_RECAP_ITEMS])
    more = len(seen) - MAX_RECAP_ITEMS
    return Recap(
        decisions=(
            "earlier in this run (not summarised by the model; the transcript was "
            f"too long): calls made were {listed}"
            + (f" and {more} more" if more > 0 else ""),
        ),
    )


_PARTIAL_PATH = re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _partial_path(arguments: str) -> str:
    """The ``path`` in a cut-off call's arguments, when the prefix reached it."""
    match = _PARTIAL_PATH.search(arguments or "")
    return match.group(1) if match else ""


#: What an endpoint says when the request will not fit.
#:
#: Matched on the message rather than on a status code, because the status is
#: 400 for a dozen unrelated things and this is the one that is recoverable.
#: Several phrasings because the request passes through LiteLLM in front of
#: vLLM and either may be the one to refuse it; all of them are lower-cased
#: before matching.
_CONTEXT_LENGTH_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "prompt is too long",
    "input is too long",
    "exceeds the maximum",
)


def _context_length_error(exc: BaseException) -> bool:
    """Whether this failure is the endpoint refusing an over-long request.

    Conservative on purpose. A false positive costs one deterministic
    compaction and a retry; a false negative costs the run, which is what
    happens today for every one of these.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _CONTEXT_LENGTH_MARKERS)


def _describe(group: Any) -> dict[str, Any]:
    """One state group, rendered small enough to read every turn.

    A ledger is summarised by its size and its keys, not dumped: `last_results`
    holds up to 6,000 characters per fingerprint and reading forty of them is
    not debugging, it is scrolling. What a reader needs from a ledger is whether
    it has an entry for the call that just misbehaved, which the keys answer.

    Anything scalar is shown as it is, because the scalars are the bounds -- the
    stall count, the gate failures, the truncation streak -- and those are the
    numbers a run dies on.
    """
    out: dict[str, Any] = {}
    for field_name, value in vars(group).items():
        if isinstance(value, dict):
            out[field_name] = {"n": len(value), "keys": sorted(value)[:8]}
        elif isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            out[field_name] = (
                {"n": len(items), "items": [str(i)[:80] for i in items[:6]]}
                if len(items) > 6
                else [str(i)[:80] for i in items]
            )
        elif value is None or isinstance(value, (str, int, float, bool)):
            out[field_name] = value if not isinstance(value, str) else value[:200]
        else:
            out[field_name] = str(value)[:200]
    return out


def _body_digest(name: str, body: str) -> str:
    """A key for "the model has seen this result before".

    Whitespace-normalised so a reflowed build log is the same log, and keyed by
    tool so two tools that happen to say "clean" are not the same finding.
    """
    normalised = re.sub(r"\s+", " ", body).strip()
    return hashlib.sha1(f"{name}\n{normalised}".encode("utf-8", "replace")).hexdigest()


def _empty_finding(outcome: ToolResult) -> bool:
    """A successful search that found nothing. A finding, but not progress twice."""
    return outcome.ok and "hits" in outcome.meta and not outcome.meta.get("hits")


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


#: How long a fingerprint's argument half may be before it is stored as a
#: digest of itself.
#:
#: A fingerprint is a dictionary *key*, held in four ledgers and carried across
#: every follow-up in the session by ``carry_from``. Most are a path and a line
#: range. Two are not: `write_file` carries the whole file it wrote, and
#: `finish` carries the whole answer -- and `MAX_ANSWER_CHARS` is 24,000. One
#: field session ended up holding two 24,000-character keys for the rest of its
#: life, and every debug record of its state printed both in full.
#:
#: 400 characters clears every real call -- the longest ordinary one measured is
#: a `patch_file` anchor at ~200 -- and nothing above it is a question anybody
#: asks twice by accident.
FINGERPRINT_CHARS = 400


def _fingerprint(call: ToolCall) -> str:
    """What makes two calls the same question.

    Falls back to the raw string when the arguments do not parse -- a model that
    resends the same malformed arguments is exactly what the ledgers are for, so
    this must not be the thing that raises on them.

    Long arguments are kept as a digest rather than in full. Identity is all a
    fingerprint is for: nothing reads the arguments back out of one, and the one
    thing that does read *into* it takes the tool name from in front of the
    first colon, which a digest leaves alone.
    """
    try:
        parsed = call.parsed()
    except ValueError:
        return _key(call.name, call.arguments)
    if not isinstance(parsed, dict):
        return _key(call.name, call.arguments)
    kept = {k: v for k, v in parsed.items() if k not in _VOLUME_PARAMS}
    return _key(call.name, json.dumps(kept, sort_keys=True, separators=(",", ":")))


def _key(name: str, arguments: str) -> str:
    """``tool:arguments``, or ``tool:#digest`` once the arguments are long."""
    if len(arguments) <= FINGERPRINT_CHARS:
        return f"{name}:{arguments}"
    digest = hashlib.sha256(arguments.encode("utf-8", "replace")).hexdigest()[:32]
    return f"{name}:#{digest}"


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
