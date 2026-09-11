"""The message shape, and the layers it is filed under.

Split out of ``context.py`` when the canonical transcript arrived, because
three modules now need it and the one that owns it must not be the one that
imports the other two. ``transcript.Record`` is what happened; ``Message`` is
what a projection of it looks like on the way to the model. They are separate
types on purpose: a ``Message`` may be elided, stubbed or synthesised, and a
``Record`` never is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from dakcoder_shared.llm import ToolCall

__all__ = [
    "Layer",
    "Message",
    "PINNED_LAYERS",
    "Role",
    "contains",
    "parseable_arguments",
]


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Layer(StrEnum):
    """Eviction priority, in the order Part A §6.1 allocates them.

    Listed lowest-priority first: the working set is what compaction consumes,
    and the pinned layers are the last things standing.
    """

    WORKING_SET = "working_set"
    RECAP = "recap"
    TASK = "task"
    #: The plan and the developer's directives. Pinned like ``TASK``, and
    #: assembled *after* the working set rather than before it — see
    #: ``ContextManager.build`` for the measurement that put it there.
    DIRECTIVE = "directive"
    MODE = "mode"
    SYSTEM = "system"


#: Layers that are never evicted. The task and the acceptance criteria are what
#: the whole run is measured against; an agent that compacts away what it was
#: asked to do will confidently finish something else.
PINNED_LAYERS = frozenset({Layer.SYSTEM, Layer.MODE, Layer.TASK, Layer.DIRECTIVE})


@dataclass(frozen=True, slots=True)
class Message:
    """One message, as the model will see it.

    Frozen on purpose. §6.4's rule — the message list is append-only below the
    pinned head, and any mutation of ``messages[0..k]`` is a cache-invalidating
    bug — is easy to state and easy to violate by accident three refactors
    later. Immutability makes the accident impossible rather than merely
    detectable.

    Since the canonical transcript arrived, a ``Message`` is *derived*: the
    projection builds it from a ``Record`` on every turn. ``seq`` carries the
    record it came from, which is what lets a caller ask "what is actually
    behind this, in full" without the projection having to carry the full text.
    """

    role: Role
    content: str
    layer: Layer = Layer.WORKING_SET
    #: Provenance, for the context inspector (Part B §10.2) and for the ledger.
    source: str = ""
    #: Set on tool results, so a stale slice can be found and replaced.
    path: str | None = None
    #: The lines this message actually carries, after any elision — not the
    #: lines the tool returned. Held because superseding on ``path`` alone
    #: discards content the model cannot get back.
    line_range: tuple[int, int] | None = None
    tool_call_id: str | None = None
    #: The calls an assistant message made, so its own actions survive into the
    #: next request. Without these every ``role: "tool"`` message that follows
    #: refers to a ``tool_call_id`` no assistant message on the wire declares.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Turn this message was appended on, for the recap and the inspector.
    turn: int = 0
    #: The canonical record this was projected from. ``-1`` for anything the
    #: projection or the pinned head synthesised, which is the honest answer:
    #: nothing in the transcript says it.
    seq: int = -1

    def wire(self) -> dict[str, Any]:
        """Render to the OpenAI chat shape, dropping our own bookkeeping.

        The assistant's ``tool_calls`` are part of the shape, not bookkeeping.
        Omitting them left every tool result orphaned -- a ``tool_call_id``
        matching no call anywhere in the request, which a strict endpoint
        rejects outright and a lenient one simply cannot make sense of. It also
        rewrote the model's own history: each of its turns came back as a
        paragraph of prose, with results appearing beside them unexplained, so
        the conversation contained no example of the very message shape the
        model was being asked to produce.
        """
        out: dict[str, Any] = {"role": str(self.role), "content": self.content}
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": parseable_arguments(call.arguments),
                    },
                }
                for call in self.tool_calls
            ]
        return out


def contains(outer: tuple[int, int] | None, inner: tuple[int, int] | None) -> bool:
    """Whether a read covering ``outer`` makes one covering ``inner`` redundant.

    ``None`` is the whole file, so it contains everything and is contained only
    by another whole-file read. Ranges are the *clamped* ones the tool actually
    returned, not what the model asked for: ``end=99999`` on a 200-line file is
    ``(1, 200)``, and comparing the raw arguments would call it disjoint from a
    later whole-file read that in fact supersedes it.
    """
    if outer is None:
        return True
    if inner is None:
        return False
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def parseable_arguments(arguments: str) -> str:
    """The call's arguments, guaranteed to parse as a JSON object.

    vLLM's chat renderer runs ``json.loads`` over every recorded tool call's
    arguments before templating, so ONE unparseable string in history rejects
    every subsequent request with a 400 -- including a plain "hi", because the
    poisoned message is still there. The field case was a reply cut off by the
    output budget mid-call: arguments arrived as the bare character ``{``, the
    loop (correctly) told the model about the truncation and never dispatched
    the call, but the recorded message re-sent ``{`` on every turn thereafter
    and the endpoint refused them all. json.loads("{") raises the exact error
    the server logged: "Expecting property name enclosed in double quotes:
    line 1 column 2 (char 1)".

    Sanitised at render time rather than at append time, deliberately: the
    canonical transcript keeps what the model actually sent (the loop's own
    bookkeeping fingerprints the raw string), and a session that already
    recorded a truncated call before this fix heals itself on its next request
    instead of staying bricked.

    The substitute is ``{}``, not a repair attempt. The tool result beside the
    call already tells the model the call was cut off and never ran; inventing
    completed-looking arguments would put words in its mouth.
    """
    try:
        if isinstance(json.loads(arguments), dict):
            return arguments
    except (json.JSONDecodeError, TypeError):
        pass
    return "{}"
