"""Publish the model-facing tool catalogue: contract C1, in full.

``gotools`` already publishes its own seven tools. That is the *sidecar's* half
of C1 — the schemas the Go process accepts. This is the other half and the larger
one: the twenty-nine tools the model is actually offered, which mode each belongs
to, which need approval, and which are specified but not yet wired.

The distinction matters because the two shapes are allowed to differ. The
model-facing schema is bound by C1's six-parameter limit and by what a model can
be expected to get right in one attempt; the sidecar's is bound only by Go's
struct tags. ``rules_lint`` takes a comma-separated string here and an array
there, on purpose. Publishing only the sidecar's half would document the seam
from the wrong side.

Generated, never edited. The check mode is what makes that true rather than
aspirational: CI fails when the file on disk disagrees with the registry, so the
catalogue cannot quietly drift from the code the way a hand-written table does.
"""

from __future__ import annotations

import json
from typing import Any

from ..modes import Mode
from . import registry
from .registry import Approval, Provider, ToolSpec

__all__ = ["as_json", "as_markdown", "conformance"]

CONTRACT = "C1"


def conformance() -> list[str]:
    """C1 violations in the registry, as human sentences.

    The registry already refuses to construct a violating spec, so this should
    always be empty. It exists because "enforced at import" is a claim, and a
    claim about a safety property is worth a second, independent check that does
    not share the first one's code path.
    """
    problems: list[str] = []
    for spec in registry.all_specs():
        params = spec.parameters.get("properties", {})
        if len(params) > registry.MAX_PARAMS:
            problems.append(f"{spec.name}: {len(params)} parameters, over C1's {registry.MAX_PARAMS}")
        if len(spec.description) > registry.MAX_DESCRIPTION:
            problems.append(
                f"{spec.name}: description is {len(spec.description)} characters, "
                f"over C1's {registry.MAX_DESCRIPTION}"
            )
        for name, schema in params.items():
            if not schema.get("description"):
                problems.append(f"{spec.name}.{name}: no description")
        if spec.mutates and spec.approval is Approval.NONE and spec.name not in registry._SAFE_MUTATORS:
            problems.append(f"{spec.name}: mutates with no approval and no recorded justification")
    return problems


def as_json(version: str = "dev") -> str:
    tools = []
    for spec in registry.all_specs():
        entry: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.schema()["function"]["parameters"],
            "modes": sorted(str(m) for m in spec.modes),
            "mutates": spec.mutates,
            "approval": str(spec.approval),
            "provider": str(spec.provider),
        }
        if spec.gate_only:
            entry["gate_only"] = True
        if spec.unavailable:
            entry["unavailable"] = spec.unavailable
        if spec.instead:
            entry["instead"] = spec.instead
        tools.append(entry)

    payload = {
        "component": "dakcoder-agent",
        "version": version,
        "contract": CONTRACT,
        "limits": {"max_params": registry.MAX_PARAMS, "max_description": registry.MAX_DESCRIPTION},
        "visible_per_mode": {
            str(mode): list(registry.names_for(mode)) for mode in Mode
        },
        "tools": tools,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def as_markdown(version: str = "dev") -> str:
    lines = [
        "# Tool catalogue — the model-facing contract",
        "",
        "> **Generated.** Do not edit. Run `make tool-catalog` and commit the result.",
        "> Regenerating is how this file stays true; editing it is how it stops being.",
        "",
        "Contract **C1** (plan.md §7) in full: every tool the model can be offered, "
        "which modes see it, and what it costs to run.",
        "",
        f"C1 limits: at most **{registry.MAX_PARAMS} parameters** per tool, description at "
        f"most **{registry.MAX_DESCRIPTION} characters**, written as an instruction to the "
        "model rather than documentation for a human. Enforced when the registry is "
        "imported, so a violating tool cannot reach a test run.",
        "",
        "`gotools` publishes the *sidecar's* schemas separately. The two differ on "
        "purpose — `rules_lint` takes a comma-separated string here and an array there — "
        "because only this side is bound by the six-parameter limit.",
        "",
        "## What each mode is offered",
        "",
        "Mode filtering is a guarantee, not a hint: a tool absent from this table is "
        "absent from that mode's schema list *and* refused by the router if called anyway.",
        "",
        "| Mode | Tools | Schema cost |",
        "|---|---|---|",
    ]

    from dakcoder_shared.tokens import estimate_tokens

    for mode in Mode:
        names = registry.names_for(mode)
        cost = estimate_tokens(json.dumps(registry.schemas_for(mode)))
        lines.append(f"| **{mode}** | {len(names)} | ~{cost:,} tokens |")

    lines += [
        "",
        "## The catalogue",
        "",
        "| Tool | Modes | Mutates | Approval | Runs in | Description |",
        "|---|---|---|---|---|---|",
    ]

    for spec in registry.all_specs():
        modes = "gate" if spec.gate_only else "".join(
            sorted(str(m)[0].upper() for m in spec.modes)
        )
        approval = {
            Approval.NONE: "",
            Approval.CONDITIONAL: "if protected",
            Approval.ALWAYS: "**always**",
        }[spec.approval]
        provider = {
            Provider.PYTHON: "agent",
            Provider.GOTOOLS: "gotools",
            Provider.GOPLS: "gopls",
        }[spec.provider]
        note = f" _(not yet available: {spec.unavailable})_" if spec.unavailable else ""
        lines.append(
            f"| `{spec.name}` | {modes} | {'✓' if spec.mutates else ''} | {approval} | "
            f"{provider} | {spec.description}{note} |"
        )

    lines += [
        "",
        "Modes: **P**lanner · **C**oder · **S**caffolder · **V**erifier · **D**ebugger. "
        "`gate` means the verification gate runs it on a fixed schedule and the model "
        "never chooses it (Part A §9.3).",
        "",
        "## Parameters",
        "",
    ]

    for spec in registry.all_specs():
        lines.append(f"### `{spec.name}`")
        lines.append("")
        lines.append(spec.description)
        lines.append("")
        props = spec.parameters.get("properties", {})
        if not props:
            lines.append("_No parameters._")
            lines.append("")
            continue
        lines.append("| Parameter | Type | Required | Description |")
        lines.append("|---|---|---|---|")
        for name, schema in props.items():
            kind = schema.get("type", "string")
            if schema.get("enum"):
                kind += " (" + " \\| ".join(schema["enum"]) + ")"
            required = "yes" if name in spec.required else ""
            lines.append(f"| `{name}` | {kind} | {required} | {schema['description']} |")
        lines.append("")

    problems = conformance()
    if problems:
        lines += ["## C1 violations", ""] + [f"- {p}" for p in problems] + [""]

    return "\n".join(lines)
