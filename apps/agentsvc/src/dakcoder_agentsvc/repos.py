"""Which repositories may be leased, and the git that clones and pushes them.

**The allowlist is a stopgap, and says so** (host-plan §7.1). The design is that
a lease clones *as the caller*, so GitLab's own permissions decide who may work
on what. That needs the caller's GitLab credential, which needs the real
identity provider (Phase 0a), which does not exist yet. Until it does, a
service account clones, and only the repositories an operator has listed, each
with the named person who answers for it and an expiry. An entry past its
expiry is refused like one that was never listed, so the stopgap cannot quietly
become permanent.

The service account's token is handed to git through its environment
(``GIT_CONFIG_*``), never on the command line, where every user of the host can
read it in the process list, and never written into ``.git/config``, where it
would outlive the operation and sit in a directory a runner can read.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

__all__ = ["AllowedRepo", "Allowlist", "Git", "GitError", "NotAllowed", "normalise", "project_path"]


class NotAllowed(Exception):
    """The repository is not one this deployment may lease, for this caller."""


class GitError(Exception):
    pass


def normalise(url: str) -> str:
    """One spelling per repository: no trailing slash or `.git`, host lower-cased."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    match = re.match(r"^([a-z][a-z0-9+.-]*://)([^/]+)(/.*)?$", url, re.I)
    if match:
        scheme, host, rest = match.groups()
        url = f"{scheme.lower()}{host.lower()}{rest or ''}"
    return url


def project_path(url: str) -> str:
    """`group/sub/project` from a repository URL, for GitLab's API."""
    match = re.match(r"^[a-z]+://[^/]*/(.+)$", normalise(url), re.I)
    if not match:
        raise ValueError(f"not a repository URL: {url}")
    return match.group(1)


@dataclass(frozen=True)
class AllowedRepo:
    repo: str
    #: The person who answers for this entry. Required: a stopgap nobody owns
    #: is a permanent one.
    owner: str
    expires: date
    #: The callers who may lease it, by `sub`. `*` for anyone signed in.
    subs: tuple[str, ...] = ("*",)

    def admits(self, sub: str, today: date) -> bool:
        return today <= self.expires and ("*" in self.subs or sub in self.subs)


class Allowlist:
    """Loaded from a JSON file: a list of ``{repo, owner, expires, subs?}``."""

    def __init__(self, entries: list[AllowedRepo]) -> None:
        self._entries = {normalise(e.repo): e for e in entries}

    @classmethod
    def load(cls, path: Path | None) -> "Allowlist":
        if path is None or not path.is_file():
            return cls([])
        entries = []
        for raw in json.loads(path.read_text(encoding="utf-8")):
            if not raw.get("owner"):
                raise ValueError(f"allowlist entry for {raw.get('repo')} names no owner")
            entries.append(
                AllowedRepo(
                    repo=str(raw["repo"]),
                    owner=str(raw["owner"]),
                    expires=date.fromisoformat(str(raw["expires"])),
                    subs=tuple(raw.get("subs") or ("*",)),
                )
            )
        return cls(entries)

    def check(self, repo_url: str, sub: str, today: date | None = None) -> AllowedRepo:
        entry = self._entries.get(normalise(repo_url))
        if entry is None or not entry.admits(sub, today or date.today()):
            raise NotAllowed(
                "that repository is not available to lease here. Until leases clone "
                "as you, only repositories an operator has listed can be used."
            )
        return entry


#: What a git subprocess inherits. Nothing else: the control plane's environment
#: holds the service account's token and its own, and git runs filters and
#: helpers that could print either.
_GIT_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG")


class Git:
    """The git the control plane runs itself: clone, snapshot, reset, push.

    **It never runs against a ``.git`` the runner can write.** The runner is
    where untrusted code runs, and git executes what a repository's own
    ``.git`` tells it to: hooks, ``core.fsmonitor``, filter drivers, and
    ``url.*.insteadOf``, which would redirect an authenticated push to any host.
    So each lease has two repositories:

    * ``mirror.git``, bare, cloned from the remote, never mounted into a
      runner. Hooks are off and its config is ours. Every credentialed
      operation, and every write, goes through it.
    * ``repo``, the runner's working copy, cloned from the mirror with no
      credential in it. The runner's own git tools use its ``.git``; this class
      reads its *files* only, as ``--work-tree`` with the mirror's ``--git-dir``
      and a temporary index. The worktree's ``.gitattributes`` can name a
      filter, but only config defines one, and the config is the mirror's.

    The runner never pushes either. Its ``git_ops`` has no push by design, and
    hosted, delivery is an action the caller takes on a finished session, not a
    tool the model calls (host-plan §7.4). This class is that action's hands.
    """

    def __init__(self, *, token: str = "", name: str = "dakcoder", email: str = "") -> None:
        self._token = token
        self._identity = (
            "-c", f"user.name={name}",
            "-c", f"user.email={email or 'dakcoder@noreply.invalid'}",
            "-c", "core.fsmonitor=false",
        )

    def _env(self, authenticated: bool, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {k: v for k in _GIT_ENV if (v := os.environ.get(k))}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
        if authenticated and self._token:
            basic = base64.b64encode(f"oauth2:{self._token}".encode()).decode()
            env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
                }
            )
        env.update(extra or {})
        return env

    def run(
        self,
        *args: str,
        cwd: Path | None = None,
        authenticated: bool = False,
        env: dict[str, str] | None = None,
    ) -> str:
        result = subprocess.run(
            ["git", *self._identity, *args],
            cwd=cwd,
            env=self._env(authenticated, env),
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if result.returncode != 0:
            # The token is in the environment, not the arguments, so an error
            # message quoting the command line cannot leak it.
            raise GitError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:500]}")
        return result.stdout.strip()

    # -- a lease's two repositories ------------------------------------------

    @staticmethod
    def mirror(lease_dir: Path) -> Path:
        return lease_dir / "mirror.git"

    @staticmethod
    def worktree(lease_dir: Path) -> Path:
        return lease_dir / "repo"

    def _in_mirror(self, lease_dir: Path, *args: str, index: Path | None = None, work: bool = False,
                   authenticated: bool = False) -> str:
        git = ["--git-dir", str(self.mirror(lease_dir))]
        cwd = None
        if work:
            # An explicit --git-dir means git does no discovery: the worktree's
            # own `.git`, whatever the runner has put in it, is never read.
            git += ["--work-tree", str(self.worktree(lease_dir))]
            cwd = self.worktree(lease_dir)
        env = {"GIT_INDEX_FILE": str(index)} if index else None
        return self.run(*git, *args, cwd=cwd, authenticated=authenticated, env=env)

    def clone(self, url: str, ref: str, lease_dir: Path) -> None:
        mirror, worktree = self.mirror(lease_dir), self.worktree(lease_dir)
        lease_dir.mkdir(parents=True, exist_ok=True)
        self.run(
            "clone", "--bare", "--branch", ref, "--single-branch", "--no-tags", url, str(mirror),
            authenticated=True,
        )
        hooks = lease_dir / "no-hooks"
        hooks.mkdir(exist_ok=True)
        self._in_mirror(lease_dir, "config", "core.hooksPath", str(hooks))
        # Runtime state is never a change to deliver, whatever the runner has
        # done to the `.gitignore` the runtime keeps inside it.
        (mirror / "info").mkdir(exist_ok=True)
        (mirror / "info" / "exclude").write_text(".dakcoder/\n", encoding="utf-8")
        self.run("clone", "--branch", ref, str(mirror), str(worktree))

    def base(self, lease_dir: Path, ref: str) -> str:
        return self._in_mirror(lease_dir, "rev-parse", f"refs/heads/{ref}")

    def tip(self, lease_dir: Path, branch: str) -> str | None:
        try:
            return self._in_mirror(lease_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        except GitError:
            return None

    def snapshot(self, lease_dir: Path, *, branch: str, base: str, message: str) -> str:
        """Commit the runner's working tree to ``branch`` in the mirror.

        On top of ``branch`` if it exists, else on ``base``. Returns the branch's
        tip: a new commit when the tree changed, the old tip when it did not.
        """
        parent = self.tip(lease_dir, branch) or base
        index = lease_dir / "snapshot.index"
        index.unlink(missing_ok=True)
        try:
            self._in_mirror(lease_dir, "read-tree", parent, index=index)
            self._in_mirror(lease_dir, "add", "--all", ".", index=index, work=True)
            tree = self._in_mirror(lease_dir, "write-tree", index=index)
        finally:
            index.unlink(missing_ok=True)
        if tree == self._in_mirror(lease_dir, "rev-parse", f"{parent}^{{tree}}"):
            if parent != base:
                return parent
            # Nothing changed and nothing was ever snapshotted: the branch is the base.
            self._in_mirror(lease_dir, "update-ref", f"refs/heads/{branch}", base)
            return base
        commit = self._in_mirror(lease_dir, "commit-tree", tree, "-p", parent, "-m", message)
        self._in_mirror(lease_dir, "update-ref", f"refs/heads/{branch}", commit)
        return commit

    def changed(self, lease_dir: Path, base: str) -> bool:
        """Whether the runner's working tree differs from ``base``."""
        index = lease_dir / "changed.index"
        index.unlink(missing_ok=True)
        try:
            self._in_mirror(lease_dir, "read-tree", base, index=index)
            self._in_mirror(lease_dir, "add", "--all", ".", index=index, work=True)
            tree = self._in_mirror(lease_dir, "write-tree", index=index)
        finally:
            index.unlink(missing_ok=True)
        return tree != self._in_mirror(lease_dir, "rev-parse", f"{base}^{{tree}}")

    def reset(self, lease_dir: Path, base: str) -> None:
        """Put the runner's working tree back to ``base``: the next session
        starts from the base, not from the last one's changes."""
        index = lease_dir / "reset.index"
        index.unlink(missing_ok=True)
        try:
            self._in_mirror(lease_dir, "read-tree", base, index=index)
            self._in_mirror(lease_dir, "checkout-index", "--all", "--force", index=index, work=True)
            self._in_mirror(lease_dir, "clean", "-d", "--force", index=index, work=True)
        finally:
            index.unlink(missing_ok=True)

    def push(self, lease_dir: Path, branch: str) -> None:
        self._in_mirror(
            lease_dir, "push", "--force", "origin", f"refs/heads/{branch}:refs/heads/{branch}",
            authenticated=True,
        )

    @staticmethod
    def size(path: Path) -> int:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
