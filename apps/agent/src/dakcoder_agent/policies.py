"""Approval policies for a run with nobody to ask (host-plan §10).

``interactive`` is the default and the local mode's only policy: an approval
waits for a person. ``auto_safe`` is for callers that cannot answer one, an
agent calling over A2A or a scheduled job, and decides each approval at once,
by rule:

* **Refused**: anything that touches a protected path (``PROTECTED_GLOBS``:
  generated or structural files), anything the router says must always ask
  (``delete_file``), and adding a dependency, which the router says is
  "allow-listed and reviewed, not added mid-task". Nobody reviews an
  unattended run, so none of these may happen in one.
* **Approved**: the rest. A scaffold or a commit on unprotected paths is the
  work an unattended caller asked for, and refusing it would make the policy a
  way of doing nothing. The runner's container, not this rule, is what bounds
  what an approved call can reach.

Every decision is recorded in the transcript as a ``gate`` event of kind
``auto_approval`` with its reason, so a run nobody watched can still be read.

Decided in the runtime's approval callback, which is the seam an approval
already passes through, and not as a branch in the loop: the loop asks whether
a call may run, and neither knows nor cares who answers.
"""

from __future__ import annotations

from .tools.router import ApprovalPolicy, ApprovalRequest

__all__ = ["AUTO_SAFE", "INTERACTIVE", "POLICIES", "auto_safe"]

INTERACTIVE = "interactive"
AUTO_SAFE = "auto_safe"
POLICIES = (INTERACTIVE, AUTO_SAFE)

#: Never approved without a person, whatever the policy.
_ALWAYS_ASK = ApprovalPolicy().always_ask


def auto_safe(request: ApprovalRequest) -> tuple[bool, str]:
    """Whether ``auto_safe`` approves this call, and the one sentence why."""
    protected = [p for p in request.as_dict()["protected"]]
    if protected:
        return False, (
            f"auto_safe refused {request.tool}: it touches {', '.join(protected)}, which is "
            "protected, and an unattended run never changes a protected file"
        )
    if request.tool in _ALWAYS_ASK:
        return False, (
            f"auto_safe refused {request.tool}: it always needs a person's approval, and "
            "this run has nobody to ask"
        )
    if request.tool == "go_mod" and request.arguments.get("op") == "get":
        return False, (
            f"auto_safe refused go_mod get {request.arguments.get('pkg', '')}: new "
            "dependencies are reviewed before they are added, and nobody reviews this run"
        )
    paths = ", ".join(request.paths) or "no files"
    return True, f"auto_safe approved {request.tool} on {paths}: nothing protected"
