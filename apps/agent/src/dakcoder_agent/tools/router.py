"""The tool router: everything that happens between a model's call and a tool running.

Six checks, in this order, and the order is the design:

1. **Does the tool exist?** A misremembered name gets the nearest real one back.
2. **Is it visible in this mode?** The second lock (see ``registry``).
3. **Is it implemented?** A specified-but-unwired tool says so and names a substitute.
4. **Are the arguments valid?** Model output is untrusted input, not a suggestion.
5. **Do the paths stay in the workspace?** Enforced by ``Workspace``, never by hand.
6. **Does it need a human?** Approval is decided from the resolved arguments.

Only then does anything run.

The ordering matters because each check makes the next one meaningful: validating
arguments for a tool that does not exist produces a confusing error, and
resolving paths before checking the mode would let a Planner learn whether a file
exists outside its permitted reach. Cheapest and broadest first, most specific
last.

Every refusal carries a ``fix``. That is not politeness — a refusal the model
cannot act on costs a full turn while it guesses, and at roughly four seconds a
turn on this endpoint, a vague error is measured in seconds of a developer's
attention.
"""

from __future__ import annotations

import uuid

import difflib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dakcoder_shared.envelope import Mutation, ToolResult
from dakcoder_shared.paths import PathEscape, Workspace, is_protected

from ..modes import Mode
from . import registry
from .registry import Approval, Provider, ToolSpec

__all__ = [
    "ApprovalPolicy",
    "ApprovalRequest",
    "Router",
    "ToolHandler",
    "ToolOutcome",
]

#: A handler takes the validated arguments and the context it needs, and returns
#: a result. It never sees the raw model output — the router has already checked
#: types, resolved paths and rejected unknown parameters by the time it is called.
ToolHandler = Callable[["Invocation"], ToolResult]

#: Arguments whose value is a workspace path, resolved and confined before any
#: handler sees them. Listed rather than inferred from the name, because
#: inferring would mean a new path-shaped parameter silently skips confinement.
_PATH_ARGS = frozenset({"path"})

#: Arguments holding a comma-separated list of workspace paths.
_PATH_LIST_ARGS = frozenset({"paths"})


@dataclass(frozen=True, slots=True)
class Invocation:
    """One validated call, as a handler receives it."""

    spec: ToolSpec
    arguments: dict[str, Any]
    workspace: Workspace
    #: Paths already resolved and confined, workspace-relative POSIX.
    paths: tuple[str, ...] = ()
    mode: Mode = Mode.CODER

    def arg(self, name: str, default: Any = None) -> Any:
        return self.arguments.get(name, default)

    def path(self, name: str = "path") -> str:
        """A confined workspace-relative path argument."""
        return self.arguments[name]

    def absolute(self, name: str = "path"):
        return self.workspace.resolve(self.arguments[name])


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A call that is legal but needs a human first.

    Returned rather than raised, and rather than resolved by blocking on a
    prompt. The router is synchronous and pure; who asks the developer, how, and
    what happens if they walk away are the caller's problem — which is the only
    way the same router serves the CLI, the extension and the test suite.
    """

    tool: str
    arguments: dict[str, Any]
    #: One sentence the UI can show as-is.
    reason: str
    paths: tuple[str, ...] = ()
    #: Set when the tool can never run without approval, whatever the arguments.
    unconditional: bool = False
    #: Minted here, where the request is minted, so the id on the wire and the
    #: id the runtime waits on are the same object's.
    #:
    #: The first version let the loopback mint one *after* the loop had already
    #: emitted `tool_pending`, which meant the event carried no id at all and the
    #: extension had no way to answer the card that event raised. It was also a
    #: race: a client polling the instant it saw the event could arrive before
    #: the approval existed.
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "arguments": self.arguments,
            "reason": self.reason,
            "paths": list(self.paths),
            "unconditional": self.unconditional,
            "protected": [p for p in self.paths if is_protected(p)],
        }


#: What ``dispatch`` returns. A union rather than an exception because needing
#: approval is an ordinary outcome, not an error, and modelling it as an
#: exception makes the happy path and the common path different shapes.
ToolOutcome = ToolResult | ApprovalRequest


@dataclass
class ApprovalPolicy:
    """Which calls may proceed without asking.

    Deliberately narrow. ``always_ask`` cannot be overridden by ``auto`` — a
    session-wide "yes to everything" that also covers ``delete_file`` is how an
    approval layer becomes decoration.
    """

    #: Tool names the developer has pre-approved for this session.
    auto: set[str] = field(default_factory=set)
    #: Never auto-approvable, whatever ``auto`` says.
    always_ask: frozenset[str] = frozenset({"delete_file"})

    def allows(self, spec: ToolSpec) -> bool:
        if spec.name in self.always_ask:
            return False
        return spec.name in self.auto


class Router:
    """Validates, confines, gates and dispatches every tool call."""

    def __init__(
        self,
        workspace: Workspace,
        handlers: Mapping[str, ToolHandler] | None = None,
        *,
        policy: ApprovalPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.workspace = workspace
        self.handlers: dict[str, ToolHandler] = dict(handlers or {})
        self.policy = policy or ApprovalPolicy()
        self._clock = clock
        #: Every path mutated this session, in order, for the gate to scope itself to.
        self.touched: list[str] = []

    def register(self, name: str, handler: ToolHandler) -> None:
        if name not in registry.REGISTRY:
            raise KeyError(f"{name} is not in the registry; add a ToolSpec first")
        self.handlers[name] = handler

    def schemas_for(self, mode: Mode | str) -> list[dict[str, Any]]:
        """The tools array for a request, with unimplemented tools filtered out.

        The registry hides tools marked unavailable; this also hides ones with no
        handler registered, so a partially-wired router never offers something
        that would fail at dispatch. Offering a tool that cannot run is worse
        than not offering it: the model spends a turn discovering it.
        """
        m = Mode(mode)
        return [
            spec.schema()
            for spec in registry.all_specs()
            if spec.visible_in(m) and spec.name in self.handlers
        ]

    # -- the entry point --------------------------------------------------

    def dispatch(
        self,
        name: str,
        arguments: Any,
        *,
        mode: Mode | str = Mode.CODER,
        approved: bool = False,
        gate: bool = False,
    ) -> ToolOutcome:
        """Run one tool call, or say precisely why not.

        ``gate=True`` marks a call made by the verification gate rather than by
        the model. The mode filter and the approval layer both exist to constrain
        what the *model* may do; applying them to the harness would mean the gate
        needed permission to check the work, which is backwards.
        """
        mode = Mode(mode)
        started = self._clock()

        spec = registry.get(name)
        if spec is None:
            return self._unknown_tool(name, mode)

        if spec.gate_only and not gate:
            # Reachable only if a stale schema list is replayed from an earlier
            # turn; the gate reaches these through `run_gate_tool`.
            return ToolResult.failure(
                f"{name} is run automatically by the verification gate, not called directly.",
                fix=f"Make your edits; the gate runs {name} for you.",
            )

        if not gate and not spec.visible_in(mode):
            return self._wrong_mode(spec, mode)

        if spec.unavailable:
            return ToolResult.failure(
                f"{name} is not available: {spec.unavailable}",
                fix=f"Instead, {spec.instead}." if spec.instead else "",
            )

        handler = self.handlers.get(name)
        if handler is None:
            return ToolResult.failure(
                f"{name} has no implementation registered in this runtime.",
                fix=f"Instead, {spec.instead}." if spec.instead else "",
            )

        try:
            args = _coerce(spec, arguments)
        except _ArgError as exc:
            return ToolResult.failure(str(exc), fix=exc.fix)

        try:
            args, paths = self._confine(spec, args)
        except PathEscape as exc:
            return ToolResult.failure(f"{name}: {exc.reason}.", fix=exc.fix)

        decision = self._approval(spec, args, paths)
        if decision is not None and not (approved or gate):
            return decision

        invocation = Invocation(
            spec=spec, arguments=args, workspace=self.workspace, paths=paths, mode=mode
        )
        result = self._run(spec, handler, invocation)

        for mutation in result.mutations:
            if mutation.path not in self.touched:
                self.touched.append(mutation.path)

        elapsed_ms = int((self._clock() - started) * 1000)
        return ToolResult(
            ok=result.ok,
            content=result.content,
            mutations=result.mutations,
            fix=result.fix,
            truncated=result.truncated,
            meta={**result.meta, "tool": name, "ms": elapsed_ms},
        )

    def run_gate_tool(self, name: str, arguments: Any = None) -> ToolOutcome:
        """Dispatch a gate stage, bypassing mode and approval.

        The gate is the harness, not the model. It runs a fixed sequence
        (Part A section 9.3) whose stages are not the model's to choose or
        refuse, so the checks that exist to constrain the model do not apply.
        """
        return self.dispatch(name, arguments or {}, mode=Mode.VERIFIER, gate=True)

    # -- the individual checks --------------------------------------------

    def _unknown_tool(self, name: str, mode: Mode) -> ToolResult:
        """A name that is not in the registry at all.

        Nearest-match rather than a bare "unknown tool", because the usual cause
        is a plausible near-miss — ``read`` for ``read_file``, ``grep`` for
        ``search_repo`` — and the model corrects instantly when told the real
        name. Suggestions are drawn from *this mode* so the fix is one it can act
        on rather than a tool it would be refused for asking about next.
        """
        available = self.schemas_for(mode)
        candidates = [s["function"]["name"] for s in available]

        # Habits from other harnesses and from the shell, mapped explicitly.
        # None of these are textually close to the right answer, so edit distance
        # never rescues them — and they are the calls a model actually makes.
        # `run_terminal` refusing `grep` and naming `search_repo` was the single
        # highest-value line in postgen; this is that idea moved one level up,
        # where it also covers the habits `run_terminal` never gets to see.
        aliased = _ALIASES.get(name.lower())
        spec = registry.get(aliased) if aliased else None
        if spec is not None:
            if spec.visible_in(mode):
                # Available here — the model simply used another name for it.
                fix = f"Use {spec.name} instead."
            elif spec.modes:
                where = ", ".join(sorted(str(m) for m in spec.modes))
                fix = f"You want {spec.name}, which {mode} mode does not have (it belongs to {where})."
            else:
                fix = f"You want {spec.name}, which runs automatically rather than on request."
            return ToolResult.failure(f"There is no tool called {name!r}.", fix=fix)

        close = difflib.get_close_matches(name, candidates, n=1, cutoff=0.6)
        if not close:
            close = difflib.get_close_matches(name, list(registry.REGISTRY), n=1, cutoff=0.75)
            if close and registry.REGISTRY[close[0]].instead:
                return ToolResult.failure(
                    f"There is no tool called {name!r}.",
                    fix=f"You may mean {close[0]}, which is not available here — "
                    f"{registry.REGISTRY[close[0]].instead}.",
                )
        fix = (
            f"Did you mean {close[0]}?"
            if close
            else f"Tools available in {mode} mode: {', '.join(candidates)}."
        )
        return ToolResult.failure(f"There is no tool called {name!r}.", fix=fix)

    def _wrong_mode(self, spec: ToolSpec, mode: Mode) -> ToolResult:
        """The tool exists but is not this mode's to use.

        Says which modes do have it. The model cannot switch modes itself — the
        loop does that — so this reads as an explanation of the boundary rather
        than an instruction, and the alternative is what it is meant to act on.
        """
        elsewhere = ", ".join(sorted(str(m) for m in spec.modes)) or "no mode"
        if spec.instead:
            fix = f"Instead, {spec.instead}."
        elif spec.mutates:
            fix = "Describe the change you want; a later step in the run will make it."
        else:
            # A read-only tool refused by mode leaves nothing to redirect to, so
            # the useful thing is the list of what *is* reachable. Without it the
            # model re-reads the schema list it already has and tries the next
            # closest name, which is a turn spent learning nothing.
            here = ", ".join(s["function"]["name"] for s in self.schemas_for(mode))
            fix = f"Work with what {mode} mode has: {here}."
        return ToolResult.failure(
            f"{spec.name} is not available in {mode} mode (it belongs to {elsewhere}).",
            fix=fix,
        )

    def _confine(
        self, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Resolve every path argument, or refuse the call.

        Rewrites the arguments to the *normalised* relative form, so a handler
        can never act on the string the model wrote. That is what makes
        ``./configs/../configs/app.yaml`` fail the protected-path check it would
        otherwise slip past by spelling.
        """
        resolved: list[str] = []
        args = dict(args)

        for key in _PATH_ARGS & args.keys():
            if not isinstance(args[key], str):
                continue
            rel = self.workspace.relative(self.workspace.resolve(args[key]))
            args[key] = rel
            resolved.append(rel)

        for key in _PATH_LIST_ARGS & args.keys():
            raw = args[key]
            if not isinstance(raw, str) or not raw.strip():
                continue
            items = [p.strip() for p in raw.split(",") if p.strip()]
            rels = [self.workspace.relative(self.workspace.resolve(p)) for p in items]
            args[key] = ",".join(rels)
            resolved.extend(rels)

        return args, tuple(dict.fromkeys(resolved))

    def _approval(
        self, spec: ToolSpec, args: dict[str, Any], paths: Sequence[str]
    ) -> ApprovalRequest | None:
        """Whether this specific call needs a human, and why."""
        if spec.approval is Approval.NONE:
            return None

        if spec.approval is Approval.ALWAYS:
            if self.policy.allows(spec):
                return None
            return ApprovalRequest(
                spec.name,
                args,
                reason=_always_reason(spec, args),
                paths=tuple(paths),
                unconditional=True,
            )

        reason = _conditional_reason(spec, args, paths)
        if reason is None:
            return None
        if self.policy.allows(spec):
            return None
        return ApprovalRequest(spec.name, args, reason=reason, paths=tuple(paths))

    def _run(self, spec: ToolSpec, handler: ToolHandler, invocation: Invocation) -> ToolResult:
        """Call the handler, turning any escape into a result rather than a crash.

        A tool that raises must not take the session with it. The traceback is
        kept in ``meta`` for the log and stripped from what the model sees: a
        Python stack tells it nothing actionable about a Go repository, and it is
        expensive text to put through the budget.
        """
        try:
            return handler(invocation)
        except PathEscape as exc:
            return ToolResult.failure(f"{spec.name}: {exc.reason}.", fix=exc.fix)
        except FileNotFoundError as exc:
            missing = getattr(exc, "filename", None) or str(exc)
            if spec.provider is not Provider.PYTHON:
                return ToolResult.failure(
                    f"the {spec.provider} sidecar is not installed or not on PATH ({missing}).",
                    fix="This is an environment problem, not a code problem. "
                    "Report it rather than working around it.",
                )
            return self._missing_path(spec, invocation, missing)
        except TimeoutError as exc:
            return ToolResult.failure(
                f"{spec.name} timed out: {exc}",
                fix="Narrow the scope — one package rather than the whole module.",
            )
        except IsADirectoryError as exc:
            return ToolResult.failure(
                f"{spec.name}: {getattr(exc, 'filename', None) or exc} is a directory.",
                fix="Use repo_map to list it, or name a file inside it.",
            )
        except Exception as exc:  # noqa: BLE001 - a tool crash must not end the session
            return ToolResult.failure(
                f"{spec.name} failed: {type(exc).__name__}: {exc}",
                meta={"traceback": True},
            )


    #: How many near-miss paths to offer. Three is enough to contain the right
    #: answer and short enough that a wrong list costs almost nothing.
    _SUGGESTIONS = 3

    def _missing_path(self, spec: ToolSpec, invocation: Invocation, missing: str) -> ToolResult:
        """Report a path that is not there, and say what is.

        "does not exist — check the path with repo_map or search_repo" is true,
        actionable in principle, and was ignored three turns running in the
        field. The model had guessed a plausible filename; being told to go
        looking gave it nothing to look *for*, so it made the same guess again
        and the run died against the no-progress detector.

        Naming the closest real files ends that in one turn. The guess is
        usually close — right directory, wrong file, or the right name in a
        different package — which is exactly the case a similarity match wins.

        The path is reported workspace-relative. The raw exception carries an
        absolute OS path, and every other message in this process speaks
        relative paths; handing the model
        ``D:\\desktop\\svc\\handler\\response\\x.go`` invites it to send that
        back, which the workspace guard then refuses for a different reason.
        """
        wanted = self._relative(missing)
        suggestions = self._nearest(wanted)

        if not suggestions:
            return ToolResult.failure(
                f"{spec.name}: {wanted} does not exist, and nothing in the workspace "
                "has a similar name.",
                fix="Call repo_map to see what is actually here before reading again. "
                "Do not retry this path.",
            )
        listed = "\n".join(f"  {p}" for p in suggestions)
        return ToolResult.failure(
            f"{spec.name}: {wanted} does not exist. The closest files that do:\n{listed}",
            fix=f"Read one of those, or call repo_map. Do not retry {wanted}.",
        )

    def _relative(self, path: str) -> str:
        """Render a path relative to the workspace, falling back to the name."""
        try:
            return self.workspace.relative(Path(path))
        except (ValueError, OSError):
            return Path(path).name

    def _nearest(self, wanted: str) -> list[str]:
        """Workspace files whose names are closest to the one asked for.

        Matched on the basename first and the whole path second: a model that
        guesses the directory wrong but the filename right is the common case,
        and a whole-path match scores that no better than an unrelated file in
        the directory it guessed.
        """
        target = Path(wanted)
        try:
            candidates = [
                p for p in self.workspace.root.rglob(f"*{target.suffix or ''}")
                if p.is_file() and not _ignored(p)
            ]
        except OSError:
            return []

        stem = target.stem.lower()
        scored: list[tuple[float, str]] = []
        for p in candidates[:5_000]:  # a bound, not a limit anyone should reach
            try:
                rel = p.relative_to(self.workspace.root).as_posix()
            except ValueError:
                continue
            score = difflib.SequenceMatcher(None, stem, p.stem.lower()).ratio()
            # A directory in common is worth something, but never enough to beat
            # a better filename: the filename is what the model was reaching for.
            if target.parent != Path(".") and str(target.parent) in rel:
                score += 0.15
            scored.append((score, rel))

        scored.sort(key=lambda s: (-s[0], s[1]))
        return [rel for score, rel in scored[: self._SUGGESTIONS] if score >= 0.5]


#: Directories whose contents are never a useful suggestion.
_IGNORED_DIRS = frozenset({".git", "vendor", "node_modules", ".venv", "bin", "dist", "build"})


def _ignored(path: Path) -> bool:
    return any(part in _IGNORED_DIRS for part in path.parts)


# ── approval reasons ────────────────────────────────────────────────────────


def _always_reason(spec: ToolSpec, args: Mapping[str, Any]) -> str:
    if spec.name == "delete_file":
        return f"Delete {args.get('path', '?')}: {args.get('reason', 'no reason given')}"
    if spec.name in ("resource_scaffold", "project_scaffold"):
        return f"{spec.name} writes several files at once and overwrites nothing silently."
    return f"{spec.name} always needs approval."


def _conditional_reason(
    spec: ToolSpec, args: Mapping[str, Any], paths: Sequence[str]
) -> str | None:
    """Why this call needs approval, or None if it does not.

    Everything here is argument-dependent by definition: the same tool is
    routine on one path and structural on another. ``patch_file`` on a handler
    is the bread and butter of the agent; ``patch_file`` on ``go.mod`` changes
    what the program depends on.
    """
    protected = [p for p in paths if is_protected(p)]
    if protected:
        return (
            f"{spec.name} changes {', '.join(protected)}, which is generated or "
            "structural. Review before it is written."
        )

    if spec.name == "go_mod":
        op = args.get("op")
        if op == "get":
            pkg = args.get("pkg", "?")
            return (
                f"Adding {pkg} as a direct dependency. New dependencies are "
                "allow-listed and reviewed, not added mid-task."
            )
        return None

    if spec.name == "git_ops":
        if args.get("op") == "commit":
            return f"Commit: {args.get('message', '(no message)')}"
        return None

    if spec.name == "run_terminal":
        return f"Run: {args.get('argv', '?')}"

    return None


# ── argument validation ─────────────────────────────────────────────────────


#: Names a model reaches for out of habit, and what this harness calls them.
#: Not misspellings — every one of these is a correct tool name somewhere else,
#: which is exactly why edit distance cannot find the right answer for them.
_ALIASES: dict[str, str] = {
    "grep": "search_repo",
    "rg": "search_repo",
    "ripgrep": "search_repo",
    "find": "search_repo",
    "glob": "search_repo",
    "ls": "repo_map",
    "dir": "repo_map",
    "tree": "repo_map",
    "list_files": "repo_map",
    "cat": "read_file",
    "head": "read_file",
    "tail": "read_file",
    "type": "read_file",
    "open": "read_file",
    "view": "read_file",
    "str_replace": "patch_file",
    "str_replace_editor": "patch_file",
    "edit": "patch_file",
    "edit_file": "patch_file",
    "replace": "patch_file",
    "sed": "patch_file",
    "apply_patch": "patch_file",
    "create": "write_file",
    "create_file": "write_file",
    "touch": "write_file",
    "rm": "delete_file",
    "del": "delete_file",
    "unlink": "delete_file",
    "bash": "run_terminal",
    "sh": "run_terminal",
    "shell": "run_terminal",
    "powershell": "run_terminal",
    "exec": "run_terminal",
    "execute": "run_terminal",
    "run": "run_terminal",
    "run_command": "run_terminal",
    "terminal": "run_terminal",
    "build": "go_build",
    "compile": "go_build",
    "make": "go_build",
    "test": "go_test",
    "lint": "rules_lint",
    "git": "git_status",
    "status": "git_status",
    "commit": "git_ops",
    "diff": "git_diff",
    "docs": "search_docs",
    "search": "search_repo",
}


class _ArgError(ValueError):
    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


def _coerce(spec: ToolSpec, raw: Any) -> dict[str, Any]:
    """Validate model-supplied arguments against the hand-written schema.

    Written rather than delegated to ``jsonschema`` for two reasons. The schemas
    use a deliberately tiny subset — type, enum, minimum, maximum, required — so
    a general validator is a dependency in the offline-install closure buying
    nothing. And the messages here are written as instructions to the model,
    which a general validator's cannot be: "start must be a whole number, e.g.
    120" is actionable in a way that "120.5 is not of type 'integer'" is not.

    Two lenient coercions, both for documented model behaviour rather than
    convenience:

    * arguments arriving as a JSON *string* instead of an object, which this
      endpoint's tool-calling does intermittently;
    * ``"40"`` for an integer parameter, which every model does.

    Both are unambiguous and both would otherwise cost a turn. Everything else
    is refused: silently accepting a wrong type is how a tool ends up reading
    line ``None`` to line ``None``.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raw = {}
        else:
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise _ArgError(
                    f"{spec.name}: arguments are not valid JSON ({exc.msg}).",
                    "Send the arguments as a JSON object.",
                ) from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _ArgError(
            f"{spec.name}: arguments must be a JSON object, got {type(raw).__name__}.",
            "Send an object with the parameter names as keys.",
        )

    props: Mapping[str, Any] = spec.parameters.get("properties", {})

    unknown = set(raw) - set(props)
    if unknown:
        known = ", ".join(props) or "none"
        raise _ArgError(
            f"{spec.name}: unknown parameter(s) {', '.join(sorted(unknown))}.",
            f"{spec.name} accepts: {known}.",
        )

    missing = [p for p in spec.required if raw.get(p) in (None, "")]
    if missing:
        raise _ArgError(
            f"{spec.name}: missing required parameter(s) {', '.join(missing)}.",
            f"Call {spec.name} again with {' and '.join(missing)} set.",
        )

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        out[key] = _coerce_one(spec.name, key, value, props[key])
    return out


def _coerce_one(tool: str, key: str, value: Any, schema: Mapping[str, Any]) -> Any:
    expected = schema.get("type", "string")

    if expected == "integer":
        if isinstance(value, bool):
            raise _ArgError(f"{tool}.{key} must be a whole number, not a boolean.")
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise _ArgError(
                f"{tool}.{key} must be a whole number, got {value!r}.",
                f"Pass {key} as a number, for example 120.",
            )
        low, high = schema.get("minimum"), schema.get("maximum")
        if low is not None and value < low:
            raise _ArgError(f"{tool}.{key} must be at least {low}, got {value}.")
        if high is not None and value > high:
            # Clamping instead of refusing: an over-large max is the model asking
            # for "everything", and the ceiling is exactly what we want to give.
            value = high
        return value

    if expected == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, (list, dict)):
            value = json.dumps(value, separators=(",", ":"))
        if not isinstance(value, str):
            raise _ArgError(f"{tool}.{key} must be text, got {type(value).__name__}.")
        allowed = schema.get("enum")
        if allowed and value not in allowed:
            raise _ArgError(
                f"{tool}.{key} must be one of {', '.join(allowed)}, got {value!r}.",
                f"Call {tool} again with {key} set to one of: {', '.join(allowed)}.",
            )
        return value

    return value


def mutation(workspace: Workspace, path: str, kind: str) -> Mutation:
    """Build a mutation record from a confined path. Handler convenience."""
    from dakcoder_shared.envelope import MutationKind

    return Mutation(path=path, kind=MutationKind(kind))
