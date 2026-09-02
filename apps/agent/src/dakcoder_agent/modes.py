"""The three modes, their budgets, and reasoning control.

Three, where there were five. The five -- Planner, Scaffolder, Coder, Verifier,
Debugger -- were a fixed pipeline every request walked, and section 4 of the
failure report is mostly an account of what that cost: a Verifier with no write
tool announcing "my job is to make the edit", a Coder saying "I am in verifier
mode", fourteen mode switches in fifteen turns, and six overlay messages stacked
in the un-evictable head each contradicting the one above it. No mature agent
ships a per-request planner-coder-verifier pipeline; they run one loop and let
the model decide what this turn needs.

What is left is the distinction that actually earns a mode, which is **what the
model is allowed to do**:

``ASK``      read-only. Answers a question and stops. Cannot write, cannot build.
``PLANNER``  read-only, plus the two tools that end the phase: ``submit_plan``
             and ``ask_developer``. A plan is a typed event, not prose.
``AGENT``    everything. Edits, scaffolds, builds, vets, tests, debugs. The gate
             runs after it, deterministically, and its report comes back as an
             ordinary message.

That is a tool allow-list, which is how Cline's Plan/Act and Cursor's Ask/Agent
are built, rather than a persona a prompt asks the model to adopt.

The reasoning setting below is unchanged and still consequential. Qwen3.8-27B is
a reasoning model: left at its default it returns ``reasoning_content`` with
``content: null``, and a ``max_tokens`` too small to finish reasoning burns the
whole turn for nothing.

The pre-implementation spike measured it (Part A section 4.4). Identical prompt,
identical temperature, only ``max_tokens`` varied:

    thinking off, 1,000 tokens   ->   2.0s, a 517-character answer
    thinking on,  1,000 tokens   ->   4.5s, 1,247 chars of reasoning
    thinking on,  4,000 tokens   ->  31.4s, 9,948 chars of reasoning
    thinking on, 16,000 tokens   ->  15.4s, 4,828 chars of reasoning

Reasoning expands to fill the available budget, non-deterministically, and the
answer did not improve. So thinking is off by default, in every mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Intent", "Mode", "ModeConfig", "MODES", "config_for"]


class Mode(StrEnum):
    """What the model may do this turn.

    One system prompt, one overlay. Modes narrow the tool schema and sharpen the
    instruction; they do not fork the process and they do not each get their own
    system prompt -- that is finding S6, and it cost the frontend agent three
    cold prefills per task.
    """

    ASK = "ask"
    PLANNER = "planner"
    AGENT = "agent"

    @classmethod
    def coerce(cls, value: "Mode | str") -> "Mode":
        """Accept the retired names as well as the current ones.

        A session's stored mode, a client that has not been rebuilt, and a
        ``dakcoder.defaultMode`` a developer set months ago all carry one of the
        five old names. Every one of them maps cleanly onto what the mode
        *permitted*, which is the only thing the name ever meant: the three that
        could write become ``AGENT``, the one that could not becomes ``ASK``.
        """
        if isinstance(value, Mode):
            return value
        key = str(value).strip().lower()
        try:
            return cls(key)
        except ValueError:
            pass
        return _RETIRED.get(key, cls.ASK)


#: The five that were, and what they were allowed to do.
#:
#: ``verifier`` maps to ASK rather than AGENT deliberately: it held no write
#: tool, and a session resuming into it should not silently acquire one.
_RETIRED: dict[str, Mode] = {
    "coder": Mode.AGENT,
    "scaffolder": Mode.AGENT,
    "debugger": Mode.AGENT,
    "verifier": Mode.ASK,
    "multi": Mode.ASK,
    "auto": Mode.ASK,
}


class Intent(StrEnum):
    """What the developer asked for, decided **before** the first turn.

    The old loop had no such thing. Every message started in the Planner, the
    model answered, and only then did about 500 lines of regex over the reply
    and the task try to work out what had been wanted -- badly: 17 of 24
    realistic read-only prompts were classified as work, and each of those ran
    the full gate on an untouched workspace and entered the escalation ladder.
    "List the routes in this service" cost about eight turns and two minutes and
    ended "Stopped -- no progress".

    Intent belongs at the front, where it can be *asked* rather than guessed:
    the panel's Ask/Agent toggle answers it directly, and ``AUTO`` answers it
    with one cheap structured-output call before any tool is offered.
    """

    #: Classify once, up front, with a schema-constrained call.
    AUTO = "auto"
    #: Read-only, whatever the model decides it would like to do.
    ASK = "ask"
    #: Plan, then execute.
    AGENT = "agent"

    @classmethod
    def coerce(cls, value: "Intent | str | None") -> "Intent":
        if isinstance(value, Intent):
            return value
        key = str(value or "").strip().lower()
        try:
            return cls(key)
        except ValueError:
            pass
        # A legacy mode name on the wire is still a statement of intent.
        legacy = {
            "planner": cls.AGENT,
            "coder": cls.AGENT,
            "scaffolder": cls.AGENT,
            "debugger": cls.AGENT,
            "verifier": cls.ASK,
            "multi": cls.AUTO,
            "": cls.AUTO,
        }
        return legacy.get(key, cls.AUTO)


@dataclass(frozen=True)
class ModeConfig:
    """What a mode changes about a request."""

    mode: Mode
    #: Hard cap on assembled prompt tokens (Part A section 6.1).
    prompt_budget: int
    #: Cap on completion tokens. Budgeted separately from the prompt, because
    #: conflating the two is how a mode ends up with room to think and no room
    #: to answer.
    max_tokens: int
    #: Whether the chat template enables reasoning.
    enable_thinking: bool
    #: Sampling temperature. Go boilerplate rewards determinism, so this sits
    #: below the frontend agent's 0.2.
    temperature: float
    #: The model role this mode's turns are dispatched as.
    #:
    #: Separate from the mode because they answer different questions: the mode
    #: decides what the model may *do* this turn, the role decides *which model*
    #: does it. They happen to line up one-to-one today, and keeping them apart
    #: is what lets an operator put the Planner on a bigger model without any of
    #: the mode's budgets or tool rules moving with it.
    #:
    #: Every turn used to dispatch as ``coder`` whatever mode it was in, so the
    #: gateway's per-role table had entries nothing could ever reach — planning
    #: and answering were billed, logged and routed as coding.
    role: str = "coder"

    def __post_init__(self) -> None:
        # Rule 2 of section 4.4: any thinking-on call gets at least 6,144 output
        # tokens. The budget has to hold a runaway reasoning block *plus* the
        # answer, and the spike showed reasoning alone exceeding 2,500 tokens on
        # a trivial prompt. A thinking-on mode with a tight budget does not
        # produce a worse answer -- it produces no answer at all.
        if self.enable_thinking and self.max_tokens < 6144:
            raise ValueError(
                f"{self.mode} enables thinking with max_tokens={self.max_tokens}; "
                "reasoning expands to fill the budget and a truncated block returns "
                "content: null, wasting the turn. Use at least 6144."
            )


#: The prompt budget every mode gets. One number, because five copies of it is
#: how the Planner came to have a different one that nobody noticed.
#:
#: 245,760 -- the model's own window, minus room to answer. The endpoint serves
#: Qwen3.8-27B at ``max_model_len`` 262,144 (asserted by the gateway probe),
#: prompt and completion share it, and the largest completion budget below is
#: 6,144; the remaining ~10k is headroom for template overhead and estimator
#: error, so a full prompt cannot push the answer out of the window.
#:
#: The history is worth keeping, because both of the old numbers were
#: load-bearing. It was 24,000 for the Planner once, and that killed a
#: seventy-one-turn run: compaction retains to ``budget * 0.35``, which at
#: 24,000 could not hold one capped file read, so the mode re-read the same
#: files forever. 32,768 fixed that case and was chosen against section 5.3's
#: cost targets; what changed the decision is the field evidence that at 32,768
#: real runs compacted at ~23k, the recap evicted the very answers the model was
#: working from, and the repeat detector ended the run.
#:
#: The budget is a ceiling, not an allocation: short runs are unchanged, and
#: prefix caching absorbs most of what a long one accumulates.
PROMPT_BUDGET = 245_760

#: Output budgets.
#:
#: Nothing below 4,096 any more. The Verifier's was 2,048, and a ``write_file``
#: of a 280-line file does not fit in it -- so the same call was truncated three
#: turns running, with no counter and nothing saying why. A mode that holds
#: ``write_file`` needs room to use it, and a mode that does not costs nothing
#: by having room it never spends: ``max_tokens`` is a ceiling, and an unused
#: ceiling is free.
MODES: dict[Mode, ModeConfig] = {
    # Answers a question. No write tools, so its ceiling is about prose.
    Mode.ASK: ModeConfig(Mode.ASK, PROMPT_BUDGET, 4096, False, 0.1, role="ask"),
    # Emits a plan through ``submit_plan``, which is structured output, and the
    # spike found no quality gain from thinking on structured output.
    Mode.PLANNER: ModeConfig(Mode.PLANNER, PROMPT_BUDGET, 4096, False, 0.1, role="planner"),
    # Every tool, including ``write_file``. The largest budget, because this is
    # the only mode that ever has to emit a whole file.
    Mode.AGENT: ModeConfig(Mode.AGENT, PROMPT_BUDGET, 6144, False, 0.1, role="coder"),
}


def config_for(mode: Mode | str) -> ModeConfig:
    """Look up a mode's configuration."""
    return MODES[Mode.coerce(mode)]
