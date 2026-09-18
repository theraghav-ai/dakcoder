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
from dakcoder_agent.loop import AgentLoop, Outcome
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
from dakcoder_agent.migration import MigrationState
from dakcoder_shared.contract import compat, events, rest
from dakcoder_shared.envelope import DeltaCoalescer, EventType, ToolResult
from scripted import build, planning_router  # noqa: F401 - fixture
from test_loopback import client, scripted, settle, start  # noqa: F401 - fixtures
from wirecheck import CheckedTransport, event_problem

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


def http_for(app) -> httpx.AsyncClient:
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


@pytest.mark.parametrize("name", ["contract.json", "openapi.json", "contract-baseline.json"])
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


def test_every_event_type_has_a_payload_model() -> None:
    assert set(events.PAYLOADS) == set(EventType)
    schemas = json.loads(published_openapi())["components"]["schemas"]
    for event, model in published()["payloads"].items():
        assert model in schemas, f"{event}'s payload {model} is not in api/openapi.json"


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
        (events.Outcome, [v for k, v in vars(Outcome).items() if k.isupper()]),
    ],
    ids=["Status", "Intent", "Mode", "AgendaState", "StepStatus", "Outcome"],
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
    async with http_for(app) as http:
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
    async with http_for(app) as http:
        accepted = await http.post("/v1/credential", json={"jwt": "a.b.c"})
        refused = await http.post("/v1/credential", json={})
    assert accepted.status_code == 200
    assert refused.status_code == 400


async def test_the_extend_route_matches_its_contract(app, monkeypatch) -> None:
    runtime: Loopback = app.state.runtime
    session = runtime.sessions.create("a task")
    request = ApprovalRequest(tool="write_file", arguments={"path": "a.go"}, reason="r")
    runtime.approvals[request.id] = PendingApproval(
        id=request.id, session_id=session.id, request=request
    )
    async with http_for(app) as http:
        # With a timeout, and without one: `seconds_left` is null then.
        timed = await http.post(f"/v1/approvals/{request.id}/extend")
        monkeypatch.setattr(loopback, "APPROVAL_TIMEOUT", 0.0)
        untimed = await http.post(f"/v1/approvals/{request.id}/extend")
        gone = await http.post("/v1/approvals/nope/extend")

    assert timed.json()["seconds_left"] > 0
    assert untimed.json()["seconds_left"] is None
    assert gone.status_code == 410


# ── events the loop tests do not reach ──────────────────────────────────────
#
# conftest.py validates every event an AgentLoop emits. These are the ones that
# do not come out of a loop in the suite: the loopback's own, the streamed
# text, and two `gate` kinds only a long or migrating run produces.


def assert_matches(recorded) -> None:
    problems = [p for e in recorded if (p := event_problem(e)) is not None]
    assert not problems, "\n\n".join(problems)


async def test_the_loopbacks_own_events_match_their_contract(client, scripted) -> None:
    """`user` is only ever emitted by the loopback, never by the loop."""
    session = await start(client)
    await settle(session["id"], scripted)
    await client.post(f"/v1/sessions/{session['id']}/messages", json={"text": "and a test"})
    await settle(session["id"], scripted)

    recorded = scripted.sessions.get(session["id"]).events
    assert [e for e in recorded if e.type is EventType.USER]
    assert_matches(recorded)


async def test_a_crashed_runs_events_match_their_contract(client, scripted, monkeypatch) -> None:
    """The loopback writes `error`, `finish` and `end` itself when a run raises."""

    def crash(self, *args, **kwargs):
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this a generator, like the real one

    monkeypatch.setattr(AgentLoop, "run", crash)
    session = await start(client)
    await settle(session["id"], scripted)

    recorded = scripted.sessions.get(session["id"]).events
    assert {EventType.ERROR, EventType.FINISH, EventType.END} <= {e.type for e in recorded}
    assert_matches(recorded)


def test_streamed_text_matches_its_contract() -> None:
    coalescer = DeltaCoalescer()
    assert_matches(list(coalescer.drain(["half ", "a ", "sentence"])))


def test_the_compaction_and_route_gates_match_their_contract(planning_router) -> None:
    loop, _ = build(planning_router, [])
    for i in range(6):
        loop.context.append_user(f"message {i} " + "x" * 400)

    relayed: list = []
    loop._relay = relayed.append
    loop.state.migration = MigrationState(active=True)
    loop.router.run_gate_tool = lambda name, args=None: ToolResult.success(
        "12 route(s)", meta={"routes": 12, "unresolved": 0}
    )
    loop._save_routes()
    compacted = list(loop._compact(retain_pct=0.15, reason="test", strategy="basic"))

    kinds = [e.data.get("kind") for e in [*relayed, *compacted]]
    assert "routes" in kinds and "compaction" in kinds, kinds
    assert_matches([*relayed, *compacted])


# ── additive only (host-plan §4.6) ──────────────────────────────────────────

BASELINE = API / "contract-baseline.json"


def current_surface() -> list[str]:
    return compat.surface(published(), json.loads(published_openapi()))


def test_the_contract_only_grows() -> None:
    """Nothing the last release promised may disappear within a major version.

    C2 allowed additive changes and forbade removals, and nothing checked the
    second half. `API_VERSION` stayed 1.1 while the surface grew by a third,
    which was legal; a removal under the same number would have been exactly
    as silent.
    """
    assert BASELINE.is_file(), (
        "api/contract-baseline.json is missing. Seed it with "
        "`make contract-baseline RELEASE=<last release>` and commit it."
    )
    previous = json.loads(BASELINE.read_text(encoding="utf-8"))
    broken = compat.breaks(previous, current_surface(), published()["api_version"])
    assert not broken, (
        f"The contract no longer promises {len(broken)} thing(s) that "
        f"api/contract-baseline.json ({previous['release']}) does. C2 is additive-only "
        "within a major version: put them back, or bump API_VERSION's major and serve "
        "both shapes through a deprecation window (host-plan §4.6).\n  "
        + "\n  ".join(broken[:50])
    )


def _schemas(openapi: dict) -> dict:
    return openapi["components"]["schemas"]


@pytest.mark.parametrize(
    ("change", "edit"),
    [
        ("a response field removed",
         lambda c, o: _schemas(o)["Session"]["properties"].pop("turns")),
        ("a response field made optional",
         lambda c, o: _schemas(o)["Session"]["required"].remove("turns")),
        ("a response field made nullable",
         lambda c, o: _schemas(o)["Session"]["properties"].update(
             turns={"anyOf": [{"type": "integer"}, {"type": "null"}]})),
        ("an event payload field removed",
         lambda c, o: _schemas(o)["UsagePayload"]["properties"].pop("budget")),
        ("an optional request field made required",
         lambda c, o: _schemas(o)["TaskRequest"]["required"].append("intent")),
        ("an accepted request value dropped",
         lambda c, o: _schemas(o)["TaskRequest"]["properties"]["intent"]["enum"].remove("auto")),
        ("an event type removed", lambda c, o: c["events"].remove("metrics")),
        ("a route removed", lambda c, o: c["routes"].remove("GET /v1/agenda")),
        ("a gate kind removed", lambda c, o: _schemas(o)["GatePayload"]["oneOf"].pop()),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_the_compat_check_catches(change: str, edit) -> None:
    """The check above is only as good as what it can see. Each of these is a
    change that would break a client written against the previous release."""
    contract, openapi = published(), json.loads(published_openapi())
    before = {"api_version": contract["api_version"], "facts": compat.surface(contract, openapi)}
    edit(contract, openapi)
    assert compat.breaks(before, compat.surface(contract, openapi), contract["api_version"]), change


def test_additions_and_a_major_bump_are_not_breaks() -> None:
    contract, openapi = published(), json.loads(published_openapi())
    before = {"api_version": contract["api_version"], "facts": compat.surface(contract, openapi)}

    grown_contract, grown = published(), json.loads(published_openapi())
    _schemas(grown)["Session"]["properties"]["new_field"] = {"type": "string"}
    _schemas(grown)["TaskRequest"]["properties"]["new_option"] = {"type": "string"}
    grown_contract["events"].append("new_event")
    grown_contract["routes"].append("GET /v1/new")
    assert compat.breaks(before, compat.surface(grown_contract, grown), "1.1") == []

    _schemas(openapi)["Session"]["properties"].pop("turns")
    assert compat.breaks(before, compat.surface(contract, openapi), "2.0") == [], (
        "a major version is allowed to remove things; that is what it is for"
    )
