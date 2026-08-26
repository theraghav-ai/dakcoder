"""``dakcoder-gateway`` — the server entrypoint, for hosting it locally.

The gateway package deliberately ships no ``main``: ``create_app`` takes a
fully-wired ``Gateway`` and the wiring is a deployment decision, not a library
one (ARCHITECTURE D-36). This file is that decision for this machine, and it
lives outside ``apps/`` so nothing here can end up inside a wheel.

    DAKCODER_MODEL_API_KEY    the LiteLLM key — the one credential this process
                              holds, and the only process that may (§15.4)
    DAKCODER_MODEL_BASE_URL   LiteLLM's OpenAI-compatible base, e.g.
                              http://127.0.0.1:4000/v1
    DAKCODER_JWT_SECRET       our own signing secret, >= 32 chars
    DAKCODER_REDIS_URL        quota counters; falls back to MemoryStore
    DAKCODER_POSTGRES_DSN     the usage ledger; falls back to MemoryLedger
    DAKCODER_GITLAB_URL/_CLIENT_ID/_CLIENT_SECRET
                              the real IdP. Absent, a local dev IdP is used and
                              the fact is stated on /v1/health rather than
                              hidden — an identity provider that trusts whatever
                              it is told must never be mistaken for the real one.

``--mint`` prints a signed access token instead of serving, which is how a
runtime gets a JWT here without a browser and a GitLab OAuth application.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import timedelta
from pathlib import Path

from dakcoder_gateway.app import Gateway, create_app
from dakcoder_gateway.auth import AuthService, RoleMap, TokenMinter
from dakcoder_gateway.auth.identity import GitLabIdentity, Profile
from dakcoder_gateway.ledger import MemoryLedger, PostgresLedger
from dakcoder_gateway.probe import CapabilityProbe
from dakcoder_gateway.proxy import ModelProxy
from dakcoder_gateway.quota import Limits, MemoryStore, QuotaPolicy, RedisStore
from dakcoder_shared.config import gateway_config
from dakcoder_shared.llm import LLMClient

ROOT = Path(__file__).resolve().parent.parent


class DevIdentity:
    """A local stand-in for GitLab, used only when no OAuth app is configured.

    It authenticates nobody: any code is accepted and mapped to a fixed
    developer. That is fine on a laptop and catastrophic anywhere else, so the
    gateway publishes ``identity: "dev"`` on /v1/health and this class refuses
    to be constructed when the host is not loopback.
    """

    def __init__(self, username: str, groups: tuple[str, ...]) -> None:
        self.username = username
        self.groups = groups

    def authorize_url(self, redirect_uri: str, challenge: str, state: str) -> str:
        # No browser step to perform; the extension can post the code straight
        # back. The URL is still returned so the C3 shape is unchanged.
        return f"dev://sign-in?state={state}&redirect_uri={redirect_uri}"

    async def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        return f"dev-token:{code}"

    async def profile(self, access_token: str) -> Profile:
        return Profile(
            sub=f"dev:{self.username}",
            username=self.username,
            name=self.username,
            email=f"{self.username}@localhost",
            groups=self.groups,
            active=True,
        )


def build_identity(host: str) -> tuple[object, str]:
    base = os.environ.get("DAKCODER_GITLAB_URL", "").strip()
    client_id = os.environ.get("DAKCODER_GITLAB_CLIENT_ID", "").strip()
    secret = os.environ.get("DAKCODER_GITLAB_CLIENT_SECRET", "").strip()
    if base and client_id and secret:
        return GitLabIdentity(base, client_id, secret), "gitlab"

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(
            f"refusing to serve on {host} with the dev identity provider. It accepts "
            "any authorization code, so off-loopback it is an open door. Set "
            "DAKCODER_GITLAB_URL, _CLIENT_ID and _CLIENT_SECRET."
        )
    groups = tuple(
        g for g in os.environ.get("DAKCODER_DEV_GROUPS", "it-2.0/backend").split(",") if g
    )
    return DevIdentity(os.environ.get("DAKCODER_DEV_USER", "localdev"), groups), "dev"


def build_minter(ttl: timedelta | None = None) -> TokenMinter:
    secret = os.environ.get("DAKCODER_JWT_SECRET", "").strip()
    if not secret:
        raise SystemExit("DAKCODER_JWT_SECRET is not set (>= 32 characters).")
    return TokenMinter(secret, **({"access_ttl": ttl} if ttl else {}))


async def build_store(limits: Limits):
    """Redis when reachable, memory otherwise — and say which, loudly.

    A silent fall back to memory would mean quota that resets on restart and
    does not span workers, which looks like working quota right up until it
    matters.
    """
    url = os.environ.get("DAKCODER_REDIS_URL", "").strip()
    if not url:
        return MemoryStore(limits), "memory"
    import redis.asyncio as aioredis

    client = aioredis.from_url(url)
    await client.ping()
    return RedisStore(client, limits), f"redis {url}"


async def build_ledger():
    dsn = os.environ.get("DAKCODER_POSTGRES_DSN", "").strip()
    if not dsn:
        return MemoryLedger(), "memory"
    import asyncpg

    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=8)
    def complain(exc: Exception, event) -> None:
        print(f"[ledger] dropped an event for {event.sub}: {exc}", file=sys.stderr, flush=True)
    return PostgresLedger(pool, on_error=complain), f"postgres {dsn.rsplit('@', 1)[-1]}"


def tool_catalog() -> dict:
    path = ROOT / "docs" / "tool-catalog.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    from dakcoder_agent.tools.catalog import as_json  # only if the agent is installed

    return json.loads(as_json())


def probe_in_background(gateway: Gateway, identity: str) -> None:
    """Run the capability probe off the event loop.

    ``CapabilityProbe`` is synchronous and makes six real model calls. On the
    loop that is thirty seconds during which the gateway answers nothing; in a
    thread it is thirty seconds during which /v1/health says "not probed".

    ``identity`` is carried through the replacement. The probe's report knows
    nothing about which IdP is configured, so assigning it wholesale dropped the
    field and /v1/health then read as though GitLab were in use — the one thing
    publishing it was meant to prevent.
    """

    def run() -> None:
        try:
            report = gateway.probe.run().as_dict()
        except Exception as exc:  # noqa: BLE001 - a failed probe is a report
            report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        gateway.capabilities = {**report, "identity": identity}
        print(f"[probe] {json.dumps(gateway.capabilities)[:600]}", flush=True)

    threading.Thread(target=run, name="capability-probe", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dakcoder-gateway")
    parser.add_argument("--host", default=os.environ.get("DAKCODER_GATEWAY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("DAKCODER_GATEWAY_PORT", 8790)))
    parser.add_argument("--no-probe", action="store_true", help="skip the startup capability probe")
    parser.add_argument("--mint", metavar="SUB", help="print an access token for SUB and exit")
    parser.add_argument("--mint-hours", type=float, default=12.0)
    parser.add_argument("--mint-roles", default="user,admin")
    args = parser.parse_args(argv)

    if args.mint:
        minter = build_minter(timedelta(hours=args.mint_hours))
        roles = tuple(r for r in args.mint_roles.split(",") if r)
        print(minter.mint(sub=args.mint, username=args.mint.split(":")[-1], roles=roles))
        return 0

    identity, kind = build_identity(args.host)
    # Raises if DAKCODER_MODEL_API_KEY is absent — the gateway has no reason to
    # exist without it.
    config = gateway_config()
    limits = Limits()

    import asyncio

    import uvicorn

    async def serve() -> None:
        store, store_name = await build_store(limits)
        ledger, ledger_name = await build_ledger()
        quota = QuotaPolicy(store, limits)
        proxy = ModelProxy(config.base_url, config.api_key, quota, ledger=ledger)

        gateway = Gateway(
            AuthService(identity, build_minter(), roles=RoleMap()),
            quota,
            proxy,
            ledger=ledger,
            probe=CapabilityProbe(LLMClient(config)),
            tool_catalog=tool_catalog(),
            version=os.environ.get("DAKCODER_VERSION", "local"),
        )
        gateway.capabilities = {"status": "not probed", "identity": kind}

        print(
            json.dumps(
                {
                    "listening": f"http://{args.host}:{args.port}",
                    "identity": kind,
                    "quota_store": store_name,
                    "ledger": ledger_name,
                    "upstream": config.base_url,
                    "model": config.model_coder,
                }
            ),
            flush=True,
        )
        if not args.no_probe:
            probe_in_background(gateway, kind)

        server = uvicorn.Server(
            uvicorn.Config(
                create_app(gateway),
                host=args.host,
                port=args.port,
                log_level=os.environ.get("DAKCODER_LOG_LEVEL", "info"),
                # The proxy streams; buffering a response defeats the point.
                timeout_keep_alive=75,
            )
        )
        await server.serve()

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
