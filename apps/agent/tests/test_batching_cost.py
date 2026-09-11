"""What a turn costs, and therefore what one call per turn costs.

Measured on a real run (`error.md`, 74 turns) before any of this was changed:

    prompt tokens        7,597,219
    completion tokens       36,891   ->  206 : 1
    turns with one call         71   (of 74; none made more than one)
    mean prompt per turn   102,665   (3,156 on turn 1, 158,136 by turn 66)
    file edits                  11   costing 1,691,433 prompt tokens
                                     -- about 154,000 per edit

The ratio is the finding. A reply re-sends the whole conversation, so the prompt
is what a turn costs and the completion is rounding. A one-line `patch_file` and
a six-file batch cost the *same* prompt: granularity does not add a little cost,
it multiplies the only expensive thing there is.

Nothing in the loop ever forbade batching -- `MAX_CALLS_PER_BATCH` has been six
throughout. What forbade it was the acting overlay's opening line, "One step at
a time", with nothing anywhere saying a reply may carry more than one call. The
model obeyed: 71 turns, 71 single calls, zero exceptions.

These tests hold the two halves of the fix: the mechanism works (a batch of
edits lands, and each closes its own plan step), and the instruction that told
the model not to use it is gone.
"""

from __future__ import annotations

import json
from pathlib import Path

from dakcoder_shared.paths import Workspace
from dakcoder_shared.tokens import estimate_tokens

from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.prompts import mode_instruction, system_prompt
from dakcoder_agent.tools.control import PlanStep
from dakcoder_agent.tools.router import Router
from scripted import ScriptedClient, build, calls, say  # noqa: E402
from scripted import gated, planning_router, written  # noqa: F401,E402

FILES = ("a.go", "b.go", "c.go")


class _Metered(ScriptedClient):
    """The scripted model, weighing every prompt it is sent.

    The endpoint's own `prompt_tokens` is the truth, and there is no endpoint
    here -- so this estimates from the assembled messages with the same
    estimator the loop budgets against. It is the right instrument for the
    question anyway: what is being compared is how many times a prompt of
    roughly this size is sent, not what any one of them weighs to the byte.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.prompts: list[int] = []

    def chat(self, messages, *, response_format=None, **kwargs):
        if response_format is None:
            self.prompts.append(estimate_tokens(json.dumps(messages)))
        return super().chat(messages, response_format=response_format, **kwargs)

    @property
    def prompt_tokens(self) -> int:
        return sum(self.prompts)


def _workspace(tmp_path: Path) -> Workspace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        (tmp_path / name).write_text(
            f"package {name[0]}\n\nfunc Old{name[0].upper()}() {{}}\n", encoding="utf-8"
        )
    return Workspace.at(tmp_path)


def _patch(name: str):
    return (
        "patch_file",
        json.dumps(
            {
                "path": name,
                "old": f"func Old{name[0].upper()}()",
                "new": f"func New{name[0].upper()}()",
            }
        ),
    )


def _run(router: Router, turns, tmp_path: Path) -> _Metered:
    loop, _ = build(router, turns, max_turns=12)
    loop.client = _Metered(turns, kind="change")
    # The acting phase, reached the way a continued run reaches it: a plan with
    # open steps. `intent=AGENT` on a first message opens in PLANNER, which
    # holds no write tool.
    loop.state.plan = tuple(PlanStep(name, "rename", "builds") for name in FILES)
    loop.state.mode = Mode.AGENT
    list(loop.run("rename them", intent=Intent.AGENT, continued=True))
    return loop.client


def test_a_batch_of_edits_all_land_and_each_closes_its_own_step(tmp_path: Path) -> None:
    """The mechanism, before the instruction that asks for it.

    Nothing had ever exercised several mutating calls in one reply. They are not
    `parallel`-safe, so they run in sequence inside the one batch -- which is
    what is wanted: one prompt, three writes, one inner gate.
    """
    router = Router(_workspace(tmp_path))
    from dakcoder_agent.tools import control, fs

    router.handlers.update({**fs.HANDLERS, **control.HANDLERS})

    loop, _ = build(router, [calls(*[_patch(n) for n in FILES]), say("done")], max_turns=6)
    loop.state.plan = tuple(PlanStep(name, "rename", "builds") for name in FILES)
    events = list(loop.run("rename them", intent=Intent.AGENT, continued=True))

    results = [e for e in events if e.type == "tool_result"]
    assert [e.data.get("ok") for e in results][:3] == [True, True, True]
    assert list(loop.router.touched) == list(FILES)
    for name in FILES:
        assert f"func New{name[0].upper()}()" in (tmp_path / name).read_text(encoding="utf-8")

    # Each step closed on its own file, rather than the batch closing one step
    # or all of them at once.
    assert [s.status for s in loop.state.plan] == ["done", "done", "done"]


def test_batching_costs_one_prompt_where_serial_costs_three(tmp_path: Path) -> None:
    """The before-and-after, on the same work.

    Three edits in one reply against three edits in three replies. The edits are
    identical and the workspace is identical; the only difference is how many
    times the conversation is sent.
    """
    from dakcoder_agent.tools import control, fs

    handlers = {**fs.HANDLERS, **control.HANDLERS}

    batched = _run(
        Router(_workspace(tmp_path / "batched"), dict(handlers)),
        [calls(*[_patch(n) for n in FILES]), say("done")],
        tmp_path / "batched",
    )
    serial = _run(
        Router(_workspace(tmp_path / "serial"), dict(handlers)),
        [calls(_patch("a.go")), calls(_patch("b.go")), calls(_patch("c.go")), say("done")],
        tmp_path / "serial",
    )

    assert len(batched.prompts) < len(serial.prompts), (
        f"batched sent {len(batched.prompts)} prompts, serial {len(serial.prompts)}"
    )
    assert batched.prompt_tokens < serial.prompt_tokens, (
        f"batched {batched.prompt_tokens:,} tokens, serial {serial.prompt_tokens:,}"
    )
    # Not a micro-saving. On this tiny workspace the prompt is small and the
    # gap is modest; on the measured field run the mean prompt was 102,665
    # tokens, so each turn removed is about that much.
    saved = serial.prompt_tokens - batched.prompt_tokens
    assert saved > 0
    print(
        f"\nbatched: {len(batched.prompts)} prompt(s), {batched.prompt_tokens:,} tokens"
        f"\nserial:  {len(serial.prompts)} prompt(s), {serial.prompt_tokens:,} tokens"
        f"\nsaved:   {saved:,} tokens ({saved * 100 // serial.prompt_tokens}%)"
    )


def test_a_batch_asks_for_each_approval_and_honours_each_answer(tmp_path: Path) -> None:
    """The risk that would have bitten, had the instruction gone in untested.

    Every write is approvable and a batch of six is six decisions, taken inside
    one dispatch loop. The failure to be sure about is not "the developer is
    asked too often" -- it is a rejection landing on the wrong call, or a batch
    that abandons the rest of its work after the first refusal.

    Neither happens: the protected paths raise one request each, in order, and a
    refusal stops that call and nothing else.
    """
    from dakcoder_agent.tools import control, fs

    def trial(decide) -> tuple[int, list[bool], list[str]]:
        root = tmp_path / ("yes" if decide(None) else "no")
        (root / "bootstrap").mkdir(parents=True)
        (root / "bootstrap" / "bootstrapper.go").write_text(
            "package bootstrap\n\nvar Old = 1\n", encoding="utf-8"
        )
        (root / "main.go").write_text("package main\n\nfunc Old() {}\n", encoding="utf-8")
        (root / "plain.go").write_text("package plain\n\nfunc Old() {}\n", encoding="utf-8")
        router = Router(Workspace.at(root), {**fs.HANDLERS, **control.HANDLERS})

        paths = ("bootstrap/bootstrapper.go", "main.go", "plain.go")
        batch = calls(
            (
                "patch_file",
                json.dumps(
                    {"path": "bootstrap/bootstrapper.go", "old": "var Old = 1", "new": "var New = 1"}
                ),
            ),
            ("patch_file", json.dumps({"path": "main.go", "old": "func Old()", "new": "func New()"})),
            ("patch_file", json.dumps({"path": "plain.go", "old": "func Old()", "new": "func New()"})),
        )
        loop, _ = build(router, [batch, say("done")], max_turns=6, approve=decide)
        loop.state.plan = tuple(PlanStep(p, "rename", "builds") for p in paths)
        events = list(loop.run("rename", intent=Intent.AGENT, continued=True))
        pending = [e for e in events if e.type == "tool_pending"]
        ok = [e.data.get("ok") for e in events if e.type == "tool_result"][:3]
        return len(pending), ok, list(loop.router.touched)

    raised, ok, touched = trial(lambda _r: True)
    # `bootstrap/**` and `main.go` are protected; `plain.go` is not.
    assert raised == 2, f"{raised} approval(s) raised, want one per protected path"
    assert ok == [True, True, True]
    assert touched == ["bootstrap/bootstrapper.go", "main.go", "plain.go"]

    raised, ok, touched = trial(lambda _r: False)
    assert raised == 2
    assert ok == [False, False, True], "a refusal took the unprotected write with it"
    assert touched == ["plain.go"], "the rest of the batch did not run"


def test_nothing_tells_the_model_to_work_one_call_at_a_time() -> None:
    """The line that produced 71 single-call turns out of 71.

    Asserted against the assembled prompt rather than the file, because that is
    what the model is sent -- and the overlay and the system prompt are
    assembled separately.
    """
    acting = mode_instruction(Mode.AGENT)
    assert "One step at a time" not in acting
    assert "in one reply" in acting, "the overlay must say batching is wanted"


def test_the_system_prompt_says_a_reply_may_carry_several_calls() -> None:
    """In the shared prompt, not the overlay: it is true in every phase, and the
    overlays have three tokens of headroom between them."""
    prompt = system_prompt()
    assert "several calls" in prompt
    assert "six calls in one reply cost what one costs" in prompt
    # And the reason the reads were whole-file: the rule was there, the cost was
    # not. 33 of 34 reads in the measured run passed only `path`.
    assert "re-sent on every turn after it" in prompt
