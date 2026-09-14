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
from dakcoder_agent.prompts import MODE_BUDGET, SYSTEM_BUDGET, mode_instruction, system_prompt
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
    assert used <= MODE_BUDGET, f"{mode} overlay is {used} tokens, budget {MODE_BUDGET}"


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
#:
#: `agent` moved again, from 3,850 to 4,100, for `revise_plan` (D-96). The
#: tripwire fired, the schema was trimmed to the shortest descriptions that
#: still say what the fields are, and what remains is the price of the acting
#: phase having a way to change course that is not a stop. Every other exit it
#: had -- a forced `finish`, a turn cap, a gate bound -- ended the run; the
#: reported transcripts ended `unverified` with the fix one different approach
#: away. About 250 tokens in the stable prefix, paid once per run.
#: Both moved again for the migration work: `planner` 3,100 to 3,250 and `agent`
#: 4,100 to 4,350. The tripwire fired, the schemas were trimmed to the shortest
#: wording that still names the fields, and what is left is two decisions.
#:
#: `submit_plan` gained `phases`, and the steps gained `phase` and `part`. That
#: is what makes a whole-service conversion plannable at all: `steps` caps at
#: eight and a service has forty handlers, so without a roadmap held separately
#: the only plan that fits is one whose steps are directories -- the plan whose
#: cursor never advances and whose every `finish` is refused on an objection it
#: cannot satisfy. About 150 tokens, and only on the planning turns.
#:
#: `agent` grew because `ask_developer` is now dispatchable there. Note what
#: this number measures: `registry.schemas_for`, which does not know whether the
#: run is a migration. `AgentLoop._tools` withholds the tool on every acting
#: turn that is not one, so an ordinary agent turn still pays the old prefix and
#: this ceiling is the migration case, measured against the looser of the two.
#: Every mode moved again, by about 110 tokens, and this one is the cheapest
#: arithmetic in the file.
#:
#: The system prompt gained two rules: that a reply may carry several calls, and
#: that a whole-file read is re-sent on every turn after it. Both are paid once
#: per turn in the stable prefix. What they buy is *turns removed*, and a turn is
#: the expensive unit here by three orders of magnitude.
#:
#: Measured on a real 74-turn run before the change: 7,597,219 prompt tokens
#: against 36,891 completion — 206 to 1 — with a mean prompt of 102,665 and
#: 71 of 74 turns making exactly one tool call (none made more than one). Eleven
#: file edits cost 1,691,433 prompt tokens between them, about 154,000 each,
#: because each one was its own turn and each turn re-sent the conversation.
#:
#: So 110 tokens a turn against ~103,000 per turn removed: the rules pay for
#: themselves nine hundred times over on the first turn they save, and there
#: were sixty spare. See `test_batching_cost.py`.
#: `planner` moved again, by twenty tokens, for the `accepts` field naming the
#: tools that can satisfy it. It is the cheapest line in this table to justify.
#: A migration plan wrote "legacy_audit reports no findings" as the acceptance
#: criterion of all seven of its steps; `legacy_audit` is an ask/planner tool,
#: so the acting phase had seven steps and no check it could apply to any of
#: them -- it called the tool, was refused, and spent the turn learning that its
#: own plan had given it nothing to verify against. Twenty tokens, on planning
#: turns only, against a turn thrown away on every phase of every migration.
PREFIX_CEILING = {Mode.ASK: 2_800, Mode.PLANNER: 3_380, Mode.AGENT: 4_420}


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
