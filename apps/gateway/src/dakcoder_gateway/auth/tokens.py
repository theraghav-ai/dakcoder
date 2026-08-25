"""Our own JWT, and why it is ours rather than GitLab's.

Part A §15.2: ``sub`` is the GitLab user id, plus ``preferred_username`` and
``dop_roles`` from group membership, with a fifteen-minute expiry.

Passing GitLab's token around instead would mean every service that wanted to
know who was calling had to ask GitLab, and every one of them would hold a token
that can read the caller's repositories. Our token carries the two facts the
gateway actually needs — who, and what they may do — and nothing else.

**Fifteen minutes is a revocation mechanism, not a nuisance.** Every refresh
re-checks the account's standing with GitLab, so a blocked account loses access
within one token lifetime and nobody has to remember to deprovision. Making the
access token long-lived would trade that away for one fewer round trip every
quarter of an hour.

**``mint_age`` exists for the write-side rule.** §15.2 keeps v1's control that
write tools need a token minted at most fifteen minutes ago. That is stricter
than "not expired": a refresh mints a new token, so the rule means a developer
who walked away and came back cannot have a scaffold applied on a token issued
before they left.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt

__all__ = ["Claims", "TokenError", "TokenMinter", "ACCESS_TTL", "REFRESH_TTL"]

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=30)

#: How recently a token must have been minted for a write-side tool. Equal to the
#: access TTL today, kept separate because they answer different questions and
#: will not always match.
WRITE_FRESHNESS = timedelta(minutes=15)

ALGORITHM = "HS256"
ISSUER = "dakcoder-gateway"
AUDIENCE = "dakcoder"


class TokenError(Exception):
    """A token that is absent, malformed, expired or not ours."""


@dataclass(frozen=True, slots=True)
class Claims:
    """A verified token's contents."""

    sub: str
    username: str
    roles: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime
    #: Ties an access token to the refresh family it came from, so revoking a
    #: family can invalidate tokens minted from it.
    family: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def age(self, now: datetime) -> timedelta:
        return now - self.issued_at

    def fresh_enough_to_write(self, now: datetime) -> bool:
        """§15.2's write-side control, inherited from v1 because it is a good one."""
        return self.age(now) <= WRITE_FRESHNESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "preferred_username": self.username,
            "dop_roles": list(self.roles),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class TokenMinter:
    """Mints and verifies the gateway's own tokens."""

    def __init__(
        self,
        secret: str,
        *,
        access_ttl: timedelta = ACCESS_TTL,
        clock=lambda: datetime.now(tz=timezone.utc),
    ) -> None:
        if not secret or len(secret) < 32:
            # Refused rather than warned about. A short signing secret is
            # forgeable, and a gateway that starts anyway is one where the
            # authentication is decorative — which is the exact failure this
            # whole section replaces.
            raise ValueError(
                "the JWT signing secret must be at least 32 characters. A short "
                "secret is forgeable, and forged tokens defeat attribution, quota "
                "and audit together."
            )
        self.secret = secret
        self.access_ttl = access_ttl
        self._clock = clock

    def mint(self, *, sub: str, username: str, roles: tuple[str, ...], family: str = "") -> str:
        now = self._clock()
        payload = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": sub,
            "preferred_username": username,
            "dop_roles": list(roles),
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + self.access_ttl).timestamp()),
        }
        if family:
            payload["fam"] = family
        return jwt.encode(payload, self.secret, algorithm=ALGORITHM)

    def verify(self, token: str) -> Claims:
        if not token:
            raise TokenError("no token supplied")
        try:
            payload = jwt.decode(
                token,
                self.secret,
                # The algorithm is pinned. Accepting whatever the header names is
                # the classic JWT failure: "none" and the RS256/HS256 confusion
                # both turn verification into a formality.
                algorithms=[ALGORITHM],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("the token has expired; refresh it") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"the token is not valid: {exc}") from exc

        return Claims(
            sub=str(payload["sub"]),
            username=str(payload.get("preferred_username", "")),
            roles=tuple(payload.get("dop_roles", ())),
            issued_at=datetime.fromtimestamp(payload["iat"], tz=timezone.utc),
            expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
            family=str(payload.get("fam", "")),
            raw=payload,
        )
