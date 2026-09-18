"""The toolchain versions ``/v1/health`` reports (host-plan §7.3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest

from dakcoder_agent import toolchain
from dakcoder_agent.loopback import Loopback, create_app
from wirecheck import CheckedTransport

#: What each tool actually prints, trimmed.
OUTPUTS = {
    "go": "go version go1.25.0 windows/amd64",
    "gotools": "gotools 0.4.8 (commit abc123)",
    "golangci-lint": "golangci-lint has version 2.1.6 built with go1.24.2 from 1234 on 2025-05-01",
    "govulncheck": "Go: go1.25.0\nScanner: govulncheck@v1.1.4\nDB: https://vuln.go.dev",
    "swag": "swag version v1.16.4",
    "buf": "1.50.0",
    "git": "git version 2.47.0.windows.1",
}


def fake_run(argv: list[str]) -> str:
    return OUTPUTS[Path(argv[0]).stem]


@pytest.fixture(autouse=True)
def gotools_at(monkeypatch):
    monkeypatch.setattr(toolchain.gotools, "_find_binary", lambda: "/opt/gotools")


def test_each_tool_reports_its_own_version() -> None:
    found = toolchain.probe(which=lambda name: f"/usr/bin/{name}", run=fake_run)
    assert found == {
        "go": "1.25.0",
        "gotools": "0.4.8",
        "golangci-lint": "2.1.6",
        # Not 1.25.0, the Go it was built with, which it prints first.
        "govulncheck": "1.1.4",
        "govalid": toolchain.INSTALLED,
        "swag": "1.16.4",
        "buf": "1.50.0",
        "git": "2.47.0",
    }


def test_a_missing_tool_is_null_and_a_silent_one_is_installed(monkeypatch) -> None:
    monkeypatch.setattr(toolchain.gotools, "_find_binary", lambda: None)

    def run(argv: list[str]) -> str:
        if "buf" in argv[0]:
            raise subprocess.TimeoutExpired(argv, 10)
        return "no version here"

    found = toolchain.probe(
        which=lambda name: None if name == "swag" else f"/usr/bin/{name}", run=run
    )
    assert found["swag"] is None, "not on PATH"
    assert found["gotools"] is None, "not found by the sidecar's own locator"
    assert found["buf"] is None, "hung: to the gate, that is missing"
    assert found["go"] == toolchain.INSTALLED, "answered, but with nothing readable"


def test_an_unstamped_build_says_so() -> None:
    """A development build of gotools prints `dev`. A runner on `dev` and a
    laptop on 0.4.8 is exactly the mismatch this report exists to show."""
    found = toolchain.probe(
        which=lambda name: f"/usr/bin/{name}",
        run=lambda argv: "dev\n" if argv[0] == "/opt/gotools" else fake_run(argv),
    )
    assert found["gotools"] == "dev"


async def test_health_reports_the_toolchain_once_probed(tmp_path: Path) -> None:
    runtime = Loopback(tmp_path, lambda _s, _a: None, token="tok")
    transport = CheckedTransport(create_app(runtime))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers={"Authorization": "Bearer tok"}
    ) as http:
        before = (await http.get("/v1/health")).json()
        runtime.set_toolchain({"go": "1.25.0", "swag": None})
        after = (await http.get("/v1/health")).json()
        anonymous = (await http.get("/v1/health", headers={"Authorization": ""})).json()

    assert "toolchain" not in before, "absent until probed, rather than empty"
    assert after["toolchain"] == {"go": "1.25.0", "swag": None}
    assert "toolchain" not in anonymous, "what is installed on a machine needs the token"
