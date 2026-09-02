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

import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from dakcoder_shared.envelope import ToolResult

from .tools.router import Router

__all__ = [
    "Baseline",
    "GateReport",
    "Stage",
    "StageResult",
    "full_gate",
    "inner_loop",
    "take_baseline",
]


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
    #: Which entry of the run-start baseline excuses a failure here.
    #:
    #: A blocking stage that fails only in ways it was *already* failing before
    #: this run touched anything is reported and does not block. This is the
    #: whole of §3: `go_vet` failed on a tab character in a struct tag that
    #: predates the session, `go_test` failed because the suite needs a Docker
    #: Postgres, `go build` can be red on arrival — and every one of those was
    #: charged to the run, which then spent two Coder attempts and three Debugger
    #: cycles trying to fix a machine.
    #:
    #: Empty means "no baseline applies", which is right for stages that are
    #: already scoped to the files this run touched.
    baseline_key: str = ""
    #: Whether this stage may block on this run, given the environment. A stage
    #: that cannot honestly run here is reported as advisory with this reason
    #: rather than failing the change. Returning ``""`` keeps it blocking.
    advisory_when: Callable[["GateContext"], str] = lambda _ctx: ""


@dataclass(frozen=True, slots=True)
class Baseline:
    """What was already broken before this run touched anything.

    The single most consequential thing missing from the gate. Four blocking
    stages ran unscoped over the whole module with no baseline, so on any
    repository that was not already green — which is every legacy service this
    agent exists to help with — a run was failed for damage it did not cause,
    told the failure was its own, and spent its whole escalation budget trying
    to fix a machine. §3 measures the result: 100% of coding tasks on the legacy
    corpus ended `unverified`, `no_progress`, or in the ladder.

    Findings are held as *stable keys*, not as output. A line number moves the
    moment anything above it is edited, so a key that contains one matches
    nothing on the second look; ``_finding_keys`` strips them.

    ``ok`` is kept alongside the keys because a stage can fail with output this
    cannot key at all — a timeout, a toolchain that is not installed. In that
    case "it was already failing" is still the honest reading, and the whole
    stage is excused rather than none of it.
    """

    #: stage tool -> the finding keys present before the run started.
    findings: dict[str, frozenset[str]] = field(default_factory=dict)
    #: stage tool -> whether it passed before the run started.
    passed: dict[str, bool] = field(default_factory=dict)
    #: stage tool -> the *rule classes* already violated anywhere in the module.
    #:
    #: The coarser half of the judgement, and on a legacy service the half that
    #: carries it. A vertical slice writes a *new* file, so every finding in it
    #: is a new key by construction and key comparison alone excuses nothing --
    #: which is how `rules_lint` came to block every run on a service whose
    #: thirty existing handlers trip `domain-tags` and whose new one, written to
    #: mirror them, trips it too. Mirroring the house style is what the agent is
    #: told to do when the contract is silent; it is not a regression this change
    #: introduced.
    #:
    #: So a finding blocks only when its *rule* is one nothing in the service was
    #: violating before. That is the thing this run actually did.
    rule_classes: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Compliance violations already present, as ``rule|path|message`` keys.
    #: Passed *into* `swagger_check` rather than compared after it, because that
    #: stage does the discounting itself.
    compliance: frozenset[str] = frozenset()
    #: True once the baseline has actually been taken. A gate that runs before
    #: it is ready must not read an empty baseline as "nothing was broken".
    taken: bool = False

    def excuses(self, tool: str, current: frozenset[str]) -> bool:
        """Whether every finding in ``current`` is one this run is not answerable for.

        Two tests, and the second is what makes this usable on a legacy service.

        A finding is excused when its **key** was already present -- the same
        rule, in the same file, with the same message, before the run touched
        anything. That covers an edit to a file that was already violating.

        It is also excused when its **rule class** was already being violated
        somewhere in the module. That covers the case key comparison cannot: a
        vertical slice writes a new file, so its findings are new keys however
        faithfully it copied the file next door. Blocking there fails the agent
        for doing what the system prompt tells it to do -- "when the contract is
        silent, copy the shape of the nearest existing resource".

        What still blocks is a rule *nothing* in the service was violating and
        this change now does. That is a regression, and it is the only thing here
        that is.
        """
        if not self.taken:
            return False
        if self.passed.get(tool, True):
            return False
        known = self.findings.get(tool)
        if known is None:
            # Failing before, with nothing keyable. Excused whole.
            return True
        if not current:
            # Failing before, and we cannot key what it is failing on now.
            # Treated as the same failure: the alternative is charging this run
            # for a stage that was red when it arrived.
            return True

        introduced = current - known
        if not introduced:
            return True

        classes = self.rule_classes.get(tool)
        if not classes:
            return False
        return all(key.split("|", 1)[0] in classes for key in introduced)


@dataclass(frozen=True, slots=True)
class GateContext:
    """What the stages need to decide their arguments."""

    router: Router
    touched: tuple[str, ...]
    #: A greenfield scaffold or a dependency change, which turns on govulncheck.
    dependencies_changed: bool = False
    #: What was already broken when this run began. Reported by the stages,
    #: never blocked on: a vertical slice has to edit a legacy handler, and the
    #: moment it does, that file's pre-existing violation would otherwise become
    #: this run's blocker and stay one for the life of the run.
    baseline: Baseline = field(default_factory=Baseline)

    @property
    def touched_packages(self) -> tuple[str, ...]:
        """``./dir/...`` for every directory this run changed a Go file in.

        What `go test` and `go vet` should be asked about. Unscoped, `go test
        ./...` on the legacy corpus takes 67 seconds and fails because the suite
        wants a Docker Postgres — a verdict about the machine, delivered as a
        verdict about the change.
        """
        dirs = sorted({str(PurePosixPath(p).parent) for p in self.go_files})
        return tuple(f"./{d}/..." if d not in (".", "") else "./..." for d in dirs)

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
    #: The stage's findings as baseline-comparable keys, when it produced any.
    #: Carried on the result so a caller can ask "is this new?" without
    #: re-deriving it from prose -- which is what the inner loop needs, and what
    #: `_stage_findings` already computes for the gate.
    findings: frozenset[str] = frozenset()

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
    stages have no use for a baseline of *this* shape: ``gofmt`` rewrites what
    it is given, and ``rules_lint`` already discounts out-of-scope findings
    itself. Only the compliance stage needs the violation keys passed in, and it
    does the discounting on the far side.
    """
    args = _scoped(ctx)
    if args is None:
        return None
    if ctx.baseline.compliance:
        args["baseline"] = sorted(ctx.baseline.compliance)
    return args


def _packages(ctx: GateContext) -> dict[str, Any] | None:
    """Scope a package-level stage to the directories this run changed.

    ``None`` when nothing Go changed, which records the stage as "nothing in
    scope" rather than running it over a module the run never opened.
    """
    packages = ctx.touched_packages
    return {"pattern": " ".join(packages)} if packages else None


def _tests_can_run(ctx: GateContext) -> str:
    """Why ``go test`` must not block this run, or ``""`` if it may.

    The single most expensive line in the report. `go_test` was blocking and
    unscoped; the legacy corpus's suite stands up Postgres through
    testcontainers, which needs a Docker daemon. On a machine without one the
    stage fails after 67 seconds, every time, whatever the change was — so **no
    change to the loop, the prompts or the model could produce a passing run on
    that corpus**, and the escalation ladder then spent five slots trying to fix
    a test suite that cannot execute here.

    A test that cannot run has not failed. It has not been asked. So the stage
    is downgraded to advisory and says which of the two it is, rather than
    reporting an environment fact as a defect in the diff.

    Probing Docker is deliberately cheap and deliberately last: `shutil.which`
    only, no daemon round trip. A `docker` binary with a dead daemon still fails
    the tests, and that failure is now advisory anyway; what this must never do
    is add seconds to a gate to answer a question about the machine.
    """
    if not _needs_containers(ctx):
        return ""
    if _container_runtime() is not None:
        return ""
    return (
        "these tests start containers (testcontainers) and no container runtime "
        "is available here, so a failure is about this machine, not the change"
    )


#: Import paths that mean a test package needs a container runtime to run.
_CONTAINER_IMPORTS = ("testcontainers", "dockertest", "ory/dockertest")


def _needs_containers(ctx: GateContext) -> bool:
    """Whether any test file in scope stands up a container.

    Read off the imports rather than assumed, so a service whose tests are pure
    unit tests keeps a blocking `go test` — which is the behaviour anyone would
    want and the behaviour the unscoped version could never deliver.
    """
    root = ctx.router.workspace.root
    for package in ctx.touched_packages:
        directory = root / package.removeprefix("./").removesuffix("/...")
        if not directory.is_dir():
            continue
        for test in directory.rglob("*_test.go"):
            try:
                body = test.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(marker in body for marker in _CONTAINER_IMPORTS):
                return True
    return False


#: Findings a failing stage reported, as keys that survive an edit above them.
#:
#: ``go build`` and ``go vet`` both write ``path:line[:col]: message``. The line
#: number is the part that moves — insert a function and every finding below it
#: shifts — so a baseline keyed on the whole line matches nothing the second
#: time it is consulted. Path and message together are stable and specific
#: enough: two identical messages in one file are the same finding for this
#: purpose.
def _finding_keys(content: str) -> frozenset[str]:
    keys: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "go: ")):
            continue
        parts = stripped.split(":")
        if len(parts) >= 3 and parts[1].strip().isdigit():
            path = parts[0].strip()
            # Drop the line, and the column when there is one.
            rest = parts[3:] if len(parts) >= 4 and parts[2].strip().isdigit() else parts[2:]
            keys.add(f"{path}|{':'.join(rest).strip()}")
        else:
            keys.add(stripped)
    return frozenset(keys)


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
        # Unscoped on purpose — a change in one package breaks another, and that
        # is exactly what compilation is for — but baselined, so a module that
        # was already red on arrival is not this run's failure. Without this a
        # workspace that does not build is a workspace where no task can ever
        # finish, and the run is told the breakage is its own.
        baseline_key="go_build",
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
        baseline_key="go_build",
    ),
    # Baselined, like every other blocking stage, and for the reason the others
    # are: without one it reports whatever it finds and the loop reads every
    # finding as the run's own.
    #
    # This stage could not fail at all until `_lint_is_clean` was fixed to read
    # the counts out of `meta` -- it was parsing a body that has been rendered
    # prose since `_render_lint` landed, so the decode raised on every call and
    # the check fell back to `result.ok`, which is True whenever the sidecar ran.
    # Fixing that without also baselining it turned "the headline promise is
    # inert" into "no legacy service can ever clear the gate": one field run
    # wrote nine correct files and was blocked by 98 findings, most of them in
    # `handler/paogen.go`, which it never opened.
    Stage(
        "rules_lint",
        "rules_lint",
        _scoped,
        when=_has_go,
        skip_reason=_NO_MODULE,
        baseline_key="rules_lint",
    ),
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
    # Scoped to the packages this run changed, and baselined against what vet
    # already said before it started. Unscoped and unbaselined, this stage
    # failed every run on the legacy corpus for a tab character in a struct tag
    # that predates the session.
    Stage(
        "go_vet",
        "go_vet",
        _packages,
        when=_has_go,
        skip_reason=_NO_MODULE,
        baseline_key="go_vet",
    ),
    Stage(
        "go_test",
        "go_test",
        _packages,
        # `has_tests` already implies `is_go_module`, so this needs no _has_go.
        when=lambda ctx: ctx.has_tests,
        skip_reason="no test files",
        baseline_key="go_test",
        advisory_when=_tests_can_run,
    ),
    Stage(
        "go_mod tidy",
        "go_mod",
        # `check`: report the drift, put go.mod back. A gate that edits the
        # repository to find out whether the repository needs editing is not a
        # gate. See `commands.go_mod`.
        lambda ctx: {"op": "tidy", "check": "true"},
        when=_has_go,
        skip_reason=_NO_MODULE,
        baseline_key="go_mod",
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
    baseline: Baseline | None = None,
) -> GateReport:
    """The gate: ordered, fail-fast, authoritative — about *this run's* work.

    "Authoritative" was doing damage without the baseline. Four blocking stages
    ran over the whole module and reported whatever they found; the loop read
    every finding as the run's own, and on a repository that was not already
    green no change could ever clear it.
    """
    return _run(
        GateContext(router, tuple(touched), dependencies_changed, baseline or Baseline()),
        GATE,
    )


#: The stages worth measuring before the run starts, and the arguments to
#: measure them with. Unscoped deliberately: the baseline has to cover whatever
#: the run later touches, and it does not know yet what that will be.
_BASELINE_STAGES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("go_build", {}),
    ("go_vet", {}),
    ("go_mod", {"op": "tidy", "check": "true"}),
    # Unscoped deliberately, and it is the one stage where that matters most:
    # the whole point is to learn which rules this service already violates
    # *anywhere*, so a new file written in the house style is not charged for
    # a pattern the house has used thirty times.
    ("rules_lint", {}),
)

#: ``go_test`` is measured separately, and off by default.
#:
#: It is 74 of the baseline's 80 seconds on a legacy service, and the first gate
#: blocks on the baseline finishing. Which buys what, exactly? A baseline for a
#: stage that is *already* advisory whenever its tests need a container runtime
#: this machine does not have -- so on the corpus where 74 seconds hurts most,
#: the 74 seconds establishes something the stage had already decided.
#:
#: Taken only when the tests can actually run and could actually block, which is
#: what ``take_baseline`` now checks. Everything else about the baseline is
#: unchanged: it is still taken before the first edit, still on a background
#: thread, and still joined before the first gate.
_BASELINE_TEST_STAGE = ("go_test", {})


@lru_cache(maxsize=1)
def _container_runtime() -> str | None:
    """The container runtime that is actually *running*, or None.

    ``shutil.which("docker")`` was the old test and it is the wrong question.
    Docker Desktop leaves the binary on PATH whether or not the daemon is up, so
    on a machine with it installed and stopped -- the ordinary state of a
    developer laptop -- the probe said yes, `go_test` stayed blocking, and the
    baseline spent 74 seconds discovering that testcontainers cannot reach a
    daemon. Measured on this machine: `which docker` succeeds, `docker info`
    exits 1.

    Cached for the process. The daemon can come up mid-run and this will not
    notice, which is the right trade: the alternative is a subprocess on every
    gate, and a run that starts without Docker is not going to grow tests that
    need it halfway through.
    """
    for name in ("docker", "podman"):
        if not shutil.which(name):
            continue
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [name, "info"],
                capture_output=True,
                timeout=8,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return name
    return None


def _tests_are_worth_baselining(router: Router) -> bool:
    """Whether a ``go test`` baseline can tell us anything the gate will use.

    It costs 74 seconds on a legacy service and the first gate waits for it, so
    it has to earn that. It does not when the stage is going to be advisory
    anyway: `_tests_can_run` downgrades `go_test` whenever the tests in scope
    need a container runtime and none is installed, and an advisory stage never
    consults a baseline. Measured: 80s with, 6s without, same verdict.
    """
    root = router.workspace.root
    if not any(root.rglob("*_test.go")):
        return False
    if _container_runtime() is not None:
        return True
    # No container runtime. If any test file stands one up, `go_test` is advisory
    # for the whole run and its baseline would be 74 seconds spent on a question
    # nobody asks.
    for test in root.rglob("*_test.go"):
        try:
            body = test.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(marker in body for marker in _CONTAINER_IMPORTS):
            return False
    return True


def take_baseline(router: Router, *, include_tests: bool = True) -> Baseline:
    """Record what is already broken, before this run can break anything.

    Correctness here is entirely a matter of timing: taken later, the snapshot
    contains the run's own damage and excuses it. So it is taken once, at the
    top of the run, when the workspace is definitely untouched — and, because
    `go vet` alone is about thirty seconds, off the critical path (see
    `AgentLoop._take_baseline`).

    ``go_mod`` runs in check mode, so taking a baseline cannot itself be the
    thing that rewrites `go.mod`.

    Failure is not fatal. An empty baseline is exactly the behaviour that
    shipped, so the worst case of not getting one is the status quo — but it is
    recorded as *not taken*, because "we did not look" and "nothing was wrong"
    must not read the same to `excuses`.
    """
    before = router.mutations
    findings: dict[str, frozenset[str]] = {}
    passed: dict[str, bool] = {}
    rule_classes: dict[str, frozenset[str]] = {}
    compliance: frozenset[str] = frozenset()

    root_is_module = (router.workspace.root / "go.mod").is_file()
    stages = list(_BASELINE_STAGES)
    if include_tests and root_is_module and _tests_are_worth_baselining(router):
        stages.append(_BASELINE_TEST_STAGE)
    for tool, args in stages:
        if not root_is_module:
            continue
        try:
            outcome = router.run_gate_tool(tool, dict(args))
        except Exception:  # noqa: BLE001 - a baseline is an optimisation, not a precondition
            continue
        if not isinstance(outcome, ToolResult):
            continue
        if tool == "go_mod":
            # tidy "passes" as a ToolResult whatever it found; the verdict is in
            # `meta.changed`, exactly as `_stage_passed` reads it.
            passed[tool] = not outcome.meta.get("changed", False)
            keys = outcome.meta.get("findings") or ()
            findings[tool] = frozenset(str(k) for k in keys)
            continue

        if tool == "rules_lint":
            # A lint that ran is a lint that succeeded; the verdict is the
            # count, exactly as `_lint_is_clean` reads it.
            count = int(outcome.meta.get("violations") or 0)
            passed[tool] = outcome.ok and count == 0
            findings[tool] = frozenset(
                str(k) for k in (outcome.meta.get("violation_keys") or ())
            )
            rule_classes[tool] = frozenset(
                str(r) for r in (outcome.meta.get("violation_rules") or ())
            )
            continue

        passed[tool] = outcome.ok
        if not outcome.ok:
            findings[tool] = _finding_keys(outcome.for_model())

    try:
        outcome = router.run_gate_tool("swagger_check", {})
        if isinstance(outcome, ToolResult):
            compliance = frozenset(str(k) for k in (outcome.meta.get("violations") or ()))
    except Exception:  # noqa: BLE001 - see above
        pass

    if router.mutations != before:
        # Something was written while this was being measured, so it is not a
        # picture of the workspace as the run found it. Better none than one
        # that excuses the run's own damage.
        return Baseline()
    return Baseline(
        findings=findings,
        passed=passed,
        rule_classes=rule_classes,
        compliance=compliance,
        taken=True,
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
        blocking = stage.blocking
        content = result.for_model()

        if not ok and blocking:
            # Two reasons a blocking stage stops blocking, both of them about
            # what this run is answerable for.
            if reason := stage.advisory_when(ctx):
                blocking = False
                content = f"{content}\n\nAdvisory, not blocking: {reason}."
            elif stage.baseline_key and ctx.baseline.excuses(
                stage.baseline_key, _stage_findings(stage, result)
            ):
                blocking = False
                content = (
                    f"{content}\n\nAdvisory, not blocking: every finding here was "
                    "already present before this run changed anything, so it is not "
                    "this change's failure. Fix it only if it bears on the work."
                )

        results.append(
            StageResult(
                stage.name, ok, blocking, content, elapsed,
                findings=frozenset() if ok else _stage_findings(stage, result),
            )
        )

        if blocking and not ok and stage.halts:
            # Fail-fast, but only where "fast" is also "correct". Running
            # `go vet` over code that does not compile produces pages of errors
            # that are all consequences of the one the model already has to fix.
            return GateReport(
                tuple(results),
                tuple(s.name for s in stages[index + 1 :]),
                time.monotonic() - started,
            )

        # Everything else that fails is recorded and the sequence carries on.
        # (`blocking` above, not `stage.blocking`: a stage excused by the
        # baseline must not halt the sequence either.)
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


def _stage_findings(stage: Stage, result: ToolResult) -> frozenset[str]:
    """The failing stage's findings, keyed the same way the baseline keyed them."""
    if stage.tool == "go_mod":
        return frozenset(str(k) for k in (result.meta.get("findings") or ()))
    if stage.tool == "rules_lint":
        # `rule|path|message`, the same shape the baseline recorded. Never the
        # rendered prose: that is grouped, elided and worst-first, so it does not
        # contain the findings at all past the third of each rule.
        return frozenset(str(k) for k in (result.meta.get("violation_keys") or ()))
    return _finding_keys(result.for_model())


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
    """Blocking violations only, read from ``meta`` rather than from the prose.

    This is the stage that carries the product's promise — "verified by a static
    template linter before a human sees the diff" — and for its whole life it
    could not fail on a finding.

    It parsed ``result.content`` as JSON. That content has been *rendered prose*
    since ``_render_lint`` landed: grouped by rule, worst first, with an elided
    count. So the decode raised on every single call, the ``except`` fell back to
    ``result.ok``, and ``result.ok`` is True whenever the sidecar ran — a lint is
    a report, and a report full of violations is the tool succeeding. The
    blocking contract-lint stage could therefore only fail on a sidecar crash.
    The gate blocked on things the run did not cause and waved through the one
    thing it was built to catch.

    The machine-readable half was never lost: ``gotools._report`` copies the
    counts into ``meta`` beside the rendered text, for exactly this. Reading it
    there needs no parsing and cannot be broken by a change to the rendering.

    ``out_of_scope`` is pre-existing damage in files the agent never touched, and
    is deliberately not counted. Blocking on it would make the first change to
    any legacy service impossible, which is precisely the codebase this agent
    exists to help with.
    """
    if not result.ok:
        return False

    meta = result.meta or {}
    if "violations" in meta:
        try:
            return int(meta["violations"]) == 0
        except (TypeError, ValueError):
            return False
    # `count` is the sidecar's own total, kept in meta by `_REPORT_FACTS`.
    if "count" in meta:
        try:
            return int(meta["count"]) == 0
        except (TypeError, ValueError):
            return False

    # No counts at all: the renderer fell through to raw JSON, which is the one
    # case where the body really is the structured form.
    import json

    try:
        payload = json.loads(result.content)
    except (json.JSONDecodeError, TypeError, ValueError):
        # Nothing to read. Refusing to pass is the safe direction for a blocking
        # stage: a lint whose verdict cannot be established has not verified
        # anything, and saying otherwise is the overclaim this stage exists to
        # prevent. `_render_lint` says "clean" in so many words when it is.
        return result.content.strip().startswith("clean")
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("ok", True)) and int(payload.get("count", 0) or 0) == 0
