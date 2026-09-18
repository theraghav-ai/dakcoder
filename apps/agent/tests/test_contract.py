"""The published wire contract must match the runtime that serves it.

``api/contract.json`` records the API version, the C2 event types and the REST
route table; ``api/openapi.json`` records every request and response shape. The
extension's TypeScript is generated from the first. Before they existed, six
routes arrived in one release and the extension's ``EventType`` union went five
releases without ``metrics``, and nothing failed, because nothing compared
either side with anything.

A missing file fails; it does not skip. The C1 catalogue check skipped on a
missing file, and it passed without running from the day its output left git.

The shapes in ``rest.py`` are held true by ``wirecheck.CheckedTransport``, which
the route tests use: every JSON response they receive is validated against its
model, with unknown fields rejected.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest

from dakcoder_agent import loopback
from dakcoder_agent.loopback import (
    Loopback,
    PendingApproval,
    create_app,
    published_contract,
    published_openapi,
    route_table,
)
from dakcoder_agent.modes import Intent, Mode
from dakcoder_agent.plan import AGENDA_STATES
from dakcoder_agent.session import Status
from dakcoder_agent.tools.control import STEP_STATUSES
from dakcoder_agent.tools.router import ApprovalRequest
from dakcoder_shared.contract import rest
from dakcoder_shared.envelope import EventType
from wirecheck import CheckedTransport

ROOT = Path(__file__).resolve().parents[3]
API = ROOT / "api"
PUBLISHED = API / "contract.json"
OPENAPI = API / "openapi.json"
GENERATED_TS = ROOT / "extension" / "src" / "contract.gen.ts"
REGENERATE = "Run `make contract` and commit the result."
TOKEN = "tok"


def published() -> dict:
    assert PUBLISHED.is_file(), f"api/contract.json is missing. {REGENERATE}"
    return json.loads(PUBLISHED.read_text(encoding="utf-8"))


@pytest.fixture
def app(tmp_path: Path):
    return create_app(Loopback(tmp_path, lambda _s, _a: None, token=TOKEN))


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=CheckedTransport(app),
        base_url="http://127.0.0.1",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


# ── the published files ─────────────────────────────────────────────────────


def test_the_published_contract_is_current() -> None:
    published()
    assert PUBLISHED.read_text(encoding="utf-8") == published_contract(), (
        f"api/contract.json is stale. {REGENERATE}"
    )


def test_the_published_openapi_is_current() -> None:
    assert OPENAPI.is_file(), f"api/openapi.json is missing. {REGENERATE}"
    assert OPENAPI.read_text(encoding="utf-8") == published_openapi(), (
        f"api/openapi.json is stale. {REGENERATE}"
    )


@pytest.mark.parametrize("name", ["contract.json", "openapi.json"])
def test_the_published_files_are_not_ignored_by_git(name: str) -> None:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", f"api/{name}"], cwd=ROOT, check=False
    ).returncode == 0
    assert not ignored, (
        f"api/{name} is ignored by git, so the check that it is current cannot run "
        "in a fresh clone."
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
    # The REST types come from openapi.json, which the hash does not cover
    # byte for byte. The generator records a digest of the file it read.
    # `read_text` has already turned CRLF into LF, as the generator does.
    openapi = hashlib.sha256(OPENAPI.read_text(encoding="utf-8").encode("utf-8")).hexdigest()[:16]
    assert f"// openapi.json digest: {openapi}" in text, (
        f"extension/src/contract.gen.ts was generated from a different openapi.json. {REGENERATE}"
    )


def test_every_ref_in_the_openapi_resolves() -> None:
    doc = json.loads(published_openapi())
    schemas = doc["components"]["schemas"]
    refs = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(doc)))
    assert refs, "no refs at all means the bodies were never filled in"
    assert sorted(refs - set(schemas)) == []


# ── one declaration of each thing ───────────────────────────────────────────


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


def test_every_route_is_described_and_every_description_is_served(app) -> None:
    """The table cannot be the part that drifts. Six routes once arrived in one
    release while the only written list of them stood still."""
    served = set(route_table(app))
    assert sorted(served - set(rest.ROUTES)) == [], "add these to rest.ROUTES"
    assert sorted(set(rest.ROUTES) - served) == [], "rest.ROUTES names routes nothing serves"


@pytest.mark.parametrize(
    ("literal", "owner"),
    [
        (rest.Status, [str(s) for s in Status]),
        (rest.Intent, [str(i) for i in Intent]),
        (rest.Mode, [str(m) for m in Mode]),
        (rest.AgendaState, list(AGENDA_STATES)),
        (rest.StepStatus, list(STEP_STATUSES)),
    ],
    ids=["Status", "Intent", "Mode", "AgendaState", "StepStatus"],
)
def test_the_contract_copies_of_agent_enumerations_match(literal: Any, owner: list[str]) -> None:
    """The contract cannot import the agent, so it copies these. This is what
    keeps the copies honest."""
    assert sorted(get_args(literal)) == sorted(owner)


# ── the runtime says the same thing ─────────────────────────────────────────


async def test_health_reports_the_published_hash_without_a_token(app) -> None:
    """What a client compares against its compiled-in ``CONTRACT_HASH``.

    Served without a token, like ``api_version``. It describes the code, not
    the developer's machine, and a client needs it before it has a token.
    """
    async with httpx.AsyncClient(
        transport=CheckedTransport(app), base_url="http://127.0.0.1"
    ) as http:
        health = (await http.get("/v1/health")).json()

    assert health["contract_hash"] == published()["hash"]
    assert route_table(app) == published()["routes"]


async def test_the_runtime_serves_the_published_openapi(app) -> None:
    async with client(app) as http:
        served = (await http.get("/openapi.json")).json()
    assert served == json.loads(published_openapi())


def test_the_hash_covers_response_fields(monkeypatch) -> None:
    """A response that gains a field is a contract change, even though no route
    or event type moved."""
    before = published()["hash"]
    monkeypatch.setattr(
        rest, "fields", lambda: {"Session": ["a", "field", "nobody", "documented"]}
    )
    assert json.loads(published_contract())["hash"] != before


# ── routes the route tests do not reach ─────────────────────────────────────


async def test_the_credential_route_matches_its_contract(app) -> None:
    async with client(app) as http:
        accepted = await http.post("/v1/credential", json={"jwt": "a.b.c"})
        refused = await http.post("/v1/credential", json={})
    assert accepted.status_code == 200
    assert refused.status_code == 400


async def test_the_extend_route_matches_its_contract(app, monkeypatch) -> None:
    runtime: Loopback = app.state.runtime
    request = ApprovalRequest(tool="write_file", arguments={"path": "a.go"}, reason="r")
    runtime.approvals[request.id] = PendingApproval(
        id=request.id, session_id="s", request=request
    )
    async with client(app) as http:
        # With a timeout, and without one: `seconds_left` is null then.
        timed = await http.post(f"/v1/approvals/{request.id}/extend")
        monkeypatch.setattr(loopback, "APPROVAL_TIMEOUT", 0.0)
        untimed = await http.post(f"/v1/approvals/{request.id}/extend")
        gone = await http.post("/v1/approvals/nope/extend")

    assert timed.json()["seconds_left"] > 0
    assert untimed.json()["seconds_left"] is None
    assert gone.status_code == 410
