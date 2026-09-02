"""The sign-in flow: start, exchange, refresh (Part A §15.2, contract C3).

    extension                          gateway                         GitLab
      │ POST /v1/auth/start ──────────▶│ issue+store state, build URL
      │◀── {state, authorize_url} ─────│
      │ openExternal(authorize_url) ───────────────────────────────────▶│
      │◀── vscode://…/callback?code&state ──────────────────────────────┘
      │ POST /v1/auth/exchange {code, code_verifier, state} ─▶│
      │                                │ verify state, POST /oauth/token ─▶
      │                                │ GET /api/v4/user, /groups ───────▶
      │◀── {access_token, refresh_token, profile, quota} ────│

**The ``start`` call is an addition to §15.2's diagram, and it is a security
fix rather than a convenience.** In the diagram the extension generates ``state``
itself, which means the gateway receives a value it has never seen and cannot
check — so the CSRF protection ``state`` exists to provide is unenforceable on
the only side that could enforce it. Having the gateway issue it closes an
authorization-code injection: an attacker who gets a victim's browser to complete
a flow cannot then post that code to the gateway, because they hold no state the
gateway issued. It costs one round trip at sign-in, once.

**Refresh tokens rotate, and reuse revokes the family.** OAuth 2.0 BCP. A
refresh token is a thirty-day credential sitting in a keychain; the one thing
that makes theft survivable is noticing when both the thief and the owner use
it. Rotation makes reuse detectable, and detection is only useful if it does
something — so it kills the whole family, forcing a fresh sign-in.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .identity import IdentityError, IdentityProvider, Profile
from .tokens import REFRESH_TTL, Claims, TokenError, TokenMinter

__all__ = ["AuthError", "AuthService", "RoleMap", "Session", "verifier_challenge"]

#: GitLab group path prefix → role. Config, not code (§15.2): a reorganisation
#: is a settings change, and a new tenant does not need a release.
DEFAULT_ROLES: tuple[tuple[str, str], ...] = (
    ("it-2.0-common/", "admin"),
    ("it-2.0/", "user"),
)

STATE_TTL = timedelta(minutes=10)


class AuthError(Exception):
    """Sign-in failed. ``retryable`` distinguishes a bad code from a bad day."""

    def __init__(self, message: str, *, retryable: bool = False, status: int = 401) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass(frozen=True, slots=True)
class RoleMap:
    """Group paths to roles."""

    rules: tuple[tuple[str, str], ...] = DEFAULT_ROLES
    default: tuple[str, ...] = ()

    def roles_for(self, groups: tuple[str, ...]) -> tuple[str, ...]:
        found: list[str] = []
        for group in groups:
            for prefix, role in self.rules:
                if group == prefix.rstrip("/") or group.startswith(prefix):
                    if role not in found:
                        found.append(role)
        return tuple(found) or self.default


@dataclass(frozen=True, slots=True)
class Session:
    """What the extension gets back from an exchange or a refresh."""

    access_token: str
    refresh_token: str
    profile: Profile
    roles: tuple[str, ...]
    expires_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat(),
            "profile": {
                "sub": self.profile.sub,
                "username": self.profile.username,
                "name": self.profile.name,
                "email": self.profile.email,
                "dop_roles": list(self.roles),
            },
        }


@dataclass
class _Refresh:
    sub: str
    family: str
    expires_at: float
    used: bool = False
    #: The provider's own access token, captured at sign-in and carried across
    #: rotations. ``recheck`` needs a credential to re-read the account with, and
    #: the alternative — an administrative GitLab token on the gateway — would
    #: make a gateway compromise a compromise of every account.
    provider_token: str = ""


def verifier_challenge(verifier: str) -> str:
    """The S256 challenge for a PKCE verifier.

    Here so the tests and any future adapter compute it the same way; the
    extension computes its own. Base64url, no padding — a padded challenge is
    rejected by conformant servers and the error says nothing useful.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class AuthService:
    """Issues and refreshes sessions."""

    def __init__(
        self,
        identity: IdentityProvider,
        minter: TokenMinter,
        *,
        roles: RoleMap | None = None,
        clock=lambda: datetime.now(tz=timezone.utc),
    ) -> None:
        self.identity = identity
        self.minter = minter
        self.roles = roles or RoleMap()
        self._clock = clock
        self._states: dict[str, tuple[str, float]] = {}
        self._refresh: dict[str, _Refresh] = {}
        self._revoked_families: set[str] = set()

    # -- start ---------------------------------------------------------------

    def start(self, redirect_uri: str, challenge: str) -> dict[str, Any]:
        """Issue a state and build the authorize URL.

        The state is stored with a ten-minute life. Anything longer widens the
        window in which a stolen callback can be replayed; anything shorter
        breaks a developer who has to authenticate to GitLab first.
        """
        self._sweep()
        state = secrets.token_urlsafe(24)
        self._states[state] = (redirect_uri, time.monotonic() + STATE_TTL.total_seconds())

        url = getattr(self.identity, "authorize_url", None)
        return {
            "state": state,
            "authorize_url": url(redirect_uri, challenge, state) if url else "",
            "expires_in": int(STATE_TTL.total_seconds()),
        }

    # -- exchange ------------------------------------------------------------

    async def exchange(
        self, *, code: str, code_verifier: str, state: str, redirect_uri: str
    ) -> Session:
        redirect = self._consume_state(state)
        if redirect != redirect_uri:
            # The redirect URI is bound to the state. Otherwise a flow started
            # for the vscode:// handler could be completed against a loopback
            # port an attacker controls.
            raise AuthError("the redirect URI does not match the one this flow started with")

        try:
            token = await self.identity.exchange(code, code_verifier, redirect_uri)
            profile = await self.identity.profile(token)
        except IdentityError as exc:
            raise AuthError(
                str(exc), retryable=exc.retryable, status=503 if exc.retryable else 401
            ) from exc

        return self._issue(profile, provider_token=token)

    # -- refresh -------------------------------------------------------------

    async def refresh(self, refresh_token: str) -> Session:
        """Rotate a refresh token, re-checking the account's standing.

        Two things happen here that do not happen anywhere else, and both are the
        point of having refresh at all: the account is re-checked with the IdP,
        so revocation is real without a deprovisioning step; and the old token is
        retired, so its reuse becomes a signal.
        """
        record = self._refresh.get(refresh_token)
        if record is None:
            raise AuthError("that refresh token is not recognised; sign in again")

        if record.family in self._revoked_families:
            raise AuthError("this session was revoked; sign in again")

        if record.used:
            # Reuse. Either the token was stolen and both parties are using it,
            # or a client replayed one. We cannot tell which, and the safe
            # reading of "cannot tell" is to end the family: a legitimate user
            # signs in again, and a thief loses everything they took.
            self._revoke_family(record.family)
            raise AuthError(
                "that refresh token was already used. For safety this session has "
                "been ended — sign in again."
            )

        if record.expires_at <= time.monotonic():
            raise AuthError("that refresh token has expired; sign in again")

        record.used = True

        profile = await self._recheck(record.sub, record.provider_token)
        if not profile.active:
            self._revoke_family(record.family)
            raise AuthError("this account is no longer active")

        return self._issue(
            profile, family=record.family, provider_token=record.provider_token
        )

    def revoke(self, refresh_token: str) -> None:
        record = self._refresh.get(refresh_token)
        if record is not None:
            self._revoke_family(record.family)

    # -- verification --------------------------------------------------------

    def verify(self, authorization: str | None) -> Claims:
        """Verify a bearer token from a request header."""
        if not authorization:
            raise AuthError("no Authorization header", status=401)
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError("Authorization must be 'Bearer <token>'", status=401)
        try:
            claims = self.minter.verify(token.strip())
        except TokenError as exc:
            raise AuthError(str(exc), status=401) from exc

        if claims.family and claims.family in self._revoked_families:
            # An access token minted from a family that has since been revoked.
            # Without this check a stolen access token stays good for its full
            # fifteen minutes after the theft was detected — which is exactly the
            # window the detection was meant to close.
            raise AuthError("this session was revoked; sign in again", status=401)
        return claims

    def require_write(self, claims: Claims) -> None:
        """§15.2's write-side control: the token must be freshly minted.

        Stricter than "not expired" on purpose. A developer who walked away and
        came back must not have a scaffold applied on a token issued before they
        left; refreshing is one round trip and re-establishes that somebody is
        there.
        """
        if not claims.fresh_enough_to_write(self._clock()):
            raise AuthError(
                "write actions need a recently issued token. Refresh and try again.",
                status=401,
            )

    # -- internals -----------------------------------------------------------

    def _issue(
        self, profile: Profile, family: str = "", *, provider_token: str = ""
    ) -> Session:
        if not profile.active:
            raise AuthError("this account is not active")

        roles = self.roles.roles_for(profile.groups)
        if not roles:
            raise AuthError(
                f"{profile.username} is not in a group that grants access. "
                "Membership of an it-2.0 group is what authorises use.",
                status=403,
            )

        family = family or secrets.token_urlsafe(16)
        access = self.minter.mint(
            sub=profile.sub, username=profile.username, roles=roles, family=family
        )
        refresh = secrets.token_urlsafe(32)
        self._refresh[refresh] = _Refresh(
            sub=profile.sub,
            family=family,
            expires_at=time.monotonic() + REFRESH_TTL.total_seconds(),
            provider_token=provider_token,
        )
        return Session(
            access_token=access,
            refresh_token=refresh,
            profile=profile,
            roles=roles,
            expires_at=self._clock() + self.minter.access_ttl,
        )

    async def _recheck(self, sub: str, provider_token: str = "") -> Profile:
        """Re-read the account from the IdP.

        A refresh that trusted the profile captured at sign-in would make the
        fifteen-minute expiry pointless: the whole reason for a short access
        token is that something re-asks this question.

        ``recheck`` is on the ``IdentityProvider`` protocol now. It was found
        with ``getattr`` and no production adapter had it, so this raised 501 in
        production and returned a session in CI — every real session died at
        fifteen minutes and asked for a full browser sign-in (BUG GW-1). The
        ``getattr`` remains only so an adapter written against the older,
        optional shape degrades with an explanation rather than an
        AttributeError.
        """
        recheck = getattr(self.identity, "recheck", None)
        if recheck is None:
            raise AuthError(
                "this identity provider cannot re-check an account, so refresh is "
                "unavailable. Sign in again.",
                status=501,
            )
        try:
            return await recheck(sub, provider_token)
        except IdentityError as exc:
            raise AuthError(
                str(exc), retryable=exc.retryable, status=503 if exc.retryable else 401
            ) from exc

    def _consume_state(self, state: str) -> str:
        """One use, then gone. An unknown state is always refused.

        No fallback for a state the gateway did not issue. Accepting one would
        make the check decorative — an attacker sends any value and passes — and
        a security control that can be satisfied by guessing is worse than none,
        because it reads as protection in a design review.

        The cost is that the extension must call ``start`` first, which §15.2's
        diagram does not show. That is the deviation, and it buys the thing the
        diagram's ``state`` cannot deliver: a CSRF check enforceable on the side
        that can enforce it.
        """
        self._sweep()
        entry = self._states.pop(state, None)
        if entry is None:
            raise AuthError(
                "that sign-in state is unknown, expired, or has already been used. "
                "Start the sign-in again."
            )
        return entry[0]

    def _revoke_family(self, family: str) -> None:
        self._revoked_families.add(family)
        for token, record in list(self._refresh.items()):
            if record.family == family:
                del self._refresh[token]

    def _sweep(self) -> None:
        now = time.monotonic()
        for state in [s for s, (_r, exp) in self._states.items() if exp <= now]:
            del self._states[state]
        for token in [t for t, r in self._refresh.items() if r.expires_at <= now]:
            del self._refresh[token]
