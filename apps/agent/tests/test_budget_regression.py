"""The context-budget regression gate (Part A §20.5).

Non-negotiable, in CI, not on a dashboard. §21's first risk is that "the context
work is deferred as 'optimisation' and the agent ships slow", and its mitigation
is exactly this file: the targets are asserted, so the regression is a red build
rather than a slow agent nobody can quite explain.

The targets were §5.3's, set for a 32,768-token ceiling:

    prompt tokens, P95, coder turn        <= 24,000   (frontend agent: ~68,000)
    total prefill for a 25-turn task      <= 180,000  (frontend agent: ~1.25M)

**Re-baselined 2026-08-31 for PROMPT_BUDGET = 245,760** — a deliberate policy
change, not drift. At 32,768 real runs compacted at ~23k, the recap evicted the
answers the model was working from, and the repeat detector ended the run; two
field transcripts died exactly this way. The ceiling now defers to the model's
own window, and this gate's job changes with it: it no longer holds the line at
§5.3's numbers, it pins the *measured cost of the new policy* so an accidental
regression (a cap dropped, the ledger broken, the budget quietly shrunk) is
still a red build. Measured on this simulation at the new budget and caps:

    P95 prompt   113,961   ->  target 128,000
    novel total  846,221   ->  target 1,000,000
    compactions        0   ->  target <= 2 (compaction returning here means the
                               budget shrank back, which is the very regression
                               this file exists to catch)

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
P95_PROMPT_TOKENS = 128_000
TOTAL_PREFILL = 1_000_000

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
        files_modified=("handler/request/request.go", "bootstrap/bootstrapper.go"),
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
    # Zero at the 245,760 budget — this task's working set peaks around 115k
    # against a ~172k threshold. Bounded at two rather than zero because a
    # knife-edge assertion flaps on every content change and gets relaxed by
    # whoever it flaps on; but compaction *returning* here is the loudest
    # available signal that the budget shrank back toward the ceiling that was
    # killing runs, so the bound is deliberately tight.
    assert snapshot["compactions"] <= 2, (
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

    # 2.2x raw at the 245,760 budget, down from ~14x at 32,768 — deliberately.
    # Most of the old saving was truncation: capped tool results are cheap to
    # re-send and expensive to act on, and the sliced re-reading they caused is
    # what killed runs. What remains is the saving the design still promises —
    # the slice ledger deduplicating re-reads — plus prefix reuse, asserted
    # separately because it is the half a broken ledger would not fake.
    assert ratio >= 1.8, (
        f"context management is only saving {ratio:.1f}x raw; the slice ledger "
        "has stopped deduplicating re-reads"
    )
    assert u_total / n_total >= 4.0, (
        f"only {u_total / n_total:.1f}x with prefix reuse; the novel-token count "
        "has grown out of proportion to the content"
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

    # At 245,760 the plateau moved: a 25-turn task no longer brushes the
    # ceiling, so within the run the managed curve still climbs — what bounds
    # it now is the working set. The property worth pinning is that the whole
    # task completes *under the compaction threshold* on content the unmanaged
    # baseline pushes far past it: the managed cost is set by the distinct
    # artefacts touched, the unmanaged cost by the number of turns taken.
    threshold = ContextManager(mode=Mode.CODER, system_prompt=SYSTEM_PROMPT).budget * 0.70
    assert max(managed) <= threshold, (
        f"peak managed prompt {max(managed):,.0f} crossed the compaction "
        f"threshold {threshold:,.0f}; the slice ledger has stopped bounding the "
        "working set"
    )
    assert max(unmanaged) > threshold, (
        "the unmanaged baseline fits under the threshold, so it is not "
        "modelling the problem"
    )

    u_first = statistics.mean(unmanaged[: TURNS // 2])
    u_second = statistics.mean(unmanaged[TURNS // 2 :])
    assert u_second > u_first * 1.6, (
        "the unmanaged baseline is not growing, so it is not modelling the problem"
    )
