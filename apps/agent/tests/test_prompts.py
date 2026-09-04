"""Tests for the prompts.

A prompt is the easiest thing in a codebase to grow by accident: every addition
looks individually reasonable, and nothing fails when it gets too long — the
working set just quietly shrinks. So the budget is a test, and the stable-prefix
rule is a test, because neither is otherwise observable.
"""

from __future__ import annotations

import json

import pytest

from dakcoder_agent.context import ContextManager
from dakcoder_agent.modes import Mode
from dakcoder_agent.prompts import (
    MODE_BUDGETS,
    SYSTEM_BUDGET,
    mode_instruction,
    system_prompt,
)
from dakcoder_agent.tools import registry
from dakcoder_shared.tokens import estimate_tokens


def test_the_system_prompt_fits_its_budget() -> None:
    """Part A section 6.1 gives it 1,200 tokens of a 32,768 prompt.

    Every token here is spent on every turn of every task, so the budget is not
    a style preference — it is the share of context that cannot hold code.
    """
    used = estimate_tokens(system_prompt())
    assert used <= SYSTEM_BUDGET, f"system prompt is {used} tokens, budget {SYSTEM_BUDGET}"


@pytest.mark.parametrize("mode", list(Mode))
def test_each_mode_overlay_stays_small(mode: Mode) -> None:
    """A mode overlay needing three hundred tokens to explain itself is usually
    a mode that has not been decided."""
    used = estimate_tokens(mode_instruction(mode))
    budget = MODE_BUDGETS[mode]
    assert used <= budget, f"{mode} overlay is {used} tokens, budget {budget}"


@pytest.mark.parametrize("mode", list(Mode))
def test_every_mode_has_an_overlay(mode: Mode) -> None:
    text = mode_instruction(mode)
    assert text.strip()
    assert str(mode) in text.lower() or mode.name.lower() in text.lower()


def test_the_system_prompt_states_what_is_out_of_scope() -> None:
    """The agent is a Go backend engineer, not an assistant.

    Asserted because the failure is invisible: an agent that cheerfully answers
    "how are you" looks like it is working. It is spending a shared GPU budget on
    a question nobody deployed it for, and every such answer is a turn of quota
    the developer waiting behind it does not get. The rule lives in the *system*
    prompt rather than a mode overlay so that no mode can be entered without it.
    """
    prompt = system_prompt().lower()
    assert "out of scope" in prompt
    assert "one sentence" in prompt, "a decline that rambles is an answer"


# ── the stable prefix ───────────────────────────────────────────────────────


def test_every_mode_gets_the_same_system_prompt() -> None:
    """Finding S6, as a test.

    The frontend agent assigned a fresh message list with a different system
    prompt in each of `_run_planner`, `_run_coder` and `_run_debugger` — three
    cold prefills per task, by design, even with prefix caching switched on.
    """
    first = system_prompt()
    for _mode in Mode:
        assert system_prompt() is first, "the system prompt must be one object, shared"


def test_a_mode_switch_appends_and_does_not_rewrite_the_head() -> None:
    """The rule §6.4 states: the message list is append-only below the pinned
    head, and any mutation of messages[0..k] is a cache-invalidating bug."""
    context = ContextManager(mode=Mode.PLANNER, system_prompt=system_prompt())
    context.set_task("Add a Pension resource")
    before = context.prefix_signature()
    head = context.build()[0].content

    for mode in (Mode.AGENT, Mode.AGENT, Mode.ASK, Mode.AGENT):
        context.switch_mode(mode, mode_instruction(mode))

    assert context.prefix_signature() == before
    assert context.build()[0].content == head


def test_the_prompt_is_normalised_so_a_checkout_cannot_change_it() -> None:
    """A prefix whose bytes depend on the reader's git configuration is not a
    stable prefix: it produces a different cache key on a colleague's machine
    for a file neither of them edited."""
    assert "\r" not in system_prompt()
    for mode in Mode:
        assert "\r" not in mode_instruction(mode)


# ── what the prompt has to say ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "phrase",
    [
        "dblib.Psql",
        "pgx.ErrNoRows",
        "fx.Annotate",
        ".Name(",
        "request_*_validator.go",
        "gin.Context",
    ],
)
def test_the_contract_essentials_are_stated(phrase: str) -> None:
    """The five failure classes that recur (§13.2) are each named here, because
    a rule the model has to look up is a rule it applies one turn late."""
    assert phrase in system_prompt()


def test_the_gate_is_described_as_something_the_model_does_not_control() -> None:
    """Not as a request. "Please verify your work" is a hope; "your work is
    verified by a gate you cannot skip" is a fact the model can reason from."""
    text = system_prompt().lower()
    assert "cannot skip" in text or "do not control" in text


def test_the_irreversible_actions_are_named() -> None:
    text = system_prompt().lower()
    assert "ddl" in text, "the agent never applies DDL"
    assert "credential" in text or "password" in text


def test_an_unreported_gap_is_called_out_as_worse_than_a_failure() -> None:
    """The instruction that keeps a partial result honest. Without it a model
    that could not finish reports the part it did finish."""
    assert "say so" in system_prompt().lower()


# ── the whole prefix ────────────────────────────────────────────────────────


#: The prefix ceiling, per mode.
#:
#: §6.4 estimates system + schemas at ~2,400 tokens and two of the old five
#: modes already exceeded it; D-43 accepted that, on the ground that the overage
#: is in the *stable prefix* — paid once per prefix rather than per turn — and
#: that buying it back means shortening tool descriptions that exist to stop the
#: model misusing the tools.
#:
#: `agent` is higher than the rest, and that is the priced cost of collapsing
#: Coder, Scaffolder, Verifier and Debugger into one mode. It holds 22 tools
#: where the Coder held 14, and two of the eight it gained are `go_vet` and
#: `go_test` — the checks the Coder was failed by and could not run. Read-only
#: modes are unaffected and came *down*: `ask` is 2,533.
#:
#: Asserted per mode rather than as one number so that a mode growing is a test
#: failure and not a rounding error absorbed by the loosest ceiling.
#:
#: `agent` moved from 3,800 to 3,850 for `write_file`'s `append` parameter, and
#: the tripwire worked exactly as this comment says it should: the change failed
#: the test, the text was tightened twice, and what remained was a decision
#: rather than a rounding error. What the 41 tokens buy is the model knowing,
#: before it tries, that a file too large for one reply can be written in
#: chunks (BUG FS-1). The alternative is not free: without the hint a model
#: discovers the wall by hitting it, which costs a full 6,144-token reply and a
#: prefill to find out, and the reported transcript spent four turns doing that
#: and never got there. This text is in the stable prefix, so it is a cache hit
#: after the first call of a run; the wasted turn is not.
#:
#: Bought back on the way: `write_file`'s description no longer repeats "use
#: patch_file for that", which `ToolSpec.instead` and the runtime refusal
#: message both already say, and neither of those is in the prefix.
PREFIX_CEILING = {Mode.ASK: 2_700, Mode.PLANNER: 3_100, Mode.AGENT: 3_850}


@pytest.mark.parametrize("mode", list(Mode))
def test_the_prefix_is_reported_honestly_against_the_target(mode: Mode) -> None:
    """The stable prefix, measured against a ceiling recorded per mode."""
    prefix = estimate_tokens(system_prompt()) + estimate_tokens(
        json.dumps(registry.schemas_for(mode))
    )
    ceiling = PREFIX_CEILING[mode]
    assert prefix <= ceiling, f"{mode} prefix is {prefix} tokens, ceiling {ceiling}"


def test_the_system_prompt_and_schemas_leave_the_working_set_intact() -> None:
    """The number that actually matters: what is left for code.

    §6.1 allocates ~27,500 to the live working set. The prefix eating into it is
    the real cost of every token spent above, and this is where it shows up.
    """
    context = ContextManager(mode=Mode.AGENT, system_prompt=system_prompt())
    context.set_task("Add a Pension resource", acceptance=["go build ./... clean"])
    schemas = estimate_tokens(json.dumps(registry.schemas_for(Mode.AGENT)))

    remaining = context.budget - context.usage().total - schemas
    assert remaining >= 26_000, f"only {remaining} tokens left for the working set"


# ── the phase is not the limit ──────────────────────────────────────────────


def test_the_prompt_frames_a_narrow_tool_list_as_a_phase_not_a_limit() -> None:
    """Asked "can you edit files or create new files?", the agent said no.

    Truthfully, about its turn: the Planner is handed thirteen read-only tools
    and its overlay said "you have read-only tools". But the system prompt
    described an agent that writes — `patch_file`, `write_file`, "say what you
    are doing before each edit" — and never once said the modes were phases of
    one run. With no frame for that, the model resolved the contradiction with
    the more specific, more recent statement and told a developer the product
    cannot do the thing it exists to do.

    The rule lives in the *system* prompt so that no phase can be entered
    without it.
    """
    text = system_prompt().lower()
    assert "phase" in text, "nothing tells the model its tool list is a phase"
    assert "not the limit" in text


def test_the_read_only_phase_names_the_one_that_writes() -> None:
    """The Planner has no write tool and is where a first message lands, so it
    is where this question gets asked. Saying "read-only" without saying which
    phase writes is what produced the wrong half of the answer."""
    text = mode_instruction(Mode.PLANNER).lower()
    assert "read-only" in text
    assert "agent" in text, "the read-only phase must name the phase that writes"


def test_the_planner_really_has_no_write_tool() -> None:
    """The premise of the two tests above, asserted rather than assumed.

    If this ever stops being true the prompts are describing a split that no
    longer exists, and both tests above are checking prose against nothing.
    """
    writes = {"write_file", "patch_file", "delete_file", "go_mod"}
    planner = {s["function"]["name"] for s in registry.schemas_for(Mode.PLANNER)}
    coder = {s["function"]["name"] for s in registry.schemas_for(Mode.AGENT)}

    assert not (planner & writes), f"the Planner can write: {sorted(planner & writes)}"
    assert writes <= coder, f"the Coder cannot write: {sorted(writes - coder)}"
