"""Unit tests for the context manager (Part A §20.4)."""

from __future__ import annotations

import pytest

from dakcoder_agent import ContextManager, Layer, Mode, Recap, Role
from dakcoder_agent.context import DEFAULT_TOOL_CAP, TOOL_CAPS, OverBudgetError
from dakcoder_agent.modes import ModeConfig, config_for
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
            cm.switch_mode(Mode.VERIFIER, "Run the verification gate.")
        if turn == 7:
            cm.switch_mode(Mode.DEBUGGER, "Reproduce, then localise.")

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

    cm.switch_mode(Mode.CODER, "Execute plan step 1.")
    after = cm.build()

    assert len(after) == len(before) + 1
    # Everything that was there is still there, in order and unchanged. The
    # inserted instruction goes into the pinned head region, above the working
    # set, so the assistant turn keeps its relative position at the end.
    assert after[-1] == before[-1]
    assert cm.mode is Mode.CODER


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
    huge = "\n".join(f"line {i}" for i in range(5000))
    cm.begin_turn()
    msg = cm.append_tool_result("read_file", huge, path="handler/user.go", line_range=(1, 5000))

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
    cm.switch_mode(Mode.CODER, "Execute the plan.")
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
    [(Mode.PLANNER, 24_000), (Mode.CODER, 32_768), (Mode.DEBUGGER, 32_768)],
)
def test_budget_follows_the_mode(mode: Mode, budget: int):
    assert manager(mode=mode).budget == budget


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
    cm.switch_mode(Mode.CODER, "Execute.")
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
    reconciles, which is why its quota is fiction."""
    cal = Calibration()
    assert not cal.calibrated

    cm = manager(calibration=cal)
    cm.set_task("t")
    cm.begin_turn()
    cm.append_assistant("some content of a realistic length " * 20)

    chars = sum(len(m.content) for m in cm.build())
    cm.observe_usage(prompt_tokens=chars // 3)  # a denser tokenizer than assumed
    assert cal.calibrated
    assert cal.ratio < 4.0


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
        ModeConfig(Mode.DEBUGGER, 32_768, 4096, enable_thinking=True, temperature=0.1)

    ok = ModeConfig(Mode.DEBUGGER, 32_768, 6144, enable_thinking=True, temperature=0.1)
    assert ok.enable_thinking


def test_prompt_and_output_budgets_are_tracked_separately():
    """Conflating them is how a mode ends up with room to think and none to
    answer."""
    cfg = config_for(Mode.PLANNER)
    assert cfg.prompt_budget == 24_000
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
    assert snap["mode"] == "coder"
    assert snap["by_layer"]["working_set"] > 0


def test_over_budget_error_exists_for_the_overflow_path():
    """§6.5's overflow_recovery path needs a typed failure, not a silent trim:
    a prompt that quietly dropped the task would answer the wrong question."""
    assert issubclass(OverBudgetError, RuntimeError)
