"""What a token may be used for (host-plan §8).

A person's token names no scope and may do anything a person may. Two kinds of
token are narrower, and say so in a ``scope`` claim:

**A runner's.** A hosted runner calls the model on its lease owner's behalf,
and a token minted *as the owner* is what makes quota and the ledger charge the
owner rather than one service account for every hosted run. But the runner is
where untrusted code runs, and a token stolen from it must not be a way to act
as the owner everywhere else: to start runs, or to open merge requests. So it
is delegated with ``llm`` and nothing else.

**A machine's.** A portal's backend or another agent cannot sign in with PKCE.
It gets a client-credentials token with the scopes it was registered with, so a
read-only dashboard cannot open merge requests or rewrite the shared agenda.

``delegate`` is the one scope a person's token never has: minting a runner's
token for someone else is the control plane's job, and nobody else's.
"""

from __future__ import annotations

from .tokens import Claims

__all__ = ["SCOPES", "SERVICE_ONLY", "allowed", "runtime_scope"]

#: Every scope, and what it opens.
SCOPES = {
    "llm": "the model proxy and the caller's own quota",
    "sessions:read": "read sessions, their events and their transcripts",
    "sessions:write": "start runs, send messages, answer approvals, stop runs",
    "workspaces:write": "lease and release workspaces",
    "agenda:write": "propose and move work on a workspace's agenda",
    "deliver:mr": "push a session's branch and open a merge request",
    "a2a": "call dakcoder as another agent",
    "delegate": "mint a runner's model-only token for a lease's owner",
}

#: Scopes only ever granted explicitly, never implied by a person's token.
SERVICE_ONLY = frozenset({"delegate"})


def allowed(claims: Claims, needed: str) -> bool:
    if claims.scopes is None:
        return needed not in SERVICE_ONLY
    return needed in claims.scopes


def runtime_scope(method: str, path: str) -> str:
    """The scope a request to the hosted side needs, from its method and path.

    ``path`` is relative to the hosted API, as the gateway forwards it
    (``v1/sessions/abc/deliver``).
    """
    method = method.upper()
    parts = path.strip("/").split("/")
    if parts[-1:] == ["deliver"]:
        return "deliver:mr"
    if parts[:2] == ["v1", "workspaces"] and (
        (method == "POST" and len(parts) == 2) or (method == "DELETE" and len(parts) == 3)
    ):
        return "workspaces:write"
    if "agenda" in parts and method == "POST":
        return "agenda:write"
    if method == "GET":
        return "sessions:read"
    return "sessions:write"
