"""The control plane's git never runs what the runner's `.git` says.

The runner is where untrusted code runs, and it can write anything in its
working copy, `.git` included. Git executes hooks, `core.fsmonitor` and filter
drivers from a repository's own config, and `url.*.insteadOf` would redirect an
authenticated push to any host. `repos.Git` only ever uses the lease's mirror as
its git directory, so none of that is read. These tests plant all of it and
check that none of it fires.
"""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from dakcoder_agentsvc.repos import AllowedRepo, Allowlist, Git, NotAllowed, normalise, project_path

from conftest import BASE, git, remote_branches, remote_files


@pytest.fixture
def lease_dir(tmp_path: Path, remote: str) -> Path:
    lease = tmp_path / "lease"
    Git(token="service-token").clone(remote, BASE, lease)
    return lease


def plant(lease_dir: Path, marker: Path, elsewhere: str) -> None:
    """Everything a hostile runner could put in its own `.git`."""
    runner_git = Git.worktree(lease_dir) / ".git"
    for hook in ("post-checkout", "pre-commit", "pre-push", "post-commit", "reference-transaction"):
        path = runner_git / "hooks" / hook
        path.write_text(f"#!/bin/sh\necho {hook} >> '{marker.as_posix()}'\n", encoding="utf-8")
        path.chmod(0o755)
    worktree = Git.worktree(lease_dir)
    git("config", "core.fsmonitor", f"echo fsmonitor >> '{marker.as_posix()}'", cwd=worktree)
    git("config", "filter.evil.clean", f"echo filter >> '{marker.as_posix()}'", cwd=worktree)
    (worktree / ".gitattributes").write_text("*.go filter=evil\n", encoding="utf-8")
    git("config", f"url.{elsewhere}.insteadOf", "file://", cwd=worktree)


def test_nothing_the_runner_plants_is_run(lease_dir: Path, tmp_path: Path, remote: str) -> None:
    marker = tmp_path / "pwned.txt"
    elsewhere = tmp_path / "elsewhere.git"
    git("init", "--bare", str(elsewhere))
    plant(lease_dir, marker, elsewhere.as_uri())

    g = Git(token="service-token")
    worktree = Git.worktree(lease_dir)
    (worktree / "handler" / "pension.go").write_text("package handler\n", encoding="utf-8")
    base = g.base(lease_dir, BASE)
    g.snapshot(lease_dir, branch="dakcoder/s1", base=base, message="s1")
    g.reset(lease_dir, base)
    g.push(lease_dir, "dakcoder/s1")

    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    assert "dakcoder/s1" in remote_branches(remote), "pushed to the real remote"
    assert git("--git-dir", str(elsewhere), "branch") == "", "and not redirected elsewhere"
    assert "handler/pension.go" in remote_files(remote, "dakcoder/s1")

    # The control: the same plant fires for anyone who runs git *in* the
    # runner's copy. Without this the test above could pass on a platform where
    # nothing planted would ever have run.
    (worktree / "handler" / "again.go").write_text("package handler\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=worktree, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "x"],
        cwd=worktree, capture_output=True,
    )
    assert marker.exists(), "the plant is live: run in the runner's .git, it fires"


def test_the_mirror_runs_no_hooks_and_ignores_runtime_state(lease_dir: Path) -> None:
    mirror = Git.mirror(lease_dir)
    hooks_path = git("--git-dir", str(mirror), "config", "core.hooksPath")
    assert Path(hooks_path).is_dir() and not any(Path(hooks_path).iterdir())
    assert ".dakcoder/" in (mirror / "info" / "exclude").read_text()


def test_runtime_state_is_never_delivered(lease_dir: Path) -> None:
    """Even when the runner has removed the `.gitignore` the runtime keeps in it."""
    g = Git()
    state = Git.worktree(lease_dir) / ".dakcoder" / "sessions" / "s1"
    state.mkdir(parents=True)
    (state / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (Git.worktree(lease_dir) / "real.go").write_text("package main\n", encoding="utf-8")
    base = g.base(lease_dir, BASE)
    sha = g.snapshot(lease_dir, branch="dakcoder/s1", base=base, message="s1")
    files = g.run("--git-dir", str(Git.mirror(lease_dir)), "ls-tree", "-r", "--name-only", sha).split()
    assert "real.go" in files
    assert not any(f.startswith(".dakcoder") for f in files)


def test_git_gets_only_the_environment_it_needs(monkeypatch, lease_dir: Path) -> None:
    """The control plane's own secrets are in its environment; git's filters and
    helpers could print them, so git is never given them."""
    monkeypatch.setenv("DAKCODER_AGENTSVC_TOKEN", "cp-secret")
    monkeypatch.setenv("DAKCODER_GITLAB_SERVICE_TOKEN", "gl-secret")
    env = Git(token="gl-secret")._env(authenticated=True)
    assert "DAKCODER_AGENTSVC_TOKEN" not in env
    assert "DAKCODER_GITLAB_SERVICE_TOKEN" not in env
    assert "gl-secret" not in " ".join(k for k in env)  # only inside the auth header's value
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert "gl-secret" not in Git(token="gl-secret")._env(authenticated=False).values().__repr__()


# ── the allowlist ───────────────────────────────────────────────────────────


def test_repository_urls_have_one_spelling() -> None:
    assert normalise("HTTPS://GitLab.CEPT.gov.in/Group/Svc.git/") == "https://gitlab.cept.gov.in/Group/Svc"
    assert project_path("https://gitlab.cept.gov.in/it-2.0/pao/svc.git") == "it-2.0/pao/svc"


def test_the_allowlist_names_who_answers_for_it_and_when_it_ends(tmp_path: Path) -> None:
    listed = tmp_path / "allow.json"
    listed.write_text(
        '[{"repo": "https://g/a/b.git", "owner": "Ops", "expires": "2099-01-01", "subs": ["alice"]}]',
        encoding="utf-8",
    )
    allowlist = Allowlist.load(listed)
    assert allowlist.check("https://g/a/b", "alice").owner == "Ops"
    with pytest.raises(NotAllowed):
        allowlist.check("https://g/a/b", "bob")
    with pytest.raises(NotAllowed):
        allowlist.check("https://g/a/b", "alice", today=date(2099, 1, 2))

    listed.write_text('[{"repo": "https://g/a/b.git", "expires": "2099-01-01"}]', encoding="utf-8")
    with pytest.raises(ValueError):
        Allowlist.load(listed)  # a stopgap nobody owns is a permanent one


def test_an_allowlist_entry_admits_anyone_only_when_it_says_so() -> None:
    entry = AllowedRepo(repo="https://g/x", owner="Ops", expires=date.today() + timedelta(days=1))
    assert entry.admits("anyone", date.today())
