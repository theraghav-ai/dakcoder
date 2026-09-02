"""Unit tests for the context manager (Part A §20.4)."""

from __future__ import annotations

import pytest

from dakcoder_agent import ContextManager, Layer, Mode, Recap, Role
from dakcoder_agent.context import (
    DEFAULT_TOOL_CAP,
    MAX_DIRECTIVES,
    MAX_MODE_MESSAGES,
    RECAP_BUDGET_TOKENS,
    TOOL_CAPS,
    OverBudgetError,
)
from dakcoder_agent.modes import PROMPT_BUDGET, ModeConfig, config_for
from dakcoder_shared.tokens import Calibration, estimate_tokens

SYSTEM = "You are dakcoder. " + ("Follow the n-api-template contract. " * 40)


def manager(**kw) -> ContextManager:
    kw.setdefault("system_prompt", SYSTEM)
    kw.setdefault("tool_schema_tokens", 1200)
    return ContextManager(**kw)


# ── the pinned head ─────────────────────────────────────────────────────────


def test_prefix_is_byte_identical_across_turns_and_modes():
    """Finding S6, as a regression.

    The frontend agent assigns a fresh message list with a *different* system
    prompt in each of its three phase runners, so every phase transition is a
    cold prefill. Here the head is one message that never changes, and a mode
    switch appends rather than rebuilds.
    """
    cm = manager()
    cm.set_task("Add a Pension resource")
    head = cm.build()[0]
    signature = cm.prefix_signature()

    for turn in range(10):
        cm.begin_turn()
        cm.append_assistant(f"working on step {turn}")
        cm.append_tool_result("rules_lint", "OK — 0 violations")
        if turn == 4:
            cm.switch_mode(Mode.ASK, "Run the verification gate.")
        if turn == 7:
            cm.switch_mode(Mode.AGENT, "Reproduce, then localise.")

        assert cm.build()[0] is head, "the system message was replaced"
        assert cm.build()[0].content == SYSTEM
        assert cm.prefix_signature() == signature


def test_messages_are_immutable():
    """§6.4's rule is easy to state and easy to violate three refactors later.

    Immutability makes the accident impossible rather than merely detectable.
    """
    cm = manager()
    cm.set_task("Add a Pension resource")
    msg = cm.build()[0]
    with pytest.raises(Exception):
        msg.content = "something else"  # type: ignore[misc]


def test_mode_switch_appends_and_never_rewrites_history():
    cm = manager()
    cm.set_task("t")
    cm.begin_turn()
    cm.append_assistant("planned")
    before = cm.build()

    cm.switch_mode(Mode.AGENT, "Execute plan step 1.")
    after = cm.build()

    assert len(after) == len(before) + 1
    # Everything that was there is still there, in order and unchanged. The
    # inserted instruction goes into the pinned head region, above the working
    # set, so the assistant turn keeps its relative position at the end.
    assert after[-1] == before[-1]
    assert cm.mode is Mode.AGENT


def test_task_and_acceptance_are_pinned_above_the_working_set():
    cm = manager()
    cm.set_task("Add Pension", plan="1. domain\n2. repo", acceptance=["go build clean"])
    cm.begin_turn()
    cm.append_assistant("x")

    layers = [m.layer for m in cm.build()]
    assert layers.index(Layer.TASK) < layers.index(Layer.WORKING_SET)
    task = next(m for m in cm.build() if m.layer is Layer.TASK)
    assert "go build clean" in task.content
    assert "1. domain" in task.content


# ── insertion caps ──────────────────────────────────────────────────────────


def test_tool_results_are_capped_at_insertion_not_at_display():
    """Finding S8.

    The frontend agent caps the SSE event at 4,000 characters while the payload
    that enters history is uncapped — so the developer sees a tidy preview of
    something that put 25k tokens into context permanently.
    """
    cm = manager()
    huge = "\n".join(f"line {i} of a very long file with plenty of content" for i in range(4000))
    cm.begin_turn()
    msg = cm.append_tool_result("read_file", huge, path="handler/user.go", line_range=(1, 4000))

    cap = TOOL_CAPS["read_file"]
    assert estimate_tokens(msg.content) <= cap.max_tokens * 1.1
    assert len(msg.content) < len(huge)


def test_elision_marker_is_machine_readable_and_actionable():
    """An elision the model cannot see is one it treats as absence."""
    cm = manager()
    # Sized off the cap rather than a literal, so the fixture keeps overflowing
    # it when the cap is tuned. 5,000 lines was comfortably over a 6,000-token
    # cap and comfortably under a 48,000-token one.
    lines = TOOL_CAPS["read_file"].max_tokens
    huge = "\n".join(f"line {i}" for i in range(lines))
    cm.begin_turn()
    msg = cm.append_tool_result("read_file", huge, path="handler/user.go", line_range=(1, lines))

    assert "[..." in msg.content and "...]" in msg.content
    assert "handler/user.go" in msg.content
    assert "elided" in msg.content
    # It has to say how to get the rest, or the model just concludes the
    # content does not exist.
    assert TOOL_CAPS["read_file"].recover in msg.content


def test_build_output_keeps_error_lines_verbatim():
    """§6.2: 'error and failure lines verbatim, always.'

    A build log is mostly noise around a handful of diagnostics. Head-or-tail
    truncation throws away exactly the file:line:col messages the agent needs
    while keeping the package list it does not — and an agent shown a build log
    with the errors elided concludes the build passed.
    """
    noise = ["compiling package foo/bar/baz " + "x" * 80 for _ in range(600)]
    errors = [
        "handler/pension.go:42:9: undefined: NewPensionRepository",
        "repo/postgres/pension.go:18:2: cannot use ins (variable of type sq.InsertBuilder)",
        "--- FAIL: TestCreatePension (0.01s)",
    ]
    log = "\n".join(noise[:300] + errors + noise[300:])

    cm = manager()
    cm.begin_turn()
    msg = cm.append_tool_result("go_build", log)

    for err in errors:
        assert err in msg.content, f"a diagnostic was elided: {err}"
    assert estimate_tokens(msg.content) <= TOOL_CAPS["go_build"].max_tokens * 1.2


def test_unknown_tools_get_the_default_cap():
    cm = manager()
    cm.begin_turn()
    msg = cm.append_tool_result("some_new_tool", "x" * 100_000)
    assert estimate_tokens(msg.content) <= DEFAULT_TOOL_CAP.max_tokens * 1.2


def test_small_results_pass_through_untouched():
    cm = manager()
    cm.begin_turn()
    body = "OK — 0 violations (12 files, 21 rules, 4ms)"
    assert cm.append_tool_result("rules_lint", body).content == body


# ── the file-slice ledger ───────────────────────────────────────────────────
#
# `ContextManager.SUPERSEDE_SLICES` says why this is still on at a 245,760-token
# budget, against the failure report's advice: measured, turning it off puts
# `test_budget_regression`'s P95 at 166,801 tokens against a 128,000 target.
# What the report is right about is the bug, and the containment rule below is
# what fixes that -- a read is replaced only when every line of it is inside a
# newer read further down.




def test_only_the_newest_read_of_a_path_survives():
    """§6.3, the largest single win on edit-heavy tasks.

    Read, patch, re-read, patch, re-read currently keeps three full copies in
    history for the rest of the run.
    """
    cm = manager()
    body = "\n".join(f"func Handler{i}() {{}}" for i in range(200))

    for turn in range(3):
        cm.begin_turn()
        cm.append_tool_result("read_file", body, path="handler/user.go", line_range=(1, 200))
        cm.append_assistant(f"patched, round {turn}")

    reads = [m for m in cm.build() if m.path == "handler/user.go"]
    assert len(reads) == 3, "messages must be superseded in place, never removed"
    assert sum(1 for m in reads if m.content.startswith("[stale read of ")) == 2
    assert not reads[-1].content.startswith("[stale read of "), "the newest read was collapsed"
    assert cm.stale_slices() == 2


def test_disjoint_reads_of_one_file_all_survive():
    """The run-killer of 2026-09-01, in four lines.

    A Planner read one 6,571-line handler at three disjoint windows. Superseding
    on the path alone left two stubs saying "re-read if needed" over lines that
    were then nowhere in the context — and the loop's repeat ledger refused the
    re-read the stub had just asked for. Neither message was wrong and neither
    could be obeyed, so the run alternated between them until it was killed for
    making no progress.
    """
    cm = manager()
    cm.begin_turn()
    for span in ((40, 150), (153, 205), (3777, 3840)):
        cm.append_tool_result(
            "read_file", f"body {span}", path="handler/paogen.go", line_range=span
        )

    reads = [m for m in cm.build() if m.path == "handler/paogen.go"]
    assert [m.content for m in reads] == [
        "body (40, 150)",
        "body (153, 205)",
        "body (3777, 3840)",
    ], "disjoint windows carry different lines; none of them supersedes another"
    assert cm.stale_slices() == 0


def test_a_containing_read_still_supersedes():
    """The saving the ledger exists for, which containment must not give up."""
    cm = manager()
    cm.begin_turn()
    cm.append_tool_result("read_file", "narrow", path="a.go", line_range=(60, 120))
    cm.append_tool_result("read_file", "wider", path="a.go", line_range=(1, 400))
    cm.append_tool_result("read_file", "whole", path="a.go")

    reads = [m for m in cm.build() if m.path == "a.go"]
    assert reads[0].content.startswith("[stale read of a.go lines 60-120")
    assert reads[1].content.startswith("[stale read of a.go lines 1-400")
    assert reads[2].content == "whole"
    assert cm.stale_slices() == 2


def test_a_partial_overlap_keeps_both():
    """Half a window is not the window; 1-120 then 60-180 loses lines 1-59."""
    cm = manager()
    cm.begin_turn()
    cm.append_tool_result("read_file", "first", path="a.go", line_range=(1, 120))
    cm.append_tool_result("read_file", "second", path="a.go", line_range=(60, 180))

    assert [m.content for m in cm.build() if m.path == "a.go"] == ["first", "second"]


def test_the_ledger_bounds_by_distinct_files_not_by_reads():
    """The property that makes the ledger worth having."""
    cm = manager()
    body = "\n".join(f"line {i} with some real content in it" for i in range(300))

    cm.begin_turn()
    for _ in range(20):
        cm.append_tool_result("read_file", body, path="handler/user.go", line_range=(1, 300))
    one_file_many_reads = cm.usage().total

    cm2 = manager()
    cm2.begin_turn()
    for i in range(20):
        cm2.append_tool_result("read_file", body, path=f"handler/file{i}.go", line_range=(1, 300))
    many_files = cm2.usage().total

    assert one_file_many_reads < many_files / 5, (
        "twenty reads of one file should cost about one file, not twenty"
    )


def test_stale_stubs_are_not_re_superseded():
    cm = manager()
    cm.begin_turn()
    for _ in range(3):
        cm.append_tool_result("read_file", "body", path="a.go")
    stubs = [m.content for m in cm.build() if m.content.startswith("[stale read of ")]
    assert len(stubs) == 2
    assert all(s.count("[stale read of") == 1 for s in stubs)


# ── budget ──────────────────────────────────────────────────────────────────


def test_usage_accounts_for_every_layer_and_the_tool_schemas():
    cm = manager(tool_schema_tokens=1200)
    cm.set_task("Add Pension", acceptance=["clean"])
    cm.switch_mode(Mode.AGENT, "Execute the plan.")
    cm.begin_turn()
    cm.append_assistant("thinking")
    cm.append_tool_result("rules_lint", "OK")

    use = cm.usage()
    assert use.tools == 1200
    assert use.by_layer[Layer.SYSTEM] > 0
    assert use.by_layer[Layer.TASK] > 0
    assert use.by_layer[Layer.MODE] > 0
    assert use.by_layer[Layer.WORKING_SET] > 0
    assert use.total == sum(use.by_layer.values()) + 1200
    assert 0 < use.used_pct < 100


@pytest.mark.parametrize(
    "mode,budget",
    # Read off the constant rather than restated, so raising the budget is one
    # edit rather than a hunt for every place that pinned the old number.
    [(Mode.PLANNER, PROMPT_BUDGET), (Mode.AGENT, PROMPT_BUDGET), (Mode.AGENT, PROMPT_BUDGET)],
)
def test_budget_follows_the_mode(mode: Mode, budget: int):
    assert manager(mode=mode).budget == budget


@pytest.mark.parametrize("mode", list(Mode))
def test_compaction_can_retain_one_whole_file_read(mode: Mode):
    """The invariant the Planner's 24,000 budget broke.

    Compaction retains to ``budget * 0.35`` minus the pinned head. A capped
    ``read_file`` is 6,000 tokens. When the retention floor is below that,
    compaction cannot keep even one of the files the model just read — so the
    model re-reads it, which puts the context back over the threshold that fired
    the compaction, which evicts it again. That circuit ran seventy-one turns
    and twenty-five compactions in one session without producing a plan.

    Checked for every mode, because the mode that broke it was the one nobody
    thought to check: the Planner reads the most and had the smallest budget.
    """
    cm = manager(mode=mode)
    cm.set_task("write a new handler")
    cm.switch_mode(mode, "do the thing")

    overhead = (
        1200  # tool_schema_tokens, as `manager` sets it
        + estimate_tokens(SYSTEM)
        + estimate_tokens("do the thing")
        + estimate_tokens(cm.build()[-1].content)
        + RECAP_BUDGET_TOKENS
    )
    floor = int(cm.budget * 0.35) - overhead

    assert floor >= TOOL_CAPS["read_file"].max_tokens, (
        f"{mode} retains {floor:,} tokens, below one capped read_file "
        f"({TOOL_CAPS['read_file'].max_tokens:,}); compaction cannot keep a file it just read"
    )


def test_compaction_triggers_at_the_threshold():
    cm = manager(compact_at=0.70)
    cm.set_task("t")
    assert not cm.should_compact()

    cm.begin_turn()
    while not cm.should_compact():
        cm.append_tool_result("read_file", "x " * 3000, path=f"f{cm.usage().total}.go")
    assert cm.usage().total >= cm.budget * 0.70


# ── compaction ──────────────────────────────────────────────────────────────


def _summariser(messages) -> Recap:
    return Recap(
        goal="add Pension resource",
        plan_step="6 of 8 — wiring FxHandler",
        files_created=("core/domain/pension.go",),
        decisions=("table = pensions (user-confirmed)",),
        do_not_retry=("hand-editing request_*_validator.go (generated; run govalid_gen)",),
        turns=(1, len(messages)),
    )


def test_compaction_summarises_rather_than_truncates():
    """Cline's lesson: truncation drops the decision that explains the current
    diff, and the agent then re-derives it wrongly."""
    cm = manager()
    cm.set_task("Add Pension")
    for i in range(12):
        cm.begin_turn()
        cm.append_assistant(f"step {i}")
        cm.append_tool_result("read_file", "body " * 500, path=f"f{i}.go")

    before = cm.usage().total
    recap = cm.compact(_summariser, keep_recent=4)
    after = cm.usage().total

    assert after < before
    assert cm.compactions == 1
    body = next(m for m in cm.build() if m.layer is Layer.RECAP).content
    assert "add Pension resource" in body
    # Recording dead ends is what stops the post-compaction agent repeating them.
    assert "Do not retry" in body
    assert "govalid_gen" in body
    assert recap.goal == "add Pension resource"


def test_compaction_keeps_the_most_recent_turns_verbatim():
    """The agent is usually mid-edit; a summary of four seconds ago is strictly
    worse than the thing itself."""
    cm = manager()
    for i in range(12):
        cm.begin_turn()
        cm.append_assistant(f"marker-{i}")

    cm.compact(_summariser, keep_recent=3)
    tail = [m.content for m in cm.build() if m.layer is Layer.WORKING_SET]
    assert tail == ["marker-9", "marker-10", "marker-11"]


def test_compaction_never_evicts_the_pinned_layers():
    cm = manager()
    cm.set_task("Add Pension", acceptance=["go build clean"])
    cm.switch_mode(Mode.AGENT, "Execute.")
    for i in range(12):
        cm.begin_turn()
        cm.append_assistant(f"step {i}")

    cm.compact(_summariser)
    layers = {m.layer for m in cm.build()}
    assert Layer.SYSTEM in layers
    assert Layer.TASK in layers
    assert Layer.MODE in layers
    task = next(m for m in cm.build() if m.layer is Layer.TASK)
    assert "go build clean" in task.content


def test_compaction_rebuilds_the_ledger():
    """A stale index after compaction would supersede the wrong message."""
    cm = manager()
    for i in range(10):
        cm.begin_turn()
        cm.append_tool_result("read_file", f"body {i}", path="handler/user.go")

    cm.compact(_summariser, keep_recent=2)
    cm.begin_turn()
    cm.append_tool_result("read_file", "newest", path="handler/user.go")

    reads = [m for m in cm.build() if m.path == "handler/user.go"]
    assert reads[-1].content == "newest"
    assert all(m.content.startswith("[stale read of ") for m in reads[:-1])


def test_compacting_an_empty_working_set_is_a_no_op():
    cm = manager()
    cm.set_task("t")
    assert cm.compact(_summariser) == Recap(turns=(0, 0))
    assert cm.compactions == 0


# ── calibration ─────────────────────────────────────────────────────────────


def test_estimate_is_recalibrated_from_real_usage():
    """§16.4: the frontend agent reserves a flat 4,096 tokens and never
    reconciles, which is why its quota is fiction.

    The `prompt_tokens` here is derived from the *whole* prompt -- message
    content plus the tool schemas -- because that is what the endpoint reports.
    Deriving it from content alone is defect T11: the numerator counted less
    than the denominator, every observed ratio came out low, the calibrated
    ratio was dragged toward its floor, and every estimate built on it ran high.
    That estimate is what compaction fires on and what `X-Estimated-Tokens`
    reserves against a 600k/hour quota, so a long run over-reserved its way into
    429s it had not earned.
    """
    cal = Calibration()
    assert not cal.calibrated

    cm = manager(calibration=cal)
    cm.set_task("t")
    cm.begin_turn()
    cm.append_assistant("some content of a realistic length " * 20)

    content_chars = sum(len(m.content) for m in cm.build())
    # 1,200 schema tokens is what `manager` declares; at the default 4.0 ratio
    # that is 4,800 characters on the wire the endpoint will also charge for.
    whole_prompt = content_chars + 1_200 * 4
    cm.observe_usage(prompt_tokens=whole_prompt // 3)  # a denser tokenizer than assumed
    assert cal.calibrated
    assert cal.ratio < 4.0


def test_calibration_counts_the_tool_schemas_and_not_just_the_messages():
    """Defect T11, as a regression.

    Two managers, identical messages, identical reported `prompt_tokens`. The
    one carrying tool schemas is describing a *bigger* prompt for the same
    token count, so its characters-per-token must come out higher. When the
    numerator ignored the schemas the two were indistinguishable.
    """
    bare, with_tools = Calibration(), Calibration()

    for cal, schema_tokens in ((bare, 0), (with_tools, 4_000)):
        cm = manager(calibration=cal, tool_schema_tokens=schema_tokens)
        cm.set_task("t")
        cm.begin_turn()
        cm.append_assistant("some content of a realistic length " * 20)
        cm.observe_usage(prompt_tokens=2_000)

    assert with_tools.ratio > bare.ratio


def test_calibration_is_bounded_against_a_malformed_usage_payload():
    cal = Calibration()
    for _ in range(50):
        cal.observe(estimated_chars=1000, actual_tokens=1)  # ratio 1000
    assert cal.ratio <= cal.max_ratio
    for _ in range(50):
        cal.observe(estimated_chars=1, actual_tokens=100_000)
    assert cal.ratio >= cal.min_ratio


def test_a_single_observation_nudges_rather_than_redefines():
    cal = Calibration()
    before = cal.ratio
    cal.observe(estimated_chars=3000, actual_tokens=1500)  # ratio 2.0
    assert before > cal.ratio > 2.0


# ── mode configuration ──────────────────────────────────────────────────────


def test_thinking_is_off_in_every_mode():
    """The spike's headline finding: a 15x latency penalty for no quality gain,
    and two outright failures on turns that had to produce structured output."""
    for mode in Mode:
        assert config_for(mode).enable_thinking is False


def test_a_thinking_mode_must_budget_for_a_runaway_reasoning_block():
    """§4.4 rule 2. Reasoning expands to fill the budget, non-deterministically —
    1,247 then 9,948 then 4,828 characters for the same prompt — so a tight
    budget does not produce a worse answer, it produces content: null."""
    with pytest.raises(ValueError, match="content: null"):
        ModeConfig(Mode.AGENT, 32_768, 4096, enable_thinking=True, temperature=0.1)

    ok = ModeConfig(Mode.AGENT, 32_768, 6144, enable_thinking=True, temperature=0.1)
    assert ok.enable_thinking


def test_prompt_and_output_budgets_are_tracked_separately():
    """Conflating them is how a mode ends up with room to think and none to
    answer."""
    cfg = config_for(Mode.PLANNER)
    assert cfg.prompt_budget == PROMPT_BUDGET
    assert cfg.max_tokens == 4096


# ── inspection ──────────────────────────────────────────────────────────────


def test_inspect_reports_what_the_context_inspector_renders():
    cm = manager()
    cm.set_task("Add Pension")
    cm.begin_turn()
    cm.append_tool_result("read_file", "x" * 50_000, path="handler/user.go")

    snap = cm.inspect()
    for key in ("mode", "turn", "total_tokens", "budget", "used_pct", "by_layer", "compactions"):
        assert key in snap
    assert snap["mode"] == "ask"
    assert snap["by_layer"]["working_set"] > 0


def test_over_budget_error_exists_for_the_overflow_path():
    """§6.5's overflow_recovery path needs a typed failure, not a silent trim:
    a prompt that quietly dropped the task would answer the wrong question."""
    assert issubclass(OverBudgetError, RuntimeError)


# ── the pinned layers are pinned, not unbounded ─────────────────────────────


def test_the_mode_layer_does_not_grow_without_limit():
    """MODE is pinned, so compaction can never reclaim it.

    A run that switched mode on nearly every turn reached thirteen instructions
    stacked in the head -- five Coder, four Verifier -- each contradicting the
    one above it, in the one layer that is exempt from eviction. Bounding it at
    six stopped it growing and did nothing about six sets of live instructions:
    the Verifier read the Coder's and announced it was about to make an edit.

    The bound is one now. The head carries the instruction in force and nothing
    else, so there is no "which of these is current" for the model to get wrong
    and no preamble needed to tell it.
    """
    cm = manager()
    cm.set_task("t")
    for _ in range(20):
        cm.begin_turn()
        cm.switch_mode(Mode.AGENT, "Execute one plan step.")
        cm.switch_mode(Mode.ASK, "Report; do not fix.")

    modes = [m for m in cm.build() if m.layer is Layer.MODE]
    assert len(modes) <= MAX_MODE_MESSAGES
    assert modes[-1].content == "Report; do not fix.", (
        "the current mode must be the only word"
    )

def test_re_entering_the_mode_you_are_in_restates_nothing():
    """Cheaper than the guard in the loop and, unlike it, keeps the head
    byte-identical — so a re-entry costs no prefill either."""
    cm = manager()
    cm.set_task("t")
    cm.switch_mode(Mode.AGENT, "Execute one plan step.")
    before = cm.build()

    cm.switch_mode(Mode.AGENT, "Execute one plan step.")

    assert cm.build() == before


def test_a_pinned_directive_survives_compaction():
    """The working-set copy of a developer message is the first thing evicted.

    In a run compacting every other turn that is two turns of life, after which
    the session carries on with the task it was redirected away from — and the
    developer has no way to tell, because the redirect appeared to land.
    """
    cm = manager()
    cm.set_task("Add a Pension resource")
    cm.begin_turn()
    cm.append_user("stop reading and write the handler")
    cm.pin_directive("stop reading and write the handler")
    cm.append_assistant("reading one more file")

    cm.compact(lambda messages: Recap(goal="g"), keep_recent=0)

    assert cm.directives == ("stop reading and write the handler",)
    assert "stop reading and write the handler" in "\n".join(m.content for m in cm.build())


def test_the_directive_block_is_bounded():
    cm = manager()
    cm.set_task("t")
    for i in range(20):
        cm.pin_directive(f"correction {i}")

    assert len(cm.directives) == MAX_DIRECTIVES
    assert cm.directives[-1] == "correction 19", "the newest correction was dropped"


def test_the_same_directive_twice_is_pinned_once():
    cm = manager()
    cm.set_task("t")
    cm.pin_directive("use patch_file")
    cm.pin_directive("use patch_file")

    assert cm.directives == ("use patch_file",)


def test_a_planner_turn_that_reads_two_files_does_not_trip_compaction():
    """The turn the seventy-one-turn session made on every one of its turns.

    `repo_map` plus two `read_file` results is what orienting in an unfamiliar
    service costs. At the old 24,000 budget that turn was over the threshold
    the moment it landed, so it was compacted away before the model could use
    it — and the model, now missing the files, read them again.
    """
    cm = manager(mode=Mode.PLANNER)
    cm.set_task("write a new handler to send a whatsapp message")
    cm.begin_turn()
    # Big enough to reach the insertion caps, which is what the session's files
    # actually did — 312 and 718 lines of handler and service code.
    cm.append_tool_result(
        "repo_map",
        "\n".join(f"  handler/file{i}.go  types: X{i}  funcs: NewX{i}, ListX{i}" for i in range(900)),
    )
    body = "\n".join(
        f"func (h *WhatsappHandler) Method{i}(sctx *serverRoute.Context) error {{ return nil }}"
        for i in range(700)
    )
    for path in ("handler/whatsapp.go", "internal/app/service/whatsapp.go"):
        cm.append_tool_result("read_file", body, path=path)

    assert not cm.should_compact(), (
        f"the orienting turn is {cm.usage().total:,} tokens against a "
        f"{cm.budget:,} budget and already over the threshold"
    )


def test_the_tools_array_is_counted_against_the_budget():
    """It is part of the prompt and it is sent on every call.

    `serve.py` built every session's context with the default of zero, so the
    schemas were charged to the endpoint and counted as nothing here — and the
    compaction threshold, the retention floor and `complete`'s budget check
    were all decided against a prompt smaller than the one actually sent.
    """
    cm = ContextManager(mode=Mode.AGENT, system_prompt=SYSTEM)
    cm.set_task("t")
    assert cm.usage().tools == 0

    cm.observe_tool_schemas(1_414)

    assert cm.usage().tools == 1_414
    assert cm.usage().total > sum(cm.usage().by_layer.values()) - 1


def test_only_the_overlay_in_force_is_in_the_head():
    """The Verifier was handed no write tool and its overlay opened "Report; do
    not fix anything here". It announced "My job is to make the edit" on four
    separate turns, because the Coder's instruction was still sitting two
    messages above its own in the pinned head.

    The earlier fix prefixed each overlay with "this replaces the ones above
    it", which is one more sentence in a pile of contradictory sentences for a
    27B model at temperature 0.1 to weigh. Removing the pile is the fix.
    """
    cm = manager()
    cm.set_task("t")
    cm.switch_mode(Mode.AGENT, "Execute one plan step.")
    cm.switch_mode(Mode.ASK, "Report; do not fix anything here.")

    overlays = [m for m in cm.build() if m.layer is Layer.MODE]
    assert len(overlays) == 1, "a superseded overlay must not stay in the head"
    assert overlays[0].content == "Report; do not fix anything here."
    assert "Execute one plan step." not in overlays[0].content
