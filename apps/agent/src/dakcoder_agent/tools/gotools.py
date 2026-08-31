"""The bridge to the Go sidecar: MCP over stdio, spoken directly.

``gotools`` owns everything that needs a Go parser — the rule engine, the legacy
audit, the repository map, the scaffolder, the FX rewriter. It is a separate
process because Go's own AST packages are the right tool for reading Go, and
reimplementing them in Python would mean two parsers disagreeing about the same
file.

**Why the protocol is hand-written.** The ``mcp`` Python SDK is installed and
would work. It is also async, which would push an event loop into an otherwise
synchronous agent, and it is another package in the closure that Part B section
4.3 has to vendor for offline install. Against that: we use exactly three
methods — ``initialize``, ``notifications/initialized``, ``tools/call`` — against
a server in this same repository, whose wire format is pinned by a test that
spawns the real binary. Same reasoning as choosing httpx over the OpenAI SDK, and
the same outcome: fewer moving parts, and the test exercises the real thing
rather than a mock of it.

**Why one long-lived process.** Starting ``gotools`` costs about 30 ms and the
handshake another few. Per call that is nothing; across a session with a hundred
lint calls it is several seconds of a latency budget Part A section 3 says is the
single biggest lever. The process is started on first use and reused, and a
crashed sidecar is restarted once — transparently, because a rule engine dying is
not something the model can do anything about.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dakcoder_shared.envelope import Mutation, MutationKind, ToolResult

from .router import Invocation

__all__ = ["GoTools", "Reply", "SidecarError", "handlers_for"]

PROTOCOL_VERSION = "2025-06-18"
STARTUP_TIMEOUT = 20.0
CALL_TIMEOUT = 120.0


class SidecarError(RuntimeError):
    """The sidecar is missing, would not start, or broke the protocol."""


@dataclass(frozen=True, slots=True)
class Reply:
    """One tool result from the sidecar.

    ``is_error`` is carried rather than raised. MCP uses it for two very
    different things — a malformed argument, and a tool that ran and reported a
    problem — and only the first is a bridge failure. Collapsing them would make
    a lint run that found violations indistinguishable from a broken sidecar,
    and the loop would retry the one thing that is working correctly.
    """

    text: str
    is_error: bool = False


class GoTools:
    """A supervised ``gotools mcp`` process, addressed synchronously.

    Not thread-safe by accident — the lock is deliberate. JSON-RPC over a pipe
    correlates responses by id, and two threads interleaving writes would read
    each other's replies. The agent loop is single-threaded, but the gate and a
    future editor-diagnostics path both want to call in, so the invariant is
    enforced here rather than assumed.
    """

    def __init__(self, root: Path, binary: str | None = None) -> None:
        self.root = root
        self.binary = binary or _find_binary()
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._restarts = 0

    # -- lifecycle --------------------------------------------------------

    def _spawn(self) -> subprocess.Popen[str]:
        if self.binary is None:
            raise SidecarError(
                "the gotools binary could not be located: GOTOOLS_PATH is unset "
                "or points at nothing, and there is no gotools on PATH. It ships "
                "inside the extension; a missing one means a broken install, not "
                'a code problem. Reinstall the .vsix, or set "dakcoder.gotoolsPath".'
            )
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.Popen(  # noqa: S603 - argv list, shell=False, our own binary
            [self.binary, "mcp", "--root", str(self.root)],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
        self._handshake(proc)
        return proc

    def _handshake(self, proc: subprocess.Popen[str]) -> None:
        self._write(
            proc,
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "dakcoder-agent", "version": "0.1.0"},
                },
            },
        )
        reply = self._read(proc, STARTUP_TIMEOUT)
        if "result" not in reply:
            raise SidecarError(f"gotools refused the handshake: {reply.get('error')}")
        self._write(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()

    def __enter__(self) -> GoTools:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- calling ----------------------------------------------------------

    def call(self, tool: str, arguments: Mapping[str, Any]) -> Reply:
        """Call one sidecar tool and return its text content.

        A dead process is restarted once and the call retried. Once, not in a
        loop: if the sidecar dies twice on the same input, the input is the
        problem, and retrying it forever turns a bad argument into a hang.
        """
        with self._lock:
            try:
                return self._call_locked(tool, arguments)
            except (BrokenPipeError, SidecarError, OSError) as first:
                if self._restarts >= 3:
                    raise SidecarError(
                        f"gotools has failed {self._restarts} times; not restarting again. "
                        f"Last error: {first}"
                    ) from first
                self._restarts += 1
                self._kill()
                try:
                    return self._call_locked(tool, arguments)
                except Exception as second:
                    raise SidecarError(
                        f"gotools failed twice on {tool}: {second}"
                    ) from second

    def _call_locked(self, tool: str, arguments: Mapping[str, Any]) -> Reply:
        if self._proc is None or self._proc.poll() is not None:
            self._proc = self._spawn()
        proc = self._proc

        request_id = self._id()
        self._write(
            proc,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": dict(arguments)},
            },
        )

        while True:
            message = self._read(proc, CALL_TIMEOUT)
            # Notifications carry no id and are not ours to wait on. Skipping
            # rather than failing keeps the client working when the server starts
            # emitting progress events, which is an additive change on its side.
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                raise SidecarError(f"{tool}: {error.get('message', error)}")
            result = message.get("result", {})
            return Reply(_text_of(result), bool(result.get("isError")))

    # -- framing ----------------------------------------------------------

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def _write(proc: subprocess.Popen[str], message: Mapping[str, Any]) -> None:
        if proc.stdin is None:
            raise SidecarError("gotools stdin is closed")
        proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    @staticmethod
    def _read(proc: subprocess.Popen[str], timeout: float) -> dict[str, Any]:
        """Read one newline-delimited JSON message.

        The timeout is enforced by a reader thread rather than by a socket
        option, because ``Popen.stdout`` is a blocking pipe with no timeout of
        its own on Windows. Without this a sidecar that hangs takes the agent
        with it, silently, with no output to explain why.
        """
        if proc.stdout is None:
            raise SidecarError("gotools stdout is closed")

        holder: list[str] = []

        def _reader() -> None:
            try:
                line = proc.stdout.readline()  # type: ignore[union-attr]
            except (OSError, ValueError):
                line = ""
            holder.append(line)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            proc.kill()
            raise SidecarError(f"gotools did not respond within {timeout:.0f}s")

        line = holder[0] if holder else ""
        if not line:
            code = proc.poll()
            raise SidecarError(f"gotools exited (code {code}) without replying")
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise SidecarError(f"gotools sent something that is not JSON: {line[:200]!r}") from exc

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()


def _find_binary() -> str | None:
    # The extension resolves the platform-suffixed binary it shipped
    # (`bin/gotools-win32-x64.exe`) and passes the full path, under the name
    # Part B §4.6 assigns. That is the only answer that works for an installed
    # runtime: the daemon lives in a venv under the extension's globalStorage,
    # so PATH holds no entry for the sidecar and the checkout fallback below
    # points into site-packages. Checked first because that path is
    # checksum-verified and version-matched to this build, which is the order
    # `dakcoder.gotoolsPath` documents. The name is composed on the Node side on
    # purpose (§4.5) — a second copy of that platform table here is the mapping
    # the build script exists to avoid.
    packaged = os.environ.get("GOTOOLS_PATH", "").strip()
    if packaged and Path(packaged).is_file():
        return packaged
    for name in ("gotools", "gotools.exe"):
        found = shutil.which(name)
        if found:
            return found
    # A development checkout, where the binary sits next to its source.
    local = Path(__file__).resolve().parents[5] / "gotools"
    for name in ("gotools.exe", "gotools"):
        candidate = local / name
        if candidate.is_file():
            return str(candidate)
    return None


def _text_of(result: Mapping[str, Any]) -> str:
    """Flatten an MCP tool result to text."""
    blocks = result.get("content") or []
    parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    text = "\n".join(p for p in parts if p)
    if not text and result.get("structuredContent"):
        text = json.dumps(result["structuredContent"], indent=2)
    return text


# ── handlers ────────────────────────────────────────────────────────────────
#
# Each one maps the router's validated arguments onto the sidecar's, and turns
# the reply into a ToolResult. The mapping is explicit rather than pass-through
# because the two schemas are allowed to diverge: the model-facing one is bound
# by C1's six-parameter limit, the sidecar's is not.


def handlers_for(sidecar: GoTools) -> dict[str, Any]:
    def repo_map(inv: Invocation) -> ToolResult:
        args: dict[str, Any] = {}
        if inv.arg("package"):
            args["package"] = inv.arg("package")
        if inv.arg("max_tokens"):
            args["max_tokens"] = inv.arg("max_tokens")
        return _plain(sidecar.call("repo_map", args))

    def rules_lint(inv: Invocation) -> ToolResult:
        args: dict[str, Any] = {}
        if inv.arg("paths"):
            args["paths"] = _list(inv.arg("paths"))
        if inv.arg("only"):
            args["only"] = _list(inv.arg("only"))
        # A finding is not a tool failure — it is the tool succeeding. Marking it
        # ok=False would make the loop treat a lint report as a broken tool and
        # retry it, which wastes a turn and reads as noise in a transcript.
        return _report(
            sidecar.call("rules_lint", args),
            lambda p: _render_lint(p, scope_hint="pass paths= to scope the lint"),
        )

    def legacy_audit(inv: Invocation) -> ToolResult:
        args = {"paths": _list(inv.arg("paths"))} if inv.arg("paths") else {}
        return _report(
            sidecar.call("legacy_audit", args),
            lambda p: _render_lint(p, scope_hint="pass paths= to scope the audit"),
        )

    def fx_wire(inv: Invocation) -> ToolResult:
        reply = sidecar.call("fx_wire", {"kind": inv.arg("kind"), "ctor": inv.arg("ctor")})
        if reply.is_error:
            return _plain(reply)

        payload = _json(reply.text)
        if payload.get("already_registered") and not payload.get("changed"):
            # The sidecar is explicit that this is success, not failure, and it
            # is worth preserving: a Debugger re-running fx_wire on an already
            # wired handler must not read that as "the wiring did not work".
            names = ", ".join(payload["already_registered"])
            return ToolResult.success(f"{names} was already registered; nothing to change.")

        path = payload.get("path") or "bootstrap/bootstrapper.go"
        added = ", ".join(payload.get("added") or []) or inv.arg("ctor")
        return ToolResult.success(
            f"registered {added} in {path}",
            mutations=[Mutation(path, MutationKind.MODIFY)] if payload.get("changed") else [],
        )

    def resource_scaffold(inv: Invocation) -> ToolResult:
        return _scaffold(sidecar, "resource_scaffold", inv, ("spec", "spec"))

    def project_scaffold(inv: Invocation) -> ToolResult:
        return _scaffold(
            sidecar, "project_scaffold", inv, ("project", "project"), ("resource", "resource")
        )

    def swagger_check(inv: Invocation) -> ToolResult:
        return _swagger_check(sidecar, inv)

    # The four review audits take no arguments: each answers one question about
    # the whole workspace. A report full of findings is the tool succeeding, so
    # none of them returns ok=False — that would make the loop retry the one
    # thing that is working.
    def db_roundtrip_audit(_inv: Invocation) -> ToolResult:
        return _report(
            sidecar.call("db_roundtrip_audit", {}),
            lambda p: _render_rows(
                p,
                "methods",
                lambda m: (
                    f"  {m.get('path')}:{m.get('line')}  {m.get('method')}  "
                    f"[{m.get('statements')} stmt"
                    f"{', in a loop' if m.get('in_loop') else ''}"
                    f"{', batched' if m.get('batched') else ''}"
                    f"{', txn' if m.get('transaction') else ''}]  {m.get('verdict')}"
                ),
                empty="no repository method makes more than one database call.",
                how="worst are shown first",
            ),
        )

    def validation_audit(_inv: Invocation) -> ToolResult:
        return _report(
            sidecar.call("validation_audit", {}),
            lambda p: _render_rows(
                p,
                "fields",
                lambda f: (
                    f"  {f.get('path')}:{f.get('line')}  "
                    f"{f.get('struct')}.{f.get('field')} ({f.get('type')})  "
                    + (
                        # "missing no validate tag" is what the naive form reads
                        # as, and the field with no tag at all is the worst case
                        # of the two — it should not be the one that reads like
                        # a typo.
                        "has no validate tag"
                        if f.get("missing") == "no validate tag"
                        else f'validate="{f.get("tag")}" — needs {f.get("missing")}'
                    )
                ),
                empty="every request field is bound.",
                how="the same fix applies to each",
            ),
        )

    def temporal_audit(_inv: Invocation) -> ToolResult:
        return _report(
            sidecar.call("temporal_audit", {}),
            lambda p: _render_rows(
                p,
                "candidates",
                lambda c: (
                    f"  {c.get('path')}:{c.get('line')}  {c.get('func')}  "
                    f"[{c.get('kind')}]  {c.get('call')}"
                ),
                empty="no off-request-path candidates found.",
                how="all are the same kinds of work",
            ),
        )

    def lib_version_check(_inv: Invocation) -> ToolResult:
        def render(payload: Mapping[str, Any]) -> str:
            result = payload.get("result") or {}
            rows = list(result.get("reports") or [])
            out = [str(payload.get("summary") or "").strip(), ""]
            for r in rows[:_MAX_ROWS]:
                module = str(r.get("module", "")).rsplit("/", 1)[-1]
                status = str(r.get("status", ""))
                if r.get("superseded_by"):
                    status = "SUPERSEDED -> " + str(r["superseded_by"]).rsplit("/", 1)[-1]
                if r.get("behind"):
                    status += f" (behind {r['behind']})"
                out.append(f"  {module:24s} {r.get('current','?'):10s} {status}")
            out.extend(_elided(_MAX_ROWS, len(rows), "all are CEPT modules"))
            if note := str(payload.get("note") or "").strip():
                out.extend(["", note])
            return "\n".join(line for line in out if line is not None)

        return _report(sidecar.call("lib_version_check", {}), render)

    return {
        "repo_map": repo_map,
        "rules_lint": rules_lint,
        "legacy_audit": legacy_audit,
        "fx_wire": fx_wire,
        "resource_scaffold": resource_scaffold,
        "project_scaffold": project_scaffold,
        "swagger_check": swagger_check,
        "db_roundtrip_audit": db_roundtrip_audit,
        "validation_audit": validation_audit,
        "temporal_audit": temporal_audit,
        "lib_version_check": lib_version_check,
    }


#: The config key that turns framework swagger generation on. Absent from all six
#: of the reference template's environment configs, so every service scaffolded
#: from it inherits the gap — which makes this the single most likely reason a
#: working endpoint is missing from /docs/v3Doc.json.
_SWAGGER_KEY = ("swagger", "generation", "mode")


def _swagger_check(sidecar: GoTools, inv: Invocation) -> ToolResult:
    """Check the preconditions for an endpoint appearing in the API document.

    Checks; does not generate. Part A section 7.2 is emphatic about the
    distinction because the previous generation used swaggo, where "check" and
    "generate" were the same command — and a developer who expects generation
    here will conclude the tool is broken when the document does not change.

    The boot-and-diff half of section 7.2 (start the service, fetch
    ``/docs/v3Doc.json``, compare) is deliberately not here. It needs a database
    and a free port, and a check that fails when Postgres is down would be
    disabled within a week.
    """
    blocking: list[str] = []
    advisory: list[str] = []
    #: Violations that were already there when the run started. Reported, never
    #: blocked on -- this run did not cause them and cannot be asked to fix them.
    pre_existing: list[str] = []

    # Scoped to what this run touched, like every other lint call in the gate.
    #
    # This one was unscoped, and it is why a two-file change could not pass. The
    # service has eight legacy handlers, none of which has a `Routes()` method;
    # an unscoped lint returned all eight as blocking, so `swagger_check` failed
    # on seven files the run had never opened. The Verifier correctly reported
    # them as pre-existing — twice — and the ladder sent it back to the Coder
    # anyway, because a blocking stage is blocking whatever the report says.
    #
    # The split below already states the principle for the config half: "block
    # on what this run did, report what was already broken". It simply was not
    # applied to the half that does the blocking.
    args: dict[str, Any] = {"only": ["swagger-visible", "routes-in-handler"]}
    if scoped := _list(inv.arg("paths")):
        args["paths"] = scoped
    reply = sidecar.call("rules_lint", args)
    if reply.is_error:
        return ToolResult.failure(reply.text, fix="Correct the arguments and re-run.")
    try:
        payload = json.loads(reply.text)
    except json.JSONDecodeError:
        payload = {}

    # Scoping cures the seven handlers this run never opened. The baseline cures
    # the eighth, which is the one the task is about.
    #
    # `routes-in-handler` reports at file granularity: "handler declared here has
    # no Routes() method". Eight legacy handlers predate the contract and every
    # one of them carries it. A vertical slice *has* to edit one of those files,
    # and the moment it does, that file is in scope and its pre-existing
    # violation becomes a blocking one -- permanently, because `touched` is
    # append-only for the life of the run. The change was correct and could not
    # be shipped.
    #
    # So the gate is told what was already broken before this run began, and
    # anything on that list is reported rather than blocked. Keyed on rule, path
    # and message with the line number deliberately left out: inserting a
    # function moves every violation below it, and a key that moves is a key that
    # matches nothing.
    seen = _baseline_keys(inv.arg("baseline"))
    keys: list[str] = []
    for violation in payload.get("violations") or []:
        key = _violation_key(violation)
        keys.append(key)
        rendered = (
            f"{violation.get('path', '?')}:{violation.get('line', 0)}: "
            f"{violation.get('message', '')} — {violation.get('fix', '')}"
        )
        if key in seen:
            pre_existing.append(rendered)
        else:
            blocking.append(rendered)

    configs = sorted(
        p
        for pattern in ("configs/*.yaml", "configs/*.yml")
        for p in inv.workspace.root.glob(pattern)
    )
    if not configs:
        blocking.append("there is no configs/ directory, so generation cannot be enabled")
    checked = {p: _has_key(p, _SWAGGER_KEY) for p in configs}
    missing = [inv.workspace.relative(p) for p, present in checked.items() if present is False]
    unknown = [inv.workspace.relative(p) for p, present in checked.items() if present is None]
    if unknown:
        advisory.append(
            f"could not read {', '.join(unknown)} — swagger.generation.mode was not "
            "checked there. If PyYAML is missing from this runtime, install it; the "
            "check is inconclusive, not passing."
        )

    # The split is the same principle as `out_of_scope_count` in rules_lint:
    # block on what this run did, report what was already broken. The key is
    # absent from all six of the reference template's configs, so every service
    # derived from it inherits the gap — and a gate that fails every run on a
    # pre-existing template defect is a gate that gets switched off.
    if missing:
        advisory.append(
            "swagger.generation.mode is not set in "
            + ", ".join(missing)
            + ". Routes will not reach /docs/v3Doc.json until it is. This is a gap in "
            "the reference template, not something this change introduced."
        )

    # Carried on every return, pass or fail, because the caller that needs them
    # is the one taking the baseline -- and it takes it on a clean repository,
    # where this returns success and there is nothing to read off the body.
    meta: dict[str, Any] = {"violations": keys}
    if pre_existing:
        advisory.append(
            f"{len(pre_existing)} handler(s) were already missing Routes() before "
            "this run started, and are not this change to fix:\n"
            + "\n".join(f"      {p}" for p in pre_existing)
        )
        meta["pre_existing"] = len(pre_existing)

    if blocking:
        listed = "\n".join(f"  - {p}" for p in blocking)
        body = f"swagger_check found {len(blocking)} problem(s):\n{listed}"
        if advisory:
            body += "\n\nAlso, not blocking:\n" + "\n".join(f"  - {a}" for a in advisory)
        return ToolResult.failure(
            body,
            fix="Every route needs .Name(...) — routes without one are skipped silently.",
            meta=meta,
        )

    if advisory:
        return ToolResult.success(
            "swagger_check: routes are named. Not blocking:\n"
            + "\n".join(f"  - {a}" for a in advisory),
            meta={**meta, "advisory": True},
        )
    return ToolResult.success(
        f"swagger_check: routes are named and generation is enabled in "
        f"{len(configs)} config(s)",
        meta=meta,
    )


def _has_key(path: Path, key: tuple[str, ...]) -> bool | None:
    """Whether a nested key exists in a YAML file. ``None`` means unknown.

    Parsed rather than grepped: ``swagger:`` appearing anywhere in the file would
    satisfy a substring search, including inside a comment explaining that the
    key is missing.

    The three-valued return is the point. PyYAML is the one optional dependency
    here, and collapsing "I could not look" into ``False`` would make an absent
    library report every config as missing the key — a check that cannot run
    claiming a negative result, which is the worst thing a check can do. An
    unreadable or malformed file is unknown for the same reason: the answer is
    that we do not know, and saying so is more useful than guessing.
    """
    try:
        import yaml
    except ImportError:
        return None
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    for part in key:
        if not isinstance(data, dict) or part not in data:
            return False
        data = data[part]
    return data is not None and data != ""


def _violation_key(violation: dict[str, Any]) -> str:
    """A violation's identity across an edit.

    Rule, path and message; deliberately not the line. Inserting a function
    moves every violation below it in the file, so a key carrying a line number
    would stop matching the moment the run did the work it was asked to do --
    which is exactly when the baseline has to hold.
    """
    return "|".join(
        (
            str(violation.get("rule", "")),
            str(violation.get("path", "")),
            str(violation.get("message", "")),
        )
    )


def _baseline_keys(raw: Any) -> frozenset[str]:
    """The keys the gate recorded before this run made its first edit."""
    if not raw:
        return frozenset()
    if isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset(str(x) for x in raw)
    return frozenset(part for part in str(raw).split("\x1f") if part)


def _list(raw: Any) -> list[str]:
    """Comma-separated string to list.

    The model-facing schema takes a string because C1 caps parameters at six and
    a flat string is one; the sidecar takes an array because Go's JSON schema
    generator produced one from a []string field. The seam is here, and it is the
    only place the two shapes have to agree.
    """
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _plain(reply: Reply) -> ToolResult:
    if reply.is_error:
        return ToolResult.failure(
            reply.text,
            fix="Correct the arguments and call the tool again.",
        )
    return ToolResult.success(reply.text)


# ── rendering reports for a model ───────────────────────────────────────────
#
# The sidecar answers in JSON, on one line. That is right for a program and
# wrong for the thing that actually reads it.
#
# A lint of one legacy service returns 705,000 characters of single-line JSON.
# The context manager caps a tool result at a few thousand tokens, so what
# reached the model was a JSON fragment ending mid-string: unparseable, and
# indistinguishable from the tool being broken. It said "the audits came back
# truncated", scoped the call, got another fragment, and repeated until the
# loop-breaker stopped it seventeen turns later.
#
# Truncating *text* degrades honestly — you lose the tail and keep the findings.
# Truncating a JSON object loses everything. So these render, and they render
# worst-first with an explicit count of what was left out, because the number
# omitted is itself a finding.

#: How much of a report to render. Chosen against the context caps below with
#: room to spare: a report that is itself elided has failed at the one job it
#: has, which is to be readable after elision.
_MAX_GROUPS = 8
_MAX_PER_GROUP = 3
_MAX_ROWS = 40


def _elided(shown: int, total: int, how: str) -> list[str]:
    """The line that says what is missing, and how to see it."""
    if total <= shown:
        return []
    return [f"… {total - shown:,} more not shown — {how}"]


def _render_lint(payload: Mapping[str, Any], *, scope_hint: str) -> str:
    """Group findings by rule, worst-first, with examples.

    Grouped rather than listed because the shape of the answer is what a review
    needs first: "1,085 missing db tags" is one decision, and the same
    information as eleven hundred lines that crowd out everything else.
    """
    findings = list(payload.get("violations") or [])
    findings += list(payload.get("out_of_scope") or [])
    warnings = list(payload.get("warnings") or [])
    files = payload.get("files_scanned", 0)

    if not findings and not warnings:
        return f"clean — nothing to report across {files} file(s)."

    by_rule: dict[str, list[Mapping[str, Any]]] = {}
    for f in findings + warnings:
        by_rule.setdefault(str(f.get("rule", "?")), []).append(f)
    ranked = sorted(by_rule.items(), key=lambda kv: -len(kv[1]))

    out = [
        f"{len(findings):,} blocking and {len(warnings):,} advisory finding(s) "
        f"across {files} file(s), in {len(by_rule)} rule(s). Grouped, worst first."
    ]
    for rule, group in ranked[:_MAX_GROUPS]:
        out.append("")
        out.append(f"{rule} — {len(group):,}")
        for f in group[:_MAX_PER_GROUP]:
            out.append(f"  {f.get('path')}:{f.get('line')}  {f.get('message')}")
        if fix := (group[0].get("fix") if group else None):
            out.append(f"  fix: {fix}")
        out.extend("  " + line for line in _elided(_MAX_PER_GROUP, len(group), "same rule"))

    out.append("")
    out.extend(_elided(_MAX_GROUPS, len(ranked), f"{scope_hint}, or pass only= for one rule"))
    return "\n".join(out)


def _render_rows(
    payload: Mapping[str, Any],
    key: str,
    row: Any,
    *,
    empty: str,
    how: str,
) -> str:
    """Render a list report as one line per entry, with a summary and a count."""
    rows = list(payload.get(key) or [])
    summary = str(payload.get("summary") or "").strip()
    if not rows:
        return summary or empty

    out = [summary] if summary else []
    out.append("")
    out.extend(row(r) for r in rows[:_MAX_ROWS])
    out.extend(_elided(_MAX_ROWS, len(rows), how))
    if note := str(payload.get("note") or "").strip():
        out.extend(["", note])
    return "\n".join(out)


def _report(reply: Reply, render) -> ToolResult:
    """Turn a sidecar report into readable text, never raising on shape.

    A renderer that throws on an unexpected payload would turn a working tool
    into a bridge error, which is the failure this whole path exists to avoid.
    The raw JSON is the fallback: worse to read, but not a lie.
    """
    if reply.is_error:
        return ToolResult.failure(reply.text, fix="Correct the arguments and call the tool again.")
    try:
        payload = json.loads(reply.text)
        if not isinstance(payload, Mapping):
            raise ValueError("not an object")
        return ToolResult.success(render(payload))
    except (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError):
        return ToolResult.success(reply.text)


def _scaffold(
    sidecar: GoTools,
    tool: str,
    inv: Invocation,
    *specs: tuple[str, str],
) -> ToolResult:
    """Run a scaffolder and report exactly which files it wrote.

    Specs are validated on the Go side, where the rules live, so a bad one comes
    back as an ``InvalidSpecError`` listing every problem with a fix for each.
    That is passed through unchanged and deliberately: it is a better error than
    anything this layer could construct, and re-wording it would lose the field
    paths the model needs in order to correct itself.
    """
    arguments: dict[str, Any] = {}
    for arg_name, wire_name in specs:
        raw = inv.arg(arg_name, "")
        try:
            arguments[wire_name] = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError as exc:
            return ToolResult.failure(
                f"{arg_name} is not valid JSON: {exc.msg} at position {exc.pos}.",
                fix=f"Send {arg_name} as a JSON object.",
            )

    reply = sidecar.call(tool, arguments)
    if reply.is_error:
        return ToolResult.failure(
            reply.text,
            fix="Correct the spec fields named above and call the tool again.",
        )

    payload = _json(reply.text)
    files = payload.get("files") or []
    if not payload.get("ok", True) or not files:
        return ToolResult.failure(
            reply.text,
            fix="Correct the spec fields named above and call the tool again.",
        )

    mutations = [
        Mutation(
            f["path"],
            MutationKind.CREATE if f.get("action") == "create" else MutationKind.MODIFY,
        )
        for f in files
        if f.get("path")
    ]

    lines = [f"{tool} wrote {len(mutations)} file(s):"]
    lines += [f"  {m.kind:6} {m.path}" for m in mutations]
    # Notes are steps the scaffolder deliberately left for a human — applying the
    # DDL, most often. Dropping them would leave the developer with a table that
    # does not exist and a resource that compiles, which is the worst pairing.
    notes = [str(n) for n in (payload.get("notes") or [])]
    for note in notes:
        lines.append(f"  NOTE: {note}")

    # Carried structurally as well as in the text. Flattening them into `content`
    # was the only copy, so a client wanting a "manual follow-ups" panel had to
    # parse a "  NOTE: " prefix back out of a string it shares with the file
    # listing — fragile, and wrong the first time a note contains a newline.
    return ToolResult.success(
        "\n".join(lines),
        mutations=mutations,
        meta={"notes": notes} if notes else {},
    )


def _json(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
