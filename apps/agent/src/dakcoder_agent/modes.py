"""Per-mode configuration: budgets, output limits and reasoning control.

The reasoning setting is the consequential one, and it reverses this plan's
earlier guidance. Qwen3.8-27B is a reasoning model: left at its default it
returns ``reasoning_content`` with ``content: null``, and a ``max_tokens`` too
small to finish reasoning burns the whole turn for nothing.

The pre-implementation spike measured it (Part A §4.4). Identical prompt,
identical temperature, only ``max_tokens`` varied:

    thinking off, 1,000 tokens   ->   2.0s, a 517-character answer
    thinking on,  1,000 tokens   ->   4.5s, 1,247 chars of reasoning
    thinking on,  4,000 tokens   ->  31.4s, 9,948 chars of reasoning
    thinking on, 16,000 tokens   ->  15.4s, 4,828 chars of reasoning

Two things to read off that. Reasoning expands to fill the available budget,
non-deterministically — 1,247 then 9,948 then 4,828 characters for the same
prompt, so it is not a cost anyone can budget for. And the answer did not
improve: all three thinking-on runs returned the same ~330-character JSON that
thinking-off produced in two seconds. A 15x latency penalty for nothing.

It also failed outright twice in the spike, both times on a turn that had to
produce structured output.

So thinking is off by default, in every mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["Mode", "ModeConfig", "MODES", "config_for"]


class Mode(StrEnum):
    """The five agent modes (Part A §10).

    One loop, one system prompt, five overlays. Modes narrow the tool schema and
    sharpen the instruction; they do not fork the process, and they do not each
    get their own system prompt — that is finding S6, and it cost the frontend
    agent three cold prefills per task.
    """

    PLANNER = "planner"
    SCAFFOLDER = "scaffolder"
    CODER = "coder"
    VERIFIER = "verifier"
    DEBUGGER = "debugger"


@dataclass(frozen=True)
class ModeConfig:
    """What a mode changes about a request."""

    mode: Mode
    #: Hard cap on assembled prompt tokens (Part A §6.1).
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

    def __post_init__(self) -> None:
        # Rule 2 of §4.4: any thinking-on call gets at least 6,144 output
        # tokens. The budget has to hold a runaway reasoning block *plus* the
        # answer, and the spike showed reasoning alone exceeding 2,500 tokens on
        # a trivial prompt. A thinking-on mode with a tight budget does not
        # produce a worse answer — it produces no answer at all.
        if self.enable_thinking and self.max_tokens < 6144:
            raise ValueError(
                f"{self.mode} enables thinking with max_tokens={self.max_tokens}; "
                "reasoning expands to fill the budget and a truncated block returns "
                "content: null, wasting the turn. Use at least 6144."
            )


#: The prompt budget every mode gets. One number, because five copies of it is
#: how the Planner came to have a different one that nobody noticed.
#:
#: 245,760 — the model's own window, minus room to answer. The endpoint serves
#: Qwen3.8-27B at ``max_model_len`` 262,144 (asserted by the gateway probe),
#: prompt and completion share it, and the largest completion budget below is
#: 6,144; the remaining ~10k is headroom for template overhead and estimator
#: error, so a full prompt cannot push the answer out of the window.
#:
#: This supersedes 32,768, and the history is worth keeping because both of the
#: old numbers were load-bearing:
#:
#: It was 24,000 for the Planner once, and that killed a seventy-one-turn run:
#: compaction retains to ``budget * 0.35``, which at 24,000 could not hold one
#: capped file read, so the mode re-read the same files forever. 32,768 fixed
#: that case and was chosen against §5.3's cost targets (P95 prompt <= 24k,
#: effective prefill <= 180k); 65,536 was measured then and rejected for
#: breaching them.
#:
#: What changed the decision is the field evidence: at 32,768 real runs
#: compacted at ~23k, the recap evicted the very answers the model was working
#: from, the model re-asked for them, and the repeat detector ended the run.
#: Two transcripts died exactly this way. A budget that routinely destroys the
#: working set of an ordinary task is mispriced however good its prefill
#: numbers look, so the ceiling now defers to the model's window and the §5.3
#: targets are re-baselined in ``test_budget_regression.py`` with the measured
#: cost of this decision recorded next to them.
#:
#: The cost profile to keep in mind: the budget is a ceiling, not an
#: allocation. Short runs are unchanged. Long runs now accumulate instead of
#: compacting, so their per-turn prefill grows with the run; prefix caching
#: absorbs most of it, and ``_thrashing`` still catches a working set that
#: genuinely will not fit.
PROMPT_BUDGET = 245_760

MODES: dict[Mode, ModeConfig] = {
    # Reversed from the earlier plan. The spike found no quality gain on
    # structured output, and a plan *is* structured output.
    Mode.PLANNER: ModeConfig(Mode.PLANNER, PROMPT_BUDGET, 4096, False, 0.1),
    # Emits a JSON spec, which resource_scaffold then validates.
    Mode.SCAFFOLDER: ModeConfig(Mode.SCAFFOLDER, PROMPT_BUDGET, 2048, False, 0.1),
    # Mechanical edits and tool dispatch — the bulk of all turns.
    Mode.CODER: ModeConfig(Mode.CODER, PROMPT_BUDGET, 4096, False, 0.1),
    # Runs commands, reads output, reports.
    Mode.VERIFIER: ModeConfig(Mode.VERIFIER, PROMPT_BUDGET, 2048, False, 0.1),
    # The one place reasoning might genuinely pay, because ranking hypotheses is
    # the rare task where the reasoning text itself is the deliverable. Treated
    # as a Phase-2 A/B with the eval suite as judge — NOT as a default. Left off
    # here so that switching it on is a deliberate edit with a test behind it.
    Mode.DEBUGGER: ModeConfig(Mode.DEBUGGER, PROMPT_BUDGET, 6144, False, 0.1),
}


def config_for(mode: Mode | str) -> ModeConfig:
    """Look up a mode's configuration."""
    key = Mode(mode)
    return MODES[key]
