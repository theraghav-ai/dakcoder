"""The control plane (host-plan §3): the hosted half of dakcoder.

The gateway fronts it; it fronts one runner per workspace. It owns what a
per-workspace runner cannot: who holds which workspace (leases), which runner
holds which session (the registry), the repository's remote (clone, push, merge
request), and the limits that apply across all of them.

    POST   /v1/workspaces                  lease a server-side clone
    GET    /v1/workspaces                  the caller's leases
    DELETE /v1/workspaces/{wid}            release one
    POST   /v1/workspaces/{wid}/tasks      start a run on it
    *      /v1/workspaces/{wid}/agenda...  its backlog
    GET    /v1/sessions                    the caller's sessions, every workspace
    *      /v1/sessions/{id}...            forwarded to the session's runner
    POST   /v1/sessions/{id}/deliver       push its branch and open a merge request
    *      /v1/approvals...                across the caller's runners
    POST   /v1/a2a                         agent-to-agent (JSON-RPC)
"""
