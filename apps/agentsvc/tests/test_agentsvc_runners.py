"""What a runner is started with, and that the real runtime starts hosted."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from dakcoder_agentsvc.config import Settings
from dakcoder_agentsvc.runners import DockerBackend, ProcessBackend, RunnerFailed, Runners
from dakcoder_shared.callers import CALLER_HEADER


def settings_for(tmp_path: Path, **kw) -> Settings:
    return Settings(
        data_dir=tmp_path,
        token="cp-secret",
        gateway_url="http://127.0.0.1:9",
        runner_jwt="runner-jwt",
        gitlab_token="gl-secret",
        **kw,
    )


def test_a_container_runner_is_locked_down(tmp_path: Path) -> None:
    """host-plan §7.2, as data."""
    settings = settings_for(tmp_path, runner_network="dakcoder-runners", gomodcache=tmp_path / "gomod")
    backend = DockerBackend(settings)
    argv = backend.run_argv("abc123", tmp_path / "repo", "runner-token", "delegated-jwt")
    text = " ".join(argv)

    for flag in ("--read-only", "--rm", "no-new-privileges"):
        assert flag in argv, flag
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--user") + 1].split(":")[0] not in ("0", "root"), "never root"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert argv[argv.index("--publish") + 1].startswith("127.0.0.1::"), "loopback only"
    assert argv[argv.index("--network") + 1] == "dakcoder-runners"
    assert f"source={tmp_path / 'repo'},target=/workspace" in text
    assert f"source={tmp_path / 'gomod'},target=/gomodcache,readonly" in text, (
        "shared by every tenant: a runner that could write it could poison another's build"
    )
    env = backend.environment("runner-token", "delegated-jwt")
    assert env["DAKCODER_HOSTED"] == "1" and env["DAKCODER_GATEWAY_TOKEN"] == "runner-token"
    assert env["DAKCODER_JWT"] == "delegated-jwt" and env["GOMODCACHE"] == "/gomodcache"
    assert env["GOPROXY"] == "off" and env["GOTOOLCHAIN"] == "local", "no network to fetch with"
    assert argv.count("--env") == len(env) and "DAKCODER_JWT" in argv
    assert "runner-token" not in text and "delegated-jwt" not in text, (
        "values go through docker's environment: argv is readable by every user of the host"
    )
    assert "cp-secret" not in text and "gl-secret" not in text, (
        "the control plane's secrets never reach a runner"
    )


def test_a_container_runner_runs_as_the_owner_of_its_working_copy(
    tmp_path: Path, monkeypatch
) -> None:
    """The working copy is the control plane's: a runner as another uid could
    not write it, nor could the control plane then reset what it wrote."""
    monkeypatch.setattr(os, "getuid", lambda: 1234, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 5678, raising=False)

    def user(**kw) -> str:
        argv = DockerBackend(settings_for(tmp_path, **kw)).run_argv("a", tmp_path, "t")
        return argv[argv.index("--user") + 1]

    assert user() == "1234:5678"
    assert user(runner_user="4000:4000") == "4000:4000"
    for root in ("0:0", "root"):
        with pytest.raises(RunnerFailed, match="root"):
            user(runner_user=root)
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    with pytest.raises(RunnerFailed, match="root"):
        user()


def test_a_container_runner_reaches_the_gateway_on_the_runners_bridge(tmp_path: Path) -> None:
    """A container cannot reach the host's loopback, so a docker runner is
    given the gateway's bridge address; the control plane keeps loopback."""
    settings = settings_for(tmp_path, runner_gateway_url="http://172.30.0.1:8790")
    env = DockerBackend(settings).environment("t", "c")
    assert env["DAKCODER_GATEWAY_URL"] == "http://172.30.0.1:8790"
    assert settings.gateway_url == "http://127.0.0.1:9"


def test_a_process_runner_inherits_nothing_it_should_not(tmp_path: Path, monkeypatch) -> None:
    """Started with a stand-in that reports its environment and exits."""
    monkeypatch.setenv("DAKCODER_AGENTSVC_TOKEN", "cp-secret")
    monkeypatch.setenv("DAKCODER_GITLAB_SERVICE_TOKEN", "gl-secret")
    monkeypatch.setenv("DAKCODER_JWT_SECRET", "signing-secret")
    seen = tmp_path / "env.json"
    script = (
        "import json, os, sys; "
        f"open({str(seen)!r}, 'w').write(json.dumps(dict(os.environ))); "
        "print(json.dumps({'port': 1}), flush=True)"
    )
    settings = settings_for(tmp_path, runner_command=(sys.executable, "-c", script))
    backend = ProcessBackend(settings)
    url, proc = backend.start("abc", tmp_path, "runner-token", "runner-jwt")
    proc.wait(timeout=30)

    env = json.loads(seen.read_text())
    assert url == "http://127.0.0.1:1"
    assert env["DAKCODER_HOSTED"] == "1" and env["DAKCODER_GATEWAY_TOKEN"] == "runner-token"
    assert env["DAKCODER_JWT"] == "runner-jwt"
    for secret in ("DAKCODER_AGENTSVC_TOKEN", "DAKCODER_GITLAB_SERVICE_TOKEN", "DAKCODER_JWT_SECRET"):
        assert secret not in env, secret
    assert "cp-secret" not in json.dumps(env) and "gl-secret" not in json.dumps(env)
    assert env["DAKCODER_APPROVAL_TIMEOUT"] == "1800", "never wait forever, hosted"


@pytest.mark.slow
async def test_the_real_runtime_starts_hosted(tmp_path: Path, monkeypatch) -> None:
    """A real dakcoderd, as the process backend starts it: it answers health to
    anyone, and everything else only with its token and a caller."""
    root = Path(__file__).resolve().parents[3]
    monkeypatch.setenv(
        "PYTHONPATH",
        ";".join(str(root / "apps" / p / "src") for p in ("shared", "agent"))
        if sys.platform == "win32"
        else ":".join(str(root / "apps" / p / "src") for p in ("shared", "agent")),
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    settings = settings_for(
        tmp_path,
        runner_command=(sys.executable, "-c", "from dakcoder_agent.serve import main; raise SystemExit(main())"),
        runner_args=("--no-prewarm",),
    )
    runners = Runners(ProcessBackend(settings))

    async def credential():
        return "runner-jwt", float("inf")

    runner = await runners.ensure("lease1", workspace, credential)
    try:
        async with httpx.AsyncClient(base_url=runner.url, trust_env=False) as http:
            health = (await http.get("/v1/health")).json()
            token_only = await http.get(
                "/v1/sessions", headers={"Authorization": f"Bearer {runner.token}"}
            )
            forwarded = await http.get(
                "/v1/sessions",
                headers={"Authorization": f"Bearer {runner.token}", CALLER_HEADER: "alice"},
            )
        assert health["ok"] is True
        assert token_only.status_code == 401, "hosted: the token alone is nobody"
        assert forwarded.status_code == 200
    finally:
        await runners.stop("lease1")
    assert runner.handle.poll() is not None, "stopped means the process is gone"
