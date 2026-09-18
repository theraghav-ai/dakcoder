"""The published wire contract must match the runtime that serves it.

``api/contract.json`` is the one written description of the REST route table
and the C2 event types. The extension's TypeScript is generated from it. Before
it existed, six routes arrived in one release and the extension's
``EventType`` union went five releases without ``metrics``, and nothing failed,
because nothing compared either side with anything.

A missing file fails; it does not skip. The C1 catalogue check skipped on a
missing file, and it passed without running from the day its output left git.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from dakcoder_agent.loopback import Loopback, create_app, published_contract, route_table
from dakcoder_shared.envelope import EventType

ROOT = Path(__file__).resolve().parents[3]
PUBLISHED = ROOT / "api" / "contract.json"
GENERATED_TS = ROOT / "extension" / "src" / "contract.gen.ts"
REGENERATE = "Run `make contract` and commit the result."


def published() -> dict:
    assert PUBLISHED.is_file(), f"api/contract.json is missing. {REGENERATE}"
    return json.loads(PUBLISHED.read_text(encoding="utf-8"))


def test_the_published_contract_is_current() -> None:
    published()
    assert PUBLISHED.read_text(encoding="utf-8") == published_contract(), (
        f"api/contract.json is stale. {REGENERATE}"
    )


def test_the_published_contract_is_not_ignored_by_git() -> None:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "api/contract.json"], cwd=ROOT, check=False
    ).returncode == 0
    assert not ignored, (
        "api/contract.json is ignored by git, so the check above cannot run in a "
        "fresh clone."
    )


def test_the_extension_was_generated_from_this_contract() -> None:
    """The extension's own check needs node; this one does not.

    So a Python-only CI job still fails when the contract changed and the
    TypeScript was not regenerated. Neither side can be green alone.
    """
    assert GENERATED_TS.is_file(), f"extension/src/contract.gen.ts is missing. {REGENERATE}"
    text = GENERATED_TS.read_text(encoding="utf-8")
    contract = published()
    assert f"export const CONTRACT_HASH = '{contract['hash']}';" in text, (
        f"extension/src/contract.gen.ts was generated from a different contract. {REGENERATE}"
    )
    assert f"export const API_VERSION = '{contract['api_version']}';" in text


def test_every_event_type_is_published() -> None:
    assert set(published()["events"]) == {str(t) for t in EventType}


def test_api_version_is_declared_once() -> None:
    """The extension used to declare it too, by hand. Now nothing but
    ``dakcoder_shared.contract`` may assign it."""
    declared = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "apps").glob("*/src/**/*.py")
        if re.search(r"^API_VERSION\s*=", path.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert declared == ["apps/shared/src/dakcoder_shared/contract/__init__.py"]


async def test_health_reports_the_published_hash_without_a_token(tmp_path: Path) -> None:
    """What a client compares against its compiled-in ``CONTRACT_HASH``.

    Served without a token, like ``api_version``. It describes the code, not
    the developer's machine, and a client needs it before it has a token.
    """
    app = create_app(Loopback(tmp_path, lambda _s, _a: None, token="tok"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
        health = (await http.get("/v1/health")).json()

    assert health["contract_hash"] == published()["hash"]
    assert route_table(app) == published()["routes"]
