"""What the control plane does, apart from HTTP.

**The working tree's discipline.** A lease's runner works in one working copy,
and sessions on it run one at a time. Each session's changes are its own:

* When a session starts, whatever the previous one left in the working tree is
  committed to the previous session's branch in the lease's mirror, and the
  tree is put back to the base. Nothing is lost, and the new session starts
  from the base, not from someone else's half-finished change.
* When a session is delivered, its changes are committed to its branch (from
  the tree if it is the lease's latest session, already there if not), the
  branch is pushed, and a merge request is opened or updated.

Every git operation here goes through ``repos.Git``, which never runs git
against a ``.git`` the runner can write.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from .config import Settings
from .credentials import Credentials
from .gitlab import GitLab, GitLabError
from .repos import Allowlist, Git, GitError, NotAllowed, project_path
from .runners import Runner, RunnerFailed, Runners
from .store import Delivery, Lease, SessionRow, Store

__all__ = ["Refused", "Service"]

log = logging.getLogger(__name__)


def remove_tree(path: Path) -> None:
    """Delete a lease's directory, read-only files included.

    Git writes its objects read-only, and on Windows that makes ``rmtree``
    fail on every one of them. With ``ignore_errors`` it failed silently and
    left the clone, and the disk it was released to free, where it was.
    """
    import os
    import stat

    def retry(function, target, _exc) -> None:
        os.chmod(target, stat.S_IWRITE)
        function(target)

    if path.exists():
        shutil.rmtree(path, onexc=retry)


def _discard(path: Path) -> None:
    """``remove_tree`` on a path already being given up on: never raises over the
    error that caused it."""
    try:
        remove_tree(path)
    except OSError:
        log.warning("could not remove %s", path, exc_info=True)


class Refused(Exception):
    """A request refused, with the status and the one sentence that says why."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _missing_session(session_id: str) -> Refused:
    # The runtime's own wording, so a session that is not the caller's reads the
    # same from here as from a runner: as one that does not exist (§8).
    return Refused(404, f"no session {session_id}")


class Service:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        allowlist: Allowlist,
        git: Git,
        runners: Runners,
        gitlab: GitLab | None = None,
        *,
        credentials: Credentials | None = None,
        clock=time.time,
    ) -> None:
        self.settings = settings
        self.store = store
        self.allowlist = allowlist
        self.git = git
        self.runners = runners
        self.gitlab = gitlab
        self.clock = clock
        self.credentials = credentials or Credentials(
            settings.gateway_url, delegator_jwt=settings.gateway_jwt, static=settings.runner_jwt
        )
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, lease_id: str) -> asyncio.Lock:
        return self._locks.setdefault(lease_id, asyncio.Lock())

    def _dir(self, lease: Lease) -> Path:
        return Path(lease.path)

    # -- leases -------------------------------------------------------------

    async def lease(self, sub: str, repo_url: str, ref: str) -> Lease:
        repo_url, ref = repo_url.strip(), (ref or "").strip() or "main"
        if not repo_url:
            raise Refused(400, "repo_url is required")
        try:
            self.allowlist.check(repo_url, sub, date.today())
        except NotAllowed as exc:
            raise Refused(403, str(exc)) from None
        held = self.store.leases(sub)
        if len(held) >= self.settings.max_leases_per_user:
            raise Refused(
                429,
                f"you hold {len(held)} workspace(s), the most one person may; release one first",
            )

        lease_id = uuid.uuid4().hex[:12]
        lease_dir = self.settings.leases_dir / lease_id
        try:
            await asyncio.to_thread(self.git.clone, repo_url, ref, lease_dir)
        except GitError as exc:
            _discard(lease_dir)
            raise Refused(502, f"the repository could not be cloned: {exc}") from None
        size = await asyncio.to_thread(self.git.size, lease_dir)
        if size > self.settings.max_lease_bytes:
            _discard(lease_dir)
            raise Refused(413, f"the repository is {size:,} bytes, over this deployment's limit")

        now = self.clock()
        lease = Lease(
            id=lease_id,
            owner=sub,
            repo_url=repo_url,
            ref=ref,
            path=str(lease_dir),
            created_at=now,
            expires_at=now + self.settings.lease_ttl.total_seconds(),
            last_used_at=now,
        )
        self.store.add_lease(lease)
        log.info("lease %s: %s@%s for %s", lease_id, repo_url, ref, sub)
        return lease

    def owned_lease(self, sub: str, lease_id: str) -> Lease:
        lease = self.store.lease(lease_id, sub)
        if lease is None:
            raise Refused(404, f"no workspace {lease_id}")
        return lease

    async def release(self, sub: str, lease_id: str, *, force: bool = False) -> None:
        """Stop the lease's runner, archive its sessions, delete its clone."""
        lease = self.owned_lease(sub, lease_id)
        async with self._lock(lease_id):
            if not force and await self._running_on(lease, sub):
                raise Refused(409, "a session is running on that workspace; abort it first")
            await self._retire(lease)

    async def _retire(self, lease: Lease) -> None:
        await self.runners.stop(lease.id)
        await asyncio.to_thread(self.archive, lease)
        remove_tree(self._dir(lease))
        self.store.remove_lease(lease.id)
        log.info("lease %s released", lease.id)

    def archive(self, lease: Lease) -> Path | None:
        """Keep the lease's sessions after its clone is gone (host-plan §9.1).

        The journals, transcripts and plans the runtime wrote under
        ``.dakcoder/sessions`` are what a finished run *is*; the clone is only
        where it happened. Copied to the archive before the clone is deleted.
        """
        source = Git.worktree(self._dir(lease)) / ".dakcoder" / "sessions"
        if not source.is_dir():
            return None
        target = self.settings.archive_dir / lease.owner.replace("/", "_") / lease.id
        shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        return target

    # -- runners ------------------------------------------------------------

    async def runner_for(self, lease: Lease) -> Runner:
        try:
            runner = await self.runners.ensure(
                lease.id,
                Git.worktree(self._dir(lease)),
                lambda: self.credentials.for_owner(lease.owner),
            )
        except RunnerFailed as exc:
            raise Refused(503, f"the workspace's runner could not be started: {exc}") from None
        self.store.touch_lease(lease.id, self.clock())
        return runner

    async def _running_on(self, lease: Lease, sub: str) -> list[dict[str, Any]]:
        runner = self.runners.get(lease.id)
        if runner is None:
            return []
        status, body = await runner.upstream.call("GET", "v1/sessions", sub=sub, query="status=running")
        return list(body.get("sessions", [])) if status == 200 else []

    async def _running_for(self, sub: str) -> int:
        count = 0
        for lease in self.store.leases(sub):
            count += len(await self._running_on(lease, sub))
        return count

    # -- tasks --------------------------------------------------------------

    async def start_task(self, sub: str, lease_id: str, body: dict[str, Any]) -> tuple[int, Any]:
        lease = self.owned_lease(sub, lease_id)
        if await self._running_for(sub) >= self.settings.max_running_per_user:
            raise Refused(
                429,
                f"you have {self.settings.max_running_per_user} run(s) going, the most one "
                "person may; wait for one to finish",
            )
        async with self._lock(lease_id):
            runner = await self.runner_for(lease)
            if await self._running_on(lease, sub):
                raise Refused(
                    409,
                    "a session is already running on this workspace. Runs on one workspace "
                    "share its working tree and go one at a time; lease the repository again "
                    "to run two at once",
                )
            await asyncio.to_thread(self._settle, lease)
            status, session = await runner.upstream.call("POST", "v1/tasks", sub=sub, json=body)
            if status == 200 and isinstance(session, dict) and session.get("id"):
                self.store.add_session(
                    SessionRow(
                        id=str(session["id"]),
                        lease_id=lease.id,
                        owner=sub,
                        branch=f"dakcoder/{session['id']}",
                        task=str(body.get("task", ""))[:500],
                        created_at=self.clock(),
                        summary=session,
                    )
                )
                session = {**session, "workspace_id": lease.id}
            return status, session

    def _settle(self, lease: Lease) -> None:
        """Commit what the previous session left to its branch, and reset the tree."""
        lease_dir = self._dir(lease)
        base = self.git.base(lease_dir, lease.ref)
        if not self.git.changed(lease_dir, base):
            return
        previous = self._latest(lease)
        branch = previous.branch if previous else f"dakcoder/unclaimed-{int(self.clock())}"
        label = f"session {previous.id}: {previous.task[:60]}" if previous else "unclaimed changes"
        self.git.snapshot(
            lease_dir, branch=branch, base=base, message=f"dakcoder {label} (not yet delivered)"
        )
        self.git.reset(lease_dir, base)

    def _latest(self, lease: Lease) -> SessionRow | None:
        rows = sorted(self.store.lease_sessions(lease.id), key=lambda r: r.created_at)
        return rows[-1] if rows else None

    # -- sessions -----------------------------------------------------------

    def owned_session(self, sub: str, session_id: str) -> tuple[SessionRow, Lease]:
        row = self.store.session(session_id, sub)
        if row is None:
            raise _missing_session(session_id)
        lease = self.store.lease(row.lease_id, sub)
        if lease is None:
            raise Refused(410, "that session's workspace has been released; its record is archived")
        return row, lease

    async def session_runner(self, sub: str, session_id: str) -> Runner:
        _, lease = self.owned_session(sub, session_id)
        return await self.runner_for(lease)

    async def sessions(
        self,
        sub: str,
        workspace: str | None = None,
        *,
        limit: int | None = None,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.store.sessions(sub, workspace, limit=limit, before=before)
        for lease_id in {r.lease_id for r in rows}:
            runner = self.runners.get(lease_id)
            if runner is None:
                continue
            status, body = await runner.upstream.call("GET", "v1/sessions", sub=sub)
            if status == 200:
                for live in body.get("sessions", []):
                    self.store.remember(str(live.get("id")), live)
        rows = self.store.sessions(sub, workspace, limit=limit, before=before)
        return [
            {**row.summary, "id": row.id, "task": row.task or row.summary.get("task", ""),
             "workspace_id": row.lease_id, "branch": row.branch, "listed_at": row.created_at}
            for row in rows
        ]

    async def forget(self, sub: str, session_id: str) -> None:
        self.store.forget_session(session_id)

    # -- approvals ----------------------------------------------------------

    async def approvals(self, sub: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for lease in self.store.leases(sub):
            runner = self.runners.get(lease.id)
            if runner is None:
                continue
            status, body = await runner.upstream.call("GET", "v1/approvals", sub=sub)
            if status == 200:
                found += [{**a, "workspace_id": lease.id} for a in body.get("approvals", [])]
        return found

    async def approval_runner(self, sub: str, approval_id: str) -> Runner:
        for lease in self.store.leases(sub):
            runner = self.runners.get(lease.id)
            if runner is None:
                continue
            status, body = await runner.upstream.call("GET", "v1/approvals", sub=sub)
            if status == 200 and any(a.get("id") == approval_id for a in body.get("approvals", [])):
                return runner
        # The runtime's own answer for an approval that is gone, and for one
        # that was never the caller's.
        raise Refused(410, "that approval is no longer pending")

    # -- delivery (§7.4) ----------------------------------------------------

    async def deliver(
        self, sub: str, session_id: str, *, title: str, description: str, override: str = ""
    ) -> dict[str, Any]:
        row, lease = self.owned_session(sub, session_id)
        if not title.strip():
            raise Refused(400, "a delivery needs a title: it becomes the merge request's")
        async with self._lock(lease.id):
            runner = await self.runner_for(lease)
            status, session = await runner.upstream.call("GET", f"v1/sessions/{session_id}", sub=sub)
            if status != 200:
                raise Refused(status, str(session.get("error", "the session could not be read")))
            outcome = str(session.get("status", ""))
            if outcome == "running":
                raise Refused(409, "that session is still running; it can be delivered when it ends")
            if outcome != "done" and not override.strip():
                raise Refused(
                    409,
                    f"that session ended {outcome}, not done, so its gate did not pass. "
                    "Deliver it anyway by giving an `override` with the reason; the reason "
                    "goes on the merge request",
                )
            delivery = await asyncio.to_thread(self._commit, row, lease, title)
            mr = await self._merge_request(row, lease, title, description, outcome, override)
            delivery = Delivery(
                session_id=row.id,
                branch=row.branch,
                commit_sha=delivery,
                mr_iid=mr.get("iid"),
                mr_url=mr.get("web_url", ""),
                updated_at=self.clock(),
            )
            self.store.save_delivery(delivery)
            return delivery.public()

    def _commit(self, row: SessionRow, lease: Lease, title: str) -> str:
        lease_dir = self._dir(lease)
        base = self.git.base(lease_dir, lease.ref)
        latest = self._latest(lease)
        if latest is not None and latest.id == row.id:
            sha = self.git.snapshot(lease_dir, branch=row.branch, base=base, message=title)
        else:
            sha = self.git.tip(lease_dir, row.branch) or base
        if sha == base:
            raise Refused(409, "that session changed nothing, so there is nothing to deliver")
        try:
            self.git.push(lease_dir, row.branch)
        except GitError as exc:
            raise Refused(502, f"the branch could not be pushed: {exc}") from None
        return sha

    async def _merge_request(
        self, row: SessionRow, lease: Lease, title: str, description: str, outcome: str,
        override: str,
    ) -> dict[str, Any]:
        if self.gitlab is None:
            # Pushed, with no GitLab API configured to open the merge request.
            return {}
        body = description.strip()
        body += f"\n\n---\nDelivered by dakcoder from session `{row.id}` ({outcome})."
        if override.strip():
            body += f"\n\n**Delivered with an override.** The gate did not pass. Reason given: {override.strip()}"
        previous = self.store.delivery(row.id)
        try:
            return await self.gitlab.merge_request(
                project_path(lease.repo_url),
                source=row.branch,
                target=lease.ref,
                title=title.strip(),
                description=body,
                iid=previous.mr_iid if previous else None,
            )
        except GitLabError as exc:
            raise Refused(502, f"the branch was pushed but the merge request failed: {exc}") from None

    # -- the reaper (host-plan §9, Phase 4) ---------------------------------

    async def reap(self) -> dict[str, list[str]]:
        """Stop idle runners, and retire leases past their expiry.

        Never under a running session: a run can go an hour without a client
        calling its runner, and killing it would lose the run. Anything skipped
        is looked at again on the next pass.
        """
        leases = {lease.id: lease for lease in self.store.all_leases()}
        stopped: list[str] = []
        for lease_id in self.runners.idle(self.settings.runner_idle.total_seconds(), self.clock()):
            lease = leases.get(lease_id)
            if lease is not None and await self._running_on(lease, lease.owner):
                continue
            await self.runners.stop(lease_id)
            stopped.append(lease_id)
        refreshed = await self.refresh_credentials(leases)
        expired: list[str] = []
        for lease in leases.values():
            if lease.expires_at > self.clock():
                continue
            async with self._lock(lease.id):
                if await self._running_on(lease, lease.owner):
                    continue  # never under a running session; next pass
                await self._retire(lease)
                expired.append(lease.id)
        return {"stopped": stopped, "expired": expired, "refreshed": refreshed}

    #: Replace a runner's gateway token this long before it expires.
    REFRESH_MARGIN = 3600.0

    async def refresh_credentials(self, leases: dict[str, Lease] | None = None) -> list[str]:
        """Give every runner whose token is about to expire a new one.

        Through the runtime's own ``POST /v1/credential``: a run can outlive the
        token it was started with, and a model call on an expired token is a
        401 that ends the run.
        """
        leases = leases or {lease.id: lease for lease in self.store.all_leases()}
        refreshed: list[str] = []
        for runner in self.runners.running():
            lease = leases.get(runner.lease_id)
            if lease is None or runner.credential_expires - self.clock() > self.REFRESH_MARGIN:
                continue
            try:
                jwt, expires = await self.credentials.for_owner(lease.owner)
                status, _ = await runner.upstream.call(
                    "POST", "v1/credential", sub=lease.owner, json={"jwt": jwt}
                )
            except Exception:  # noqa: BLE001 - the next pass tries again
                log.warning("could not refresh the credential of %s's runner", lease.id, exc_info=True)
                continue
            if status == 200:
                runner.credential_expires = expires
                refreshed.append(lease.id)
        return refreshed
