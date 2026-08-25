"""The context-budget regression gate (Part A §20.5).

Non-negotiable, in CI, not on a dashboard. §21's first risk is that "the context
work is deferred as 'optimisation' and the agent ships slow", and its mitigation
is exactly this file: the targets are asserted, so the regression is a red build
rather than a slow agent nobody can quite explain.

The targets, from §5.3:

    prompt tokens, P95, coder turn        <= 24,000   (frontend agent: ~68,000)
    total prefill for a 25-turn task      <= 180,000  (frontend agent: ~1.25M)

The prefill target needs reading carefully. §5.3 writes it as
"<= 180k (cap + compaction + **prefix reuse**)", and the row beneath it marks
the >=80% cache-hit rate as *contingent on plan.md §9 Q1* — which is unresolved,
because ``prompt_tokens_details.cached_tokens`` is absent from this endpoint and
the hit rate therefore cannot be measured at all.

So 180k is the *effective* prefill once the cache is doing its job, not the raw
sum of prompt sizes. Both are measured here:

    raw       sum of every turn's prompt      — the no-cache worst case
    novel     tokens not in the previous
              turn's shared prefix            — the perfect-cache best case

The truth sits between them and moves the day §9 Q1 is answered. Asserting only
the raw sum would fail a design that is working; asserting only the novel count
would pass one that had quietly doubled its per-turn cost. So both are asserted,
against targets each can justify.

The simulation below is built from §5.2's own worked estimate, so the numbers
are comparable to the ones that motivated the work:

    fixed overhead per turn      5,700 tok   (system prompt + tool schemas)
    repo_map, from turn 1       25,000 tok   (resident forever, uncapped)
    new content per turn         1,500 tok   (assistant + tool result)

The unmanaged baseline is computed alongside, from the same content, so the test
says *why* the target matters rather than only whether it was met.
"""

from __future__ import annotations

import statistics

import pytest

from dakcoder_agent import ContextManager, Mode, Recap
from dakcoder_shared.tokens import estimate_tokens

TURNS = 25
P95_PROMPT_TOKENS = 24_000
TOTAL_PREFILL = 180_000

# ~1,200 tokens, per §6.1's system-prompt allocation.
SYSTEM_PROMPT = (
    "You are dakcoder, a Go backend agent for the IT 2.0 n-api-template. "
    "Follow the template contract exactly. " * 60
)
TOOL_SCHEMA_TOKENS = 1_200


def _repo_map(tokens: int = 25_000) -> str:
    """A repo_map result of the size the frontend agent actually emitted."""
    line = "  handler/pension.go  types: PensionHandler, CreatePensionRequest  funcs: NewPensionHandler"
    return "\n".join(line for _ in range(tokens * 4 // len(line)))


def _go_file(name: str, lines: int = 400) -> str:
    return "\n".join(
        f"func (h *{name}Handler) Method{i}(sctx *serverRoute.Context) error {{ return nil }}"
        for i in range(lines)
    )


def _build_log(errors: int = 3) -> str:
    noise = ["compiling gitlab.cept.gov.in/it-2.0-common/n-api-server/handler" for _ in range(400)]
    errs = [f"handler/pension.go:{40 + i}:9: undefined: Symbol{i}" for i in range(errors)]
    return "\n".join(noise[:200] + errs + noise[200:])


def _summariser(messages) -> Recap:
    return Recap(
        goal="add Pension resource with CRUD and a status filter",
        plan_step="6 of 8 — wiring FxHandler",
        files_created=("core/domain/pension.go", "repo/postgres/pension.go"),
        files_modified=("handler/request.go", "bootstrap/bootstrapper.go"),
        decisions=("table = pensions (user-confirmed)",),
        verified=("gofmt clean, go build clean at turn 12",),
        open_items=("rules_lint fx-registration — handler present but missing ResultTags",),
        do_not_retry=("hand-editing request_*_validator.go (generated; run govalid_gen)",),
        turns=(1, len(messages)),
    )


def _turn_content(turn: int) -> list[tuple[str, str, str | None]]:
    """One turn's worth of (tool, content, path).

    Deliberately repetitive on paths: a real edit loop reads the same handful of
    files over and over, which is exactly what the slice ledger is for and what
    an unmanaged history accumulates linearly.
    """
    files = ["handler/pension.go", "repo/postgres/pension.go", "core/domain/pension.go"]
    path = files[turn % len(files)]
    out: list[tuple[str, str, str | None]] = [
        ("read_file", _go_file(path.split("/")[-1].removesuffix(".go").title()), path),
    ]
    if turn % 3 == 0:
        out.append(("go_build", _build_log(), None))
    if turn % 4 == 0:
        out.append(("rules_lint", "\n".join(f"  [rule-{i}] {path}:{i} — finding" for i in range(60)), None))
    if turn % 5 == 0:
        out.append(("search_repo", "\n".join(f"{path}:{i}: match" for i in range(300)), None))
    return out


def _run_managed() -> tuple[list[int], list[int], dict]:
    """A 25-turn task through the context manager.

    Returns the per-turn prompt totals, the per-turn novel-token counts, and a
    final snapshot.
    """
    cm = ContextManager(
        mode=Mode.CODER,
        system_prompt=SYSTEM_PROMPT,
        tool_schema_tokens=TOOL_SCHEMA_TOKENS,
    )
    cm.set_task(
        "Add a Pension resource with CRUD and a status filter on List",
        plan="1. domain  2. ddl  3. repo  4. dtos  5. handler  6. fx  7. verify",
        acceptance=["go build ./... clean", "rules_lint clean", "POST /v1/pensions in v3Doc.json"],
    )

    prompts: list[int] = []
    novel: list[int] = []
    previous = None

    cm.begin_turn()
    cm.append_tool_result("repo_map", _repo_map())

    for turn in range(1, TURNS + 1):
        cm.begin_turn()
        cm.append_assistant(
            f"Step {turn}: reading the handler, then applying a minimal patch. " * 6
        )
        for tool, content, path in _turn_content(turn):
            cm.append_tool_result(
                tool, content, path=path, line_range=(1, 400) if path else None
            )

        if cm.should_compact():
            cm.compact(_summariser)

        prompts.append(cm.usage().total)
        novel.append(cm.novel_tokens(previous))
        previous = cm.build()

    return prompts, novel, cm.inspect()


def _run_unmanaged() -> list[int]:
    """The same content with no caps, no ledger and no compaction.

    This is the frontend agent's shape: an append-only message list, tool
    results entering untruncated, repo_map resident from turn one.
    """
    fixed = estimate_tokens(SYSTEM_PROMPT) + TOOL_SCHEMA_TOKENS
    history = estimate_tokens(_repo_map())
    prompts: list[int] = []

    for turn in range(1, TURNS + 1):
        history += estimate_tokens(
            f"Step {turn}: reading the handler, then applying a minimal patch. " * 6
        )
        for _, content, _ in _turn_content(turn):
            history += estimate_tokens(content)
        prompts.append(fixed + history)

    return prompts


@pytest.mark.slow
def test_p95_prompt_tokens_stay_inside_the_target():
    """§5.3: P95 prompt tokens per coder turn <= 24k."""
    prompts, novel, snapshot = _run_managed()
    p95 = statistics.quantiles(prompts, n=20)[-1]

    assert p95 <= P95_PROMPT_TOKENS, (
        f"P95 prompt is {p95:,.0f} tokens, over the {P95_PROMPT_TOKENS:,} target. "
        f"peak={max(prompts):,} compactions={snapshot['compactions']} "
        f"stale_slices={snapshot['stale_slices']}"
    )


@pytest.mark.slow
def test_no_single_turn_exceeds_the_hard_budget():
    """The cap is the cap. A turn over it is a request the endpoint may refuse,
    and it is also the point past which the context-rot literature says accuracy
    is falling anyway."""
    prompts, _, _ = _run_managed()
    budget = ContextManager(mode=Mode.CODER, system_prompt=SYSTEM_PROMPT).budget
    assert max(prompts) <= budget, (
        f"peak prompt {max(prompts):,} exceeds the {budget:,}-token budget"
    )


@pytest.mark.slow
def test_effective_prefill_stays_inside_the_target():
    """§5.3's 180k, measured as it is written: after prefix reuse.

    Prefill is what the GPU actually recomputes. Roughly 95% of the unmanaged
    figure is the same prefix processed again and again, and the novel count is
    what is left once that is not.
    """
    prompts, novel, snapshot = _run_managed()
    effective = sum(novel)

    assert effective <= TOTAL_PREFILL, (
        f"effective prefill is {effective:,} tokens over {TURNS} turns, above the "
        f"{TOTAL_PREFILL:,} target. This is the figure prefix caching cannot help "
        f"with, so it is the design's own cost. raw={sum(prompts):,} "
        f"compactions={snapshot['compactions']}"
    )


@pytest.mark.slow
def test_raw_prefill_stays_inside_the_no_cache_ceiling():
    """The other end of the range: what it costs if §9 Q1 comes back negative.

    Derived rather than quoted, because the plan does not state a no-cache
    figure: at most TURNS turns each at most the P95 target. A design that
    doubled its per-turn cost would still satisfy the novel-token assertion, and
    would fail here.
    """
    prompts, _, snapshot = _run_managed()
    ceiling = TURNS * P95_PROMPT_TOKENS
    total = sum(prompts)

    assert total <= ceiling, (
        f"raw prefill is {total:,} tokens over {TURNS} turns, above the derived "
        f"{ceiling:,} ceiling ({TURNS} x {P95_PROMPT_TOKENS:,}). "
        f"compactions={snapshot['compactions']}"
    )


@pytest.mark.slow
def test_compaction_is_rare_because_each_one_invalidates_a_prefix():
    """Compaction rewrites the middle of the message list, which invalidates
    every cached prefix below it. It buys budget headroom at the cost of a full
    prefill, so it has to be rare.

    The first version of this compaction kept a fixed *number* of recent
    messages rather than a token budget — Part B §10.4's mistake, one layer
    down — and fired sixteen times in twenty-five turns because four capped
    read_file results already exceed the threshold it had just compacted below.
    """
    _, _, snapshot = _run_managed()
    # Currently 8. The bound is the anti-thrashing floor rather than a ratchet:
    # a knife-edge assertion flaps on every content change and gets relaxed by
    # whoever it flaps on, which is how a gate stops being one.
    assert snapshot["compactions"] <= TURNS // 2, (
        f"{snapshot['compactions']} compactions in {TURNS} turns is thrashing; "
        "each one costs a full prefill of everything below the recap"
    )


@pytest.mark.slow
def test_the_managed_run_is_dramatically_cheaper_than_the_unmanaged_one():
    """The comparison that makes the targets mean something.

    Without it, the assertions above are numbers with no denominator — and the
    first person to find them inconvenient will raise them.
    """
    managed, novel, snapshot = _run_managed()
    unmanaged = _run_unmanaged()

    m_total, u_total, n_total = sum(managed), sum(unmanaged), sum(novel)
    ratio = u_total / m_total

    print(
        f"\n  managed:    peak {max(managed):>8,}  raw {m_total:>10,}  novel {n_total:>9,}"
        f"\n  unmanaged:  peak {max(unmanaged):>8,}  raw {u_total:>10,}"
        f"\n  reduction:  {ratio:.1f}x raw, {u_total / n_total:.1f}x with prefix reuse"
        f"\n  compactions={snapshot['compactions']}  stale_slices={snapshot['stale_slices']}"
    )

    assert ratio >= 5.0, (
        f"context management is only saving {ratio:.1f}x; §5.2 puts the unmanaged "
        "cost at roughly 1.25M tokens for this shape of task, so something has "
        "stopped working"
    )


@pytest.mark.slow
def test_growth_is_bounded_rather_than_linear():
    """The property behind the numbers.

    An unmanaged history grows linearly in turns, so turn 25 costs twice turn
    12 and the total is quadratic. A managed one plateaus: compaction and the
    slice ledger bound it by *distinct files touched*, not by turns taken.
    """
    managed, _, _ = _run_managed()
    unmanaged = _run_unmanaged()

    first_half = statistics.mean(managed[: TURNS // 2])
    second_half = statistics.mean(managed[TURNS // 2 :])
    assert second_half <= first_half * 1.6, (
        f"managed context grew {second_half / first_half:.1f}x between the first and "
        "second half of the run; it should plateau, not climb"
    )

    u_first = statistics.mean(unmanaged[: TURNS // 2])
    u_second = statistics.mean(unmanaged[TURNS // 2 :])
    assert u_second > u_first * 1.6, (
        "the unmanaged baseline is not growing, so it is not modelling the problem"
    )
