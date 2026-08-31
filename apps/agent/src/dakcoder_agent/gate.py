"""The verification gate (Part A section 9.3).

Two speeds, and the split between them is the whole point.

The **inner loop** runs after every edit batch and must stay under a second:
format the files just written, then lint them. It exists to catch the mistake
while the model still remembers making it.

The **gate** runs once per plan step and before finishing. It is ordered and
fail-fast, and the order is not arbitrary:

* ``go build`` first, because it is authoritative — nothing downstream means
  anything until it is clean, and diagnostics from a package that does not
  compile are noise.
* ``govalid_gen`` then ``go build`` again, because regenerating a validator can
  itself break the build (a renamed field), and that is the signal we want.
* ``go vet`` near the end, because it was measured at about 32 seconds — more
  than ``go build`` — and nothing after it depends on it.
* ``go mod tidy`` last among the blocking stages, where a diff means the
  dependency set drifted, which is a review decision rather than a fix.

**The gate is a pipeline, not a menu.** None of these stages is offered to the
model as a tool. A model that *chooses* whether to verify is one that sometimes
does not, and "it said it was done and it wasn't" is the failure this whole
design exists to prevent. So the gate does not ask.

**Everything scoped is scoped to touched files.** ``gofmt`` and ``rules_lint``
both take the mutation list. Part A section 9.3 is explicit about why: every file
in the reference template fails ``gofmt -l`` because they all use CRLF, so an
unscoped format touches files the agent never went near — and an unscoped lint
blocks on violations that were there before the session started.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .tools.router import Router

__all__ = ["GateReport", "Stage", "StageResult", "full_gate", "inner_loop"]


@dataclass(frozen=True, slots=True)
class Stage:
    """One step of the gate."""

    name: str
    tool: str
    args: Callable[["GateContext"], dict[str, Any] | None]
    #: A blocking stage that fails stops the gate. A non-blocking one is
    #: reported and the sequence continues — that is how `golangci-lint` can
    #: run at all without taxing every task with style findings.
    blocking: bool = True
    #: Whether this stage applies to this run at all.
    when: Callable[["GateContext"], bool] = lambda _ctx: True
    #: Why it was skipped, when `when` says no.
    skip_reason: str = ""
    #: Whether a failure here makes every later stage meaningless. True only for
    #: compilation: ``go vet`` over code that does not compile is pages of
    #: consequences of the one error the model already has to fix. Nothing else
    #: in the sequence has that relationship to what follows it -- ``go vet``,
    #: ``go test`` and ``go mod tidy`` do not depend on the API document being
    #: complete -- so a failure there stops blocking the run's report on stages
    #: it has nothing to do with.
    halts: bool = False


@dataclass(frozen=True, slots=True)
class GateContext:
    """What the stages need to decide their arguments."""

    router: Router
    touched: tuple[str, ...]
    #: A greenfield scaffold or a dependency change, which turns on govulncheck.
    dependencies_changed: bool = False
    #: Compliance violations that were already present when this run began, as
    #: ``rule|path|message`` keys. Reported by the stages, never blocked on: a
    #: vertical slice has to edit a legacy handler, and the moment it does, that
    #: file's pre-existing violation would otherwise become this run's blocker
    #: and stay one for the life of the run.
    baseline: frozenset[str] = frozenset()

    @property
    def go_files(self) -> tuple[str, ...]:
        return tuple(p for p in self.touched if p.endswith(".go"))

    @property
    def is_go_module(self) -> bool:
        """Whether the workspace root is itself a Go module.

        Every Go stage runs ``./...``, which is a *module*-relative pattern: with
        no ``go.mod`` at the working directory the toolchain answers ``pattern
        ./...: directory prefix . does not contain main module or its selected
        dependencies`` and exits non-zero — which ``_result`` turns into a failure
        and the loop reads as a defect in code that was never compiled here at
        all. Part A §9.3 was written against a workspace that *is* one service; a
        developer who opens the checkout root instead gets a gate no edit can
        ever clear.

        Deliberately not "is there a ``go.mod`` somewhere below". The modules
        under a checkout root are other people's services: building them — or
        worse, tidying them — on a task that never touched them is this module's
        own unscoped-gofmt mistake, one directory up.
        """
        return (self.router.workspace.root / "go.mod").is_file()

    @property
    def has_tests(self) -> bool:
        # Scoped to the module, not to the tree. An rglob from a checkout root
        # finds `*_test.go` inside a service `go test` will never reach from
        # here, which schedules a stage that cannot run.
        return self.is_go_module and any(self.router.workspace.root.rglob("*_test.go"))


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    ok: bool
    blocking: bool
    content: str
    seconds: float = 0.0
    skipped: str = ""

    @property
    def blocked(self) -> bool:
        return self.blocking and not self.ok and not self.skipped


@dataclass(frozen=True, slots=True)
class GateReport:
    """What the gate found, and whether the change is done."""

    results: tuple[StageResult, ...] = ()
    #: Stages that never ran because an earlier blocking stage failed.
    not_run: tuple[str, ...] = ()
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not any(r.blocked for r in self.results) and not self.not_run

    @property
    def blocked_by(self) -> StageResult | None:
        return next((r for r in self.results if r.blocked), None)

    @property
    def warnings(self) -> tuple[StageResult, ...]:
        return tuple(r for r in self.results if not r.ok and not r.blocking and not r.skipped)

    def summary(self) -> str:
        """One line per stage, and the failure in full.

        The failing stage's output is included whole because that is what the
        model has to act on; the passing ones are one line each because their
        content is "clean" and repeating it costs tokens for no information.
        """
        # ASCII marks, not tick and cross. This string is printed by a CLI as
        # well as rendered by the extension, and a Windows console defaults to
        # cp1252 — where U+2713 raises UnicodeEncodeError and takes the run with
        # it. A prettier report that occasionally kills the process is not a
        # better report.
        lines = []
        for result in self.results:
            if result.skipped:
                mark, note = "-", f" ({result.skipped})"
            elif result.ok:
                mark, note = "ok", ""
            elif result.blocking:
                mark, note = "FAIL", ""
            else:
                mark, note = "warn", " (advisory)"
            lines.append(f"  {mark} {result.name}{note}")
        for name in self.not_run:
            lines.append(f"  · {name} (not reached)")

        header = "gate: PASS" if self.ok else "gate: FAIL"
        body = "\n".join(lines)

        advisory = "\n\n".join(f"{w.name}:\n{w.content}" for w in self.warnings)

        failure = self.blocked_by
        if failure is not None:
            # The advisory output goes in here too.
            #
            # It used to be dropped whenever anything blocked, and that is how a
            # `govalid_gen` failure survived a whole session unexamined: it is
            # non-blocking, so it never became the failure, and something else
            # always was — so its output never reached the model once. It showed
            # in the panel as a red mark with nothing behind it, on every attempt
            # of every run. A stage that fails silently for the life of a session
            # is worse than one that does not run.
            out = f"{header}\n{body}\n\n{failure.name} failed:\n{failure.content}"
            if advisory:
                out += (
                    "\n\nAlso failing, not blocking — fix these only if they bear on "
                    f"the above:\n\n{advisory}"
                )
            return out
        if advisory:
            return f"{header}\n{body}\n\n{advisory}"
        return f"{header}\n{body}"

    #: How much of a failing stage's output reaches the client. Enough for the
    #: three compiler errors that explain a `go_build` failure; short of pasting
    #: a whole `go vet` run into a sidebar.
    CONTENT_LIMIT = 4000

    def as_dict(self) -> dict[str, Any]:
        """Serialised for the client.

        ``content`` is carried **only for stages that did not pass**. Dropping it
        entirely was the original choice, and it left the panel able to say
        *which* stage blocked and never *why* — the errors went to the model as a
        tool message and nowhere else. Carrying it for passing stages too would
        put a clean thirteen-stage run's whole output on the wire every gate, for
        nothing anyone reads.
        """
        return {
            "ok": self.ok,
            "seconds": round(self.seconds, 2),
            "stages": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "blocking": r.blocking,
                    "skipped": r.skipped,
                    "seconds": round(r.seconds, 2),
                    **(
                        {
                            "content": r.content[: self.CONTENT_LIMIT],
                            "truncated": len(r.content) > self.CONTENT_LIMIT,
                        }
                        if r.content and not r.ok and not r.skipped
                        else {}
                    ),
                }
                for r in self.results
            ],
            "not_run": list(self.not_run),
            "blocked_by": self.blocked_by.name if self.blocked_by else "",
        }


# ── the two sequences ───────────────────────────────────────────────────────


def _scoped(ctx: GateContext) -> dict[str, Any] | None:
    files = ctx.go_files
    return {"paths": ",".join(files)} if files else None


def _scoped_with_baseline(ctx: GateContext) -> dict[str, Any] | None:
    """Scoped like every other lint stage, plus what was already broken.

    Separated from ``_scoped`` rather than folded into it because the other
    stages have no use for a baseline: ``gofmt`` rewrites what it is given, and
    ``rules_lint`` is advisory here and already discounts out-of-scope findings
    itself. Only the blocking compliance stage needs to tell "this run broke it"
    from "it was broken when we arrived".
    """
    args = _scoped(ctx)
    if args is None:
        return None
    if ctx.baseline:
        args["baseline"] = sorted(ctx.baseline)
    return args


#: Why every Go stage sits out a workspace that is not itself a module, stated
#: as the environment fact it is — and saying what to do about it, because a
#: skip reason the developer cannot act on costs the same turn a failure does.
_NO_MODULE = "workspace root has no go.mod; open the service directory to gate it"


def _has_go(ctx: GateContext) -> bool:
    return ctx.is_go_module


INNER: tuple[Stage, ...] = (
    # An auto-fix, not a check. The model's formatting misses are trivial and
    # mechanical — a missing trailing newline, one struct-tag column — and
    # spending a turn asking it to fix its own whitespace is pure waste.
    Stage("gofmt", "gofmt", _scoped, blocking=False),
    Stage(
        "go_diagnostics",
        "go_diagnostics",
        lambda ctx: {},
        blocking=False,
        when=lambda ctx: "go_diagnostics" in ctx.router.handlers,
        skip_reason="gopls not wired",
    ),
    Stage("rules_lint", "rules_lint", _scoped, blocking=False),
)

# Every Go stage is guarded on `_has_go`, not just the first. Guarding only
# `go_build` would promote `swagger_check` to first blocker and replay the same
# unclearable ladder under a different stage name.
GATE: tuple[Stage, ...] = (
    Stage(
        "go_build",
        "go_build",
        lambda ctx: {},
        when=_has_go,
        skip_reason=_NO_MODULE,
        halts=True,
    ),
    Stage(
        "govalid_gen",
        "govalid_gen",
        lambda ctx: {},
        blocking=False,
        when=_has_go,
        skip_reason=_NO_MODULE,
    ),
    # Again, because regenerating a validator against a renamed field breaks the
    # build — and that break is the signal, not an accident.
    Stage(
        "go_build (after generate)",
        "go_build",
        lambda ctx: {},
        when=_has_go,
        skip_reason=_NO_MODULE,
        halts=True,
    ),
    Stage("rules_lint", "rules_lint", _scoped, when=_has_go, skip_reason=_NO_MODULE),
    # Scoped, like the other lint stages. Unscoped it reported every legacy
    # handler in the service as a blocker, so no change to a service that
    # predates the contract could ever clear the gate — the exact failure the
    # `_scoped` comment at the top of this file was written to prevent, in the
    # one stage that did not use it.
    Stage(
        "swagger_check",
        "swagger_check",
        _scoped_with_baseline,
        when=_has_go,
        skip_reason=_NO_MODULE,
    ),
    Stage("go_vet", "go_vet", lambda ctx: {}, when=_has_go, skip_reason=_NO_MODULE),
    Stage(
        "go_test",
        "go_test",
        lambda ctx: {},
        # `has_tests` already implies `is_go_module`, so this needs no _has_go.
        when=lambda ctx: ctx.has_tests,
        skip_reason="no test files",
    ),
    Stage(
        "go_mod tidy",
        "go_mod",
        lambda ctx: {"op": "tidy"},
        when=_has_go,
        skip_reason=_NO_MODULE,
    ),
    Stage(
        "golangci_lint",
        "golangci_lint",
        lambda ctx: {},
        blocking=False,
        when=_has_go,
        skip_reason=_NO_MODULE,
    ),
    Stage(
        "govulncheck",
        "govulncheck",
        lambda ctx: {},
        blocking=False,
        when=lambda ctx: ctx.is_go_module and ctx.dependencies_changed,
        skip_reason="no dependency change this run",
    ),
)


def inner_loop(router: Router, touched: Sequence[str]) -> GateReport:
    """The sub-second check after an edit batch.

    Nothing here blocks. Its job is to put the compiler's opinion in front of the
    model while the edit is still the thing it is thinking about — findings are
    information for the next turn, not a verdict on this one.
    """
    return _run(GateContext(router, tuple(touched)), INNER)


def full_gate(
    router: Router,
    touched: Sequence[str],
    *,
    dependencies_changed: bool = False,
    baseline: frozenset[str] = frozenset(),
) -> GateReport:
    """The gate: ordered, fail-fast, authoritative."""
    return _run(
        GateContext(router, tuple(touched), dependencies_changed, baseline),
        GATE,
    )


def _run(ctx: GateContext, stages: Sequence[Stage]) -> GateReport:
    results: list[StageResult] = []
    started = time.monotonic()

    for index, stage in enumerate(stages):
        if not stage.when(ctx):
            results.append(
                StageResult(stage.name, True, stage.blocking, "", skipped=stage.skip_reason)
            )
            continue

        args = stage.args(ctx)
        if args is None:
            results.append(
                StageResult(stage.name, True, stage.blocking, "", skipped="nothing in scope")
            )
            continue

        stage_started = time.monotonic()
        outcome = ctx.router.run_gate_tool(stage.tool, args)
        elapsed = time.monotonic() - stage_started

        result = outcome if isinstance(outcome, ToolResult) else ToolResult.failure(str(outcome))
        ok = _stage_passed(stage, result)
        results.append(
            StageResult(stage.name, ok, stage.blocking, result.for_model(), elapsed)
        )

        if stage.blocking and not ok and stage.halts:
            # Fail-fast, but only where "fast" is also "correct". Running
            # `go vet` over code that does not compile produces pages of errors
            # that are all consequences of the one the model already has to fix.
            return GateReport(
                tuple(results),
                tuple(s.name for s in stages[index + 1 :]),
                time.monotonic() - started,
            )

        # Everything else that fails is recorded and the sequence carries on.
        #
        # The report is still a failure -- `ok` reads the blocked results, not
        # this loop -- but the developer gets the whole picture rather than one
        # stage and five dashes. The field transcript ends "blocked at
        # swagger_check" with go_vet, go_test, go mod tidy, golangci_lint and
        # govulncheck all "not run", none of which has any dependency on the API
        # document being complete. Two of those five would have said something
        # useful about the change that was actually made, and the run finished
        # `unverified` without ever asking them.

    return GateReport(tuple(results), (), time.monotonic() - started)


def _stage_passed(stage: Stage, result: ToolResult) -> bool:
    """Whether a stage counts as passing.

    Two stages need more than ``result.ok``, and both for the same reason: the
    tool succeeded, and what it *reported* is the failure.

    ``rules_lint`` returns ok=True with a body describing violations — a finding
    is the tool working, not the tool breaking. ``go mod tidy`` returns ok=True
    having changed ``go.mod``, and at the gate a change is exactly what must not
    happen (Part A section 9.3: tidy must be a no-op).
    """
    if not result.ok:
        return False

    if stage.tool == "rules_lint":
        return _lint_is_clean(result)

    if stage.tool == "go_mod":
        return not result.meta.get("changed", False)

    return True


def _lint_is_clean(result: ToolResult) -> bool:
    """Blocking violations only.

    ``out_of_scope_count`` is pre-existing damage in files the agent never
    touched. Blocking on it would make the first change to any legacy service
    impossible, which is precisely the codebase this agent exists to help with.
    """
    import json

    try:
        payload = json.loads(result.content)
    except (json.JSONDecodeError, TypeError):
        # Not the structured form; fall back to the tool's own verdict rather
        # than guessing from the text.
        return result.ok
    return bool(payload.get("ok", True)) and int(payload.get("count", 0)) == 0
