"""The tool catalog (Part A section 7.2), and the C1 rules enforced on it.

Two ideas carry most of the weight here.

**Mode filtering is a guarantee, not a hint.** The Planner cannot write, not
because the prompt asks it not to, but because ``write_file`` is not in the
schema list it receives and the router refuses it a second time if it is somehow
called anyway. Two locks on the same door, because the first one is a list the
model sees and the second is a check the model cannot see. Asking a model not to
do something is a hope; not giving it the capability is a property.

**Every refusal names the working alternative.** Inherited from ``postgen``,
where ``run_terminal`` refusing ``grep`` and saying "use ``search_repo``" was
measurably the highest-value line in the codebase — a bare refusal costs a turn
while the model guesses, and a named alternative costs nothing.

C1's limits (≤6 parameters, ≤200-character description, hand-written schema) are
checked at import. A violation is an ``ImportError``, not a lint warning, so a
tool that breaks the contract cannot reach a test run — let alone a release
where the extension and the gateway are both bound against it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..modes import Mode

__all__ = [
    "MAX_DESCRIPTION",
    "MAX_PARAMS",
    "Approval",
    "Provider",
    "REGISTRY",
    "ToolSpec",
    "get",
    "names_for",
    "gate_tools",
    "schemas_for",
]

#: C1's hard limits.
MAX_PARAMS = 6
MAX_DESCRIPTION = 200

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

_ALL: frozenset[Mode] = frozenset(Mode)
_P, _C, _S, _V, _D = Mode.PLANNER, Mode.CODER, Mode.SCAFFOLDER, Mode.VERIFIER, Mode.DEBUGGER


class Approval(StrEnum):
    """Whether a call needs a human before it runs."""

    NONE = "none"
    #: Depends on the arguments — a protected path, a new direct dependency, a
    #: history rewrite. Resolved by the tool's own ``needs_approval`` hook.
    CONDITIONAL = "conditional"
    ALWAYS = "always"


class Provider(StrEnum):
    """Which process executes the call.

    Recorded on the spec rather than inferred, because the two providers fail
    differently: a Python tool raises, a sidecar tool can be *absent* — the
    binary missing, the wrong version, the process dead. The router needs to
    know which failure it is looking at before it can say anything useful.
    """

    PYTHON = "python"
    GOTOOLS = "gotools"
    GOPLS = "gopls"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool, as the model sees it and as the router enforces it."""

    name: str
    #: Written as an instruction to the model, not as prose about the tool.
    #: "Read a slice of a file" beats "This tool reads files" — the second wastes
    #: the tokens on grammar the model already knows.
    description: str
    parameters: dict[str, Any]
    modes: frozenset[Mode]
    mutates: bool = False
    approval: Approval = Approval.NONE
    provider: Provider = Provider.PYTHON
    #: What to use when this tool is refused, whatever the reason.
    instead: str = ""
    #: True for tools the verification gate runs on a fixed schedule and the
    #: model therefore never chooses. Part A section 9.3 specifies the gate as an
    #: ordered fail-fast sequence, which is a pipeline, not a menu — and a model
    #: that *chooses* whether to run `go vet` is one that sometimes does not.
    #: "The model forgot to verify" is the exact failure this design exists to
    #: prevent, so the gate does not ask.
    gate_only: bool = False
    #: Set when the tool is specified but not yet wired to an implementation.
    #: Kept in the registry so the catalog is the whole contract rather than
    #: only the finished part of it, and hidden from every mode's schema list so
    #: the model is never offered something that cannot run.
    unavailable: str = ""
    required: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"tool name {self.name!r} must be lower_snake_case")
        if len(self.description) > MAX_DESCRIPTION:
            raise ValueError(
                f"{self.name}: description is {len(self.description)} characters, "
                f"over C1's {MAX_DESCRIPTION}. Every turn pays for this text."
            )
        if not self.description.endswith("."):
            raise ValueError(f"{self.name}: description must be a complete sentence")
        props: Mapping[str, Any] = self.parameters.get("properties", {})
        if len(props) > MAX_PARAMS:
            raise ValueError(
                f"{self.name}: {len(props)} parameters, over C1's {MAX_PARAMS}. "
                "Split the tool or move the choice into an enum."
            )
        for param, schema in props.items():
            if not schema.get("description"):
                raise ValueError(f"{self.name}.{param}: every parameter needs a description")
        for param in self.required:
            if param not in props:
                raise ValueError(f"{self.name}: required parameter {param!r} is not declared")
        if not self.modes and not self.gate_only:
            raise ValueError(f"{self.name}: a tool visible in no mode is dead code")
        if self.mutates and self.approval is Approval.NONE and not self.unavailable:
            # Not a blanket ban: gofmt and govalid_gen mutate and are safe,
            # because both are regenerations whose output is determined by
            # files already on disk. Anything else needs a reason recorded.
            if self.name not in _SAFE_MUTATORS:
                raise ValueError(
                    f"{self.name} mutates with no approval. Add it to _SAFE_MUTATORS "
                    "with a comment, or give it an approval class."
                )

    def visible_in(self, mode: Mode) -> bool:
        """Whether the model is offered this tool in this mode.

        Gate-only tools are dispatchable — the router runs them — but never
        appear in a schema list, so they cost no prompt tokens in any mode.
        """
        return not self.unavailable and not self.gate_only and mode in self.modes

    def schema(self) -> dict[str, Any]:
        """The OpenAI function-calling shape sent in ``tools``."""
        params = dict(self.parameters)
        params.setdefault("type", "object")
        params["required"] = list(self.required)
        params.setdefault("additionalProperties", False)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }


#: Mutating tools that need no approval, each with the reason it is safe.
_SAFE_MUTATORS = frozenset(
    {
        # Rewrites only whitespace, and only in files already being edited.
        "gofmt",
        # Regenerates *_validator.go from the request structs. The output is a
        # pure function of files on disk, and NOT running it is the failure mode
        # (validation silently not firing).
        "govalid_gen",
        # AST insertion into bootstrapper.go. Mutates a protected path, but a
        # scaffolded resource that is not wired does not compile, so requiring
        # approval here would make the common path a two-step interaction for no
        # decision the developer can meaningfully make.
        "fx_wire",
    }
)


def _obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(props)}


def _str(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **extra}


def _int(desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **extra}


# ── the catalog ─────────────────────────────────────────────────────────────
#
# Ordered as Part A section 7.2 lists them: read-only first, then mutating, then
# the gate commands, then git. The order is what the model sees, and the first
# few schemas are the ones it reaches for.

_SPECS: tuple[ToolSpec, ...] = (
    # -- orientation --------------------------------------------------------
    ToolSpec(
        name="repo_map",
        description=(
            "Get the module path, package tree, exported symbols and FX providers. "
            "Call this first in an unfamiliar repository. Pass package to see one "
            "directory in full."
        ),
        parameters=_obj(
            package=_str("Directory to expand in full, e.g. 'handler'. Omit for the whole tree."),
            max_tokens=_int("Budget for the map. Defaults to 4000.", minimum=500, maximum=20000),
        ),
        modes=_ALL,
        provider=Provider.GOTOOLS,
    ),
    ToolSpec(
        name="read_file",
        description=(
            "Read a slice of one file. Always pass start and end when you know roughly "
            "where to look; whole-file reads crowd out everything else in context."
        ),
        parameters=_obj(
            path=_str("Workspace-relative path, e.g. 'handler/user.go'."),
            start=_int("First line, 1-based.", minimum=1),
            end=_int("Last line, inclusive.", minimum=1),
        ),
        required=("path",),
        modes=_ALL,
    ),
    ToolSpec(
        name="search_repo",
        description=(
            "Search file contents by regular expression. Use this instead of grep, and "
            "prefer it over reading files to find something."
        ),
        parameters=_obj(
            pattern=_str("Regular expression, e.g. 'func .*Handler.*Routes'."),
            glob=_str("Restrict to matching paths, e.g. 'handler/**/*.go'."),
            max=_int("Maximum matches to return. Defaults to 40.", minimum=1, maximum=200),
        ),
        required=("pattern",),
        modes=frozenset({_P, _C, _V, _D}),
    ),
    ToolSpec(
        name="search_docs",
        description=(
            "Search the n-api-template knowledge base for the contract rule behind a "
            "pattern. Use it before inventing an approach, not after."
        ),
        parameters=_obj(
            query=_str("What you need to know, e.g. 'repository timeout constants'."),
        ),
        required=("query",),
        modes=frozenset({_P, _C, _S, _D}),
    ),
    # -- the compiler and the linter ---------------------------------------
    ToolSpec(
        name="go_symbols",
        description=(
            "Find a symbol's definition, references or package API through gopls. Use "
            "this rather than searching for a name textually."
        ),
        parameters=_obj(
            query=_str("Symbol or package, e.g. 'serverHandler.Handler'."),
            kind=_str(
                "One of: search, references, package_api.",
                enum=["search", "references", "package_api"],
            ),
        ),
        required=("query",),
        modes=frozenset({_P, _C, _V, _D}),
        provider=Provider.GOPLS,
        instead="use search_repo for a textual search",
        unavailable=(
            "gopls is not yet wired (Part A section 8.3). Use search_repo, or go_build "
            "for type errors."
        ),
    ),
    ToolSpec(
        name="go_diagnostics",
        description=(
            "Type-check the workspace incrementally and report errors. This is the fast "
            "inner-loop signal; run it after every edit batch."
        ),
        parameters=_obj(
            path=_str("Narrow to one file. Omit for the whole workspace."),
        ),
        modes=frozenset({_C, _V, _D}),
        provider=Provider.GOPLS,
        instead="use go_build, which is authoritative but slower",
        unavailable=(
            "gopls is not yet wired (Part A section 8.3). Use go_build, which is "
            "authoritative but takes about four seconds."
        ),
    ),
    ToolSpec(
        name="rules_lint",
        description=(
            "Check Go against the n-api-template contract: layer boundaries, handler "
            "signatures, repository idiom, FX registration. Pass paths to scope it."
        ),
        parameters=_obj(
            paths=_str("Comma-separated paths to lint. Omit for the whole workspace."),
            only=_str("Comma-separated rule ids, e.g. 'layer-sql-boundary,handler-signature'."),
        ),
        modes=frozenset({_P, _C, _V, _D}),
        provider=Provider.GOTOOLS,
    ),
    ToolSpec(
        name="legacy_audit",
        description=(
            "Detect pre-template patterns in an existing service: routes.go, gin, "
            "hand-rolled SQL builders, manual validation. Run before migrating."
        ),
        parameters=_obj(
            paths=_str("Comma-separated paths to audit. Omit for the whole workspace."),
        ),
        modes=frozenset({_P, _V}),
        provider=Provider.GOTOOLS,
    ),
    ToolSpec(
        name="playbook",
        description=(
            "Get the known-good fix procedure for a failure class or rule id. Consult "
            "this before attempting a fix you have not made before."
        ),
        parameters=_obj(
            rule=_str("Rule id or failure class, e.g. 'fx-registration' or 'pgx-no-rows'."),
        ),
        modes=frozenset({_C, _V, _D}),
    ),
    # -- editing ------------------------------------------------------------
    ToolSpec(
        name="write_file",
        description=(
            "Create a new file. Refuses to overwrite an existing one — use patch_file "
            "for that. Write complete, compiling Go, not a sketch."
        ),
        parameters=_obj(
            path=_str("Workspace-relative path for the new file."),
            content=_str("Full file content."),
        ),
        required=("path", "content"),
        modes=frozenset({_C, _S, _D}),
        mutates=True,
        approval=Approval.CONDITIONAL,
        instead="use patch_file to change a file that already exists",
    ),
    ToolSpec(
        name="patch_file",
        description=(
            "Replace an exact unique string in a file. Include enough surrounding lines "
            "to make old unique; the call fails rather than guessing."
        ),
        parameters=_obj(
            path=_str("Workspace-relative path of the file to change."),
            old=_str("Exact text to replace, unique within the file."),
            new=_str("Replacement text."),
        ),
        required=("path", "old", "new"),
        modes=frozenset({_C, _D}),
        mutates=True,
        approval=Approval.CONDITIONAL,
        instead="use write_file to create a file that does not exist yet",
    ),
    ToolSpec(
        name="delete_file",
        description="Delete a file. Always needs the developer's approval; say why in reason.",
        parameters=_obj(
            path=_str("Workspace-relative path to delete."),
            reason=_str("One sentence on why it should go."),
        ),
        required=("path", "reason"),
        modes=frozenset({_C, _D}),
        mutates=True,
        approval=Approval.ALWAYS,
    ),
    ToolSpec(
        name="gofmt",
        description=(
            "Format Go files and fix their imports. Runs automatically after edits; call "
            "it directly only to clean up a file you did not just touch."
        ),
        parameters=_obj(
            paths=_str("Comma-separated paths. Omit for files changed this session."),
        ),
        modes=frozenset(),
        gate_only=True,
        mutates=True,
    ),
    # -- scaffolding --------------------------------------------------------
    ToolSpec(
        name="resource_scaffold",
        description=(
            "Write a whole CRUD resource — domain, DDL, repository, DTOs, handler — from "
            "a field spec. Produce the spec; the templates produce the code."
        ),
        parameters=_obj(
            spec=_str('Resource as JSON: {"name","table","route_base","fields","operations"}.'),
        ),
        required=("spec",),
        modes=frozenset({_S}),
        mutates=True,
        approval=Approval.ALWAYS,
        provider=Provider.GOTOOLS,
    ),
    ToolSpec(
        name="project_scaffold",
        description=(
            "Create a new n-api-template service in an empty directory, with configs, "
            "bootstrap and one working resource. Credential fields are left empty."
        ),
        parameters=_obj(
            project=_str('Service as JSON: {"module": "gitlab.cept.gov.in/it-2.0/x-api"}.'),
            resource=_str("One resource to seed the service with, same shape as resource_scaffold."),
        ),
        required=("project", "resource"),
        modes=frozenset({_S}),
        mutates=True,
        approval=Approval.ALWAYS,
        provider=Provider.GOTOOLS,
    ),
    ToolSpec(
        name="fx_wire",
        description=(
            "Register a repository or handler in bootstrap/bootstrapper.go with the right "
            "annotations. Run this after adding either, or FX fails at startup."
        ),
        parameters=_obj(
            kind=_str("Either 'repo' or 'handler'.", enum=["repo", "handler"]),
            ctor=_str("The constructor's bare name, e.g. 'NewPensionHandler'."),
        ),
        required=("kind", "ctor"),
        modes=frozenset({_C, _S, _D}),
        mutates=True,
        provider=Provider.GOTOOLS,
    ),
    ToolSpec(
        name="govalid_gen",
        description=(
            "Regenerate handler/request_*_validator.go from the request structs. Run it "
            "whenever a validate tag changes; never hand-edit the generated files."
        ),
        parameters=_obj(),
        modes=frozenset({_C, _S, _D}),
        mutates=True,
    ),
    # -- the gate -----------------------------------------------------------
    ToolSpec(
        name="go_build",
        description=(
            "Build every package. This is the authoritative signal: nothing is done until "
            "it is clean, whatever the other tools say."
        ),
        parameters=_obj(),
        modes=frozenset({_C, _V, _D}),
    ),
    ToolSpec(
        name="go_vet",
        description=(
            "Run go vet over the workspace. Gate only — it takes about thirty seconds, so "
            "never run it in the edit loop."
        ),
        parameters=_obj(),
        modes=frozenset({_D}),
    ),
    ToolSpec(
        name="go_test",
        description="Run tests. Pass pattern to scope to one package when output is large.",
        parameters=_obj(
            pattern=_str("Package pattern, e.g. './handler/...'. Omit for './...'."),
            run=_str("Regular expression for -run, e.g. 'TestCreatePension'."),
        ),
        modes=frozenset({_D}),
    ),
    ToolSpec(
        name="golangci_lint",
        description=(
            "Run golangci-lint if the repository configures it. Advisory: its findings "
            "never block, so do not spend turns on them."
        ),
        parameters=_obj(),
        modes=frozenset(),
        gate_only=True,
    ),
    ToolSpec(
        name="govulncheck",
        description=(
            "Scan dependencies for known vulnerabilities. Run it on a new service and "
            "after any dependency change, not routinely."
        ),
        parameters=_obj(),
        modes=frozenset(),
        gate_only=True,
    ),
    ToolSpec(
        name="swagger_check",
        description=(
            "Check that routes are named and swagger generation is enabled, so endpoints "
            "reach /docs/v3Doc.json. This checks; it does not generate."
        ),
        parameters=_obj(),
        modes=frozenset(),
        gate_only=True,
    ),
    ToolSpec(
        name="go_mod",
        description=(
            "Run tidy, or add a dependency. Tidy is free and must be a no-op at the gate; "
            "adding a direct dependency needs approval."
        ),
        parameters=_obj(
            op=_str("One of: tidy, get, why.", enum=["tidy", "get", "why"]),
            pkg=_str("Module path, for get and why."),
            version=_str("Version for get, e.g. 'v1.4.0'. Omit for latest."),
        ),
        required=("op",),
        modes=frozenset({_C, _D}),
        mutates=True,
        approval=Approval.CONDITIONAL,
    ),
    # -- version control ----------------------------------------------------
    ToolSpec(
        name="git_status",
        description="List changed, staged and untracked files. Cheap; use it to confirm what you changed.",
        parameters=_obj(),
        modes=_ALL,
    ),
    ToolSpec(
        name="git_diff",
        description="Show the diff of the working tree, or of one path. Read this before claiming a change is done.",
        parameters=_obj(
            path=_str("Limit the diff to one path."),
            staged=_str("Pass 'true' to diff the index instead of the working tree."),
        ),
        modes=_ALL,
    ),
    ToolSpec(
        name="git_blame",
        description="Show who last changed each line of a file, and when. Use it to date a legacy pattern.",
        parameters=_obj(
            path=_str("Workspace-relative path."),
            start=_int("First line.", minimum=1),
            end=_int("Last line.", minimum=1),
        ),
        required=("path",),
        modes=frozenset({_P, _D}),
    ),
    ToolSpec(
        name="git_ops",
        description=(
            "Stage, commit, or switch to the session branch. Never pushes and never "
            "rewrites history, so nothing here can lose committed work."
        ),
        parameters=_obj(
            op=_str(
                "One of: branch, add, commit.",
                enum=["branch", "add", "commit"],
            ),
            paths=_str("Comma-separated paths for add. Omit to stage tracked changes."),
            message=_str("Commit message, for commit."),
        ),
        required=("op",),
        modes=frozenset({_C, _S}),
        mutates=True,
        approval=Approval.CONDITIONAL,
    ),
    # -- the escape hatch ---------------------------------------------------
    ToolSpec(
        name="run_terminal",
        description=(
            "Run one allow-listed binary with explicit arguments. There is no shell, so "
            "no pipes, globs or redirection. Prefer a purpose-built tool."
        ),
        parameters=_obj(
            argv=_str("Command and arguments, JSON array, e.g. '[\"go\",\"env\",\"GOPRIVATE\"]'."),
            timeout=_int("Seconds before it is killed. Defaults to 60.", minimum=1, maximum=600),
        ),
        required=("argv",),
        modes=frozenset({_D}),
        approval=Approval.CONDITIONAL,
    ),
)

REGISTRY: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}

if len(REGISTRY) != len(_SPECS):  # pragma: no cover - import-time guard
    raise ValueError("duplicate tool name in the registry")


def get(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)


def names_for(mode: Mode | str) -> tuple[str, ...]:
    """Tool names visible in a mode, in catalog order."""
    m = Mode(mode)
    return tuple(spec.name for spec in _SPECS if spec.visible_in(m))


def schemas_for(mode: Mode | str) -> list[dict[str, Any]]:
    """The ``tools`` array for a request in this mode.

    This is the first of the two locks. It is not defence in depth by accident:
    a model cannot call what it was never shown, and the router's check exists
    for the case where a stale schema list is replayed from an earlier turn.
    """
    m = Mode(mode)
    return [spec.schema() for spec in _SPECS if spec.visible_in(m)]


def gate_tools() -> tuple[str, ...]:
    """Tools the gate dispatches and the model never sees."""
    return tuple(spec.name for spec in _SPECS if spec.gate_only)


def unavailable() -> Iterator[ToolSpec]:
    """Specified tools with no implementation yet, for the startup report."""
    return (spec for spec in _SPECS if spec.unavailable)


def all_specs() -> Sequence[ToolSpec]:
    return _SPECS
