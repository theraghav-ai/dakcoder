"""Tests for identity (Part A §15, contract C3).

What is being replaced is a shared bearer token plus a client-supplied
``X-Postgen-User`` header — anyone holding the token can claim any identity. So
the tests that matter are the ones asserting that identity cannot be claimed:
forged tokens, replayed states, stolen refresh tokens, revoked accounts.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from dakcoder_gateway.auth import (
    AuthError,
    AuthService,
    Profile,
    RoleMap,
    TokenError,
    TokenMinter,
    verifier_challenge,
)

from fakes import REDIRECT, SECRET, VERIFIER, FakeGitLab

@pytest.fixture
def auth(gitlab: FakeGitLab) -> AuthService:
    return AuthService(gitlab, TokenMinter(SECRET))


async def signed_in(auth: AuthService):
    started = auth.start(REDIRECT, verifier_challenge(VERIFIER))
    return await auth.exchange(
        code="good-code", code_verifier=VERIFIER, state=started["state"], redirect_uri=REDIRECT
    )


# ── the happy path ──────────────────────────────────────────────────────────


async def test_a_developer_signs_in_and_gets_a_session(auth: AuthService) -> None:
    session = await signed_in(auth)

    assert session.access_token
    assert session.refresh_token
    assert session.profile.username == "asha"
    assert session.roles == ("user",)


async def test_the_gateway_does_the_exchange_not_the_extension(
    auth: AuthService, gitlab: FakeGitLab
) -> None:
    """§15.2: extension code is inspectable, so a client secret in it is an
    announcement rather than a control. The extension never holds a GitLab
    token and is never the party that reads group membership."""
    await signed_in(auth)
    assert gitlab.exchanges == [("good-code", VERIFIER, REDIRECT)]


async def test_the_session_payload_never_carries_a_gitlab_token(auth: AuthService) -> None:
    payload = (await signed_in(auth)).as_dict()
    assert "gitlab-access-token" not in str(payload)


# ── the state check ─────────────────────────────────────────────────────────


async def test_a_state_the_gateway_never_issued_is_refused(auth: AuthService) -> None:
    """The security fix over §15.2's diagram.

    With the extension generating state, the gateway receives a value it has
    never seen and cannot check — so the CSRF protection state exists to provide
    is unenforceable on the only side that could enforce it.
    """
    with pytest.raises(AuthError, match="unknown"):
        await auth.exchange(
            code="good-code", code_verifier=VERIFIER, state="made-up", redirect_uri=REDIRECT
        )


async def test_a_state_is_single_use(auth: AuthService, gitlab: FakeGitLab) -> None:
    """Single use is what stops a captured callback being replayed."""
    started = auth.start(REDIRECT, verifier_challenge(VERIFIER))
    gitlab.codes["second-code"] = "another-token"
    await auth.exchange(
        code="good-code", code_verifier=VERIFIER, state=started["state"], redirect_uri=REDIRECT
    )

    with pytest.raises(AuthError, match="already been used"):
        await auth.exchange(
            code="second-code",
            code_verifier=VERIFIER,
            state=started["state"],
            redirect_uri=REDIRECT,
        )


async def test_the_redirect_uri_is_bound_to_the_state(auth: AuthService) -> None:
    """Otherwise a flow started for the vscode:// handler could be completed
    against a loopback port an attacker controls."""
    started = auth.start(REDIRECT, verifier_challenge(VERIFIER))

    with pytest.raises(AuthError, match="redirect URI"):
        await auth.exchange(
            code="good-code",
            code_verifier=VERIFIER,
            state=started["state"],
            redirect_uri="http://127.0.0.1:31337/callback",
        )


async def test_the_authorize_url_carries_the_challenge_and_the_state(
    auth: AuthService,
) -> None:
    challenge = verifier_challenge(VERIFIER)
    started = auth.start(REDIRECT, challenge)
    assert started["state"] in started["authorize_url"]
    assert challenge in started["authorize_url"]


def test_the_pkce_challenge_is_unpadded_base64url() -> None:
    """A padded challenge is rejected by conformant servers, and the error says
    nothing useful about why."""
    challenge = verifier_challenge(VERIFIER)
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge


# ── roles ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "groups,expected",
    [
        (("it-2.0/pension-api",), ("user",)),
        (("it-2.0-common/platform",), ("admin",)),
        (("it-2.0-common/platform", "it-2.0/pension-api"), ("admin", "user")),
        (("it-2.0",), ("user",)),
    ],
)
def test_group_membership_maps_to_roles(groups, expected) -> None:
    assert RoleMap().roles_for(groups) == expected


async def test_a_developer_in_no_relevant_group_is_refused(gitlab: FakeGitLab) -> None:
    """Authorisation is membership. Without this, anyone with a GitLab account
    on the instance has an agent."""
    gitlab.profile_data = Profile(sub="gitlab:9", username="stranger", groups=("other/thing",))
    auth = AuthService(gitlab, TokenMinter(SECRET))

    with pytest.raises(AuthError) as caught:
        await signed_in(auth)
    assert caught.value.status == 403


def test_the_role_mapping_is_configuration(gitlab: FakeGitLab) -> None:
    """§15.2: mapping is config, not code. A reorganisation is a settings
    change and a new tenant does not need a release."""
    custom = RoleMap(rules=(("sso-tenant/", "user"),), default=("guest",))
    assert custom.roles_for(("sso-tenant/x",)) == ("user",)
    assert custom.roles_for(("nothing/here",)) == ("guest",)


# ── the token ───────────────────────────────────────────────────────────────


# The short key is the point: this is a forgery attempt, and PyJWT's
# advice about key length is aimed at people minting real tokens.
@pytest.mark.filterwarnings("ignore::UserWarning")
def test_a_forged_token_is_refused() -> None:
    minter = TokenMinter(SECRET)
    forged = jwt.encode(
        {"sub": "gitlab:1", "dop_roles": ["admin"], "iat": 0, "exp": 9_999_999_999},
        "not-the-secret",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        minter.verify(forged)


def test_the_none_algorithm_is_refused() -> None:
    """The classic JWT failure. Accepting whatever the header names turns
    verification into a formality."""
    minter = TokenMinter(SECRET)
    unsigned = jwt.encode({"sub": "gitlab:1", "iat": 0, "exp": 9_999_999_999}, "", algorithm="none")
    with pytest.raises(TokenError):
        minter.verify(unsigned)


def test_a_short_signing_secret_is_refused_at_construction() -> None:
    """Refused rather than warned about. A gateway that starts with a forgeable
    secret is one where authentication is decorative."""
    with pytest.raises(ValueError, match="at least 32"):
        TokenMinter("short")


def test_an_expired_token_says_to_refresh() -> None:
    minter = TokenMinter(SECRET, access_ttl=timedelta(seconds=-1))
    token = minter.mint(sub="gitlab:7", username="asha", roles=("user",))
    with pytest.raises(TokenError, match="refresh"):
        minter.verify(token)


def test_a_token_from_another_issuer_is_refused() -> None:
    minter = TokenMinter(SECRET)
    now = int(time.time())
    foreign = jwt.encode(
        {
            "iss": "somebody-else",
            "aud": "dakcoder",
            "sub": "gitlab:1",
            "iat": now,
            "exp": now + 900,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        minter.verify(foreign)


async def test_verify_reads_a_bearer_header(auth: AuthService) -> None:
    session = await signed_in(auth)
    claims = auth.verify(f"Bearer {session.access_token}")
    assert claims.sub == "gitlab:7"
    assert claims.has_role("user")


@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer", "token abc"])
async def test_a_malformed_authorization_header_is_refused(
    auth: AuthService, header
) -> None:
    with pytest.raises(AuthError):
        auth.verify(header)


# ── the write-side freshness rule ───────────────────────────────────────────


async def test_write_actions_need_a_recently_minted_token(gitlab: FakeGitLab) -> None:
    """§15.2 keeps v1's control, and it is stricter than "not expired": a
    developer who walked away and came back must not have a scaffold applied on
    a token issued before they left."""
    # Anchored to the real clock: PyJWT validates iat/nbf/exp against the system
    # time, so a token minted at a fixed fictional date is rejected before
    # freshness is ever considered. Only the *elapsed* time is simulated.
    now = datetime.now(tz=timezone.utc)
    minter = TokenMinter(SECRET, clock=lambda: now)
    moving = {"at": now}
    auth = AuthService(gitlab, minter, clock=lambda: moving["at"])

    session = await signed_in(auth)
    claims = auth.verify(f"Bearer {session.access_token}")
    auth.require_write(claims)  # fresh: fine

    moving["at"] = now + timedelta(minutes=16)
    with pytest.raises(AuthError, match="recently issued"):
        auth.require_write(claims)


# ── refresh ─────────────────────────────────────────────────────────────────


async def test_refreshing_rotates_the_token(auth: AuthService) -> None:
    session = await signed_in(auth)
    refreshed = await auth.refresh(session.refresh_token)
    assert refreshed.refresh_token != session.refresh_token


async def test_every_refresh_rechecks_the_account_with_gitlab(
    auth: AuthService, gitlab: FakeGitLab
) -> None:
    """What makes revocation real: a blocked account loses access within one
    token lifetime and nobody has to run a deprovisioning step."""
    session = await signed_in(auth)
    await auth.refresh(session.refresh_token)
    assert gitlab.rechecks == 1


async def test_a_blocked_account_loses_access_at_the_next_refresh(
    auth: AuthService, gitlab: FakeGitLab
) -> None:
    session = await signed_in(auth)
    gitlab.profile_data = Profile(
        sub="gitlab:7", username="asha", groups=("it-2.0/pension-api",), active=False
    )

    with pytest.raises(AuthError, match="no longer active"):
        await auth.refresh(session.refresh_token)


async def test_reusing_a_refresh_token_kills_the_whole_family(auth: AuthService) -> None:
    """OAuth 2.0 BCP. A thirty-day credential in a keychain survives theft only
    if reuse is noticed — and noticing is only useful if it does something."""
    session = await signed_in(auth)
    second = await auth.refresh(session.refresh_token)

    with pytest.raises(AuthError, match="already used"):
        await auth.refresh(session.refresh_token)

    # The token the legitimate user holds is gone too. That is the point: we
    # cannot tell owner from thief, and the safe reading is to end the session.
    with pytest.raises(AuthError):
        await auth.refresh(second.refresh_token)


async def test_a_revoked_family_invalidates_its_access_tokens_immediately(
    auth: AuthService,
) -> None:
    """Without this a stolen access token stays good for its full fifteen
    minutes after the theft was detected — exactly the window detection was
    meant to close."""
    session = await signed_in(auth)
    assert auth.verify(f"Bearer {session.access_token}")

    auth.revoke(session.refresh_token)

    with pytest.raises(AuthError, match="revoked"):
        auth.verify(f"Bearer {session.access_token}")


async def test_an_unknown_refresh_token_is_refused(auth: AuthService) -> None:
    with pytest.raises(AuthError, match="not recognised"):
        await auth.refresh("not-a-token")


# ── failures the caller has to tell apart ───────────────────────────────────


async def test_an_unreachable_gitlab_is_reported_as_retryable(
    auth: AuthService, gitlab: FakeGitLab
) -> None:
    """A refused code and an unreachable GitLab need opposite responses. Merging
    them is how a sign-in failure becomes an infinite retry loop."""
    gitlab.unreachable = True
    started = auth.start(REDIRECT, verifier_challenge(VERIFIER))

    with pytest.raises(AuthError) as caught:
        await auth.exchange(
            code="good-code",
            code_verifier=VERIFIER,
            state=started["state"],
            redirect_uri=REDIRECT,
        )
    assert caught.value.retryable
    assert caught.value.status == 503


async def test_a_bad_authorization_code_is_not_retryable(auth: AuthService) -> None:
    started = auth.start(REDIRECT, verifier_challenge(VERIFIER))

    with pytest.raises(AuthError) as caught:
        await auth.exchange(
            code="wrong-code",
            code_verifier=VERIFIER,
            state=started["state"],
            redirect_uri=REDIRECT,
        )
    assert not caught.value.retryable
    assert caught.value.status == 401
