"""Identity (Part A section 15, contract C3).

Replaces a shared bearer token plus a client-supplied user header — an
arrangement where anyone holding the token can claim any identity, so there is
no attribution, no meaningful quota, and no audit trail worth the name.
"""

from .identity import GitLabIdentity, IdentityError, IdentityProvider, Profile
from .service import AuthError, AuthService, RoleMap, Session, verifier_challenge
from .tokens import ACCESS_TTL, REFRESH_TTL, WRITE_FRESHNESS, Claims, TokenError, TokenMinter

__all__ = [
    "ACCESS_TTL",
    "REFRESH_TTL",
    "WRITE_FRESHNESS",
    "AuthError",
    "AuthService",
    "Claims",
    "GitLabIdentity",
    "IdentityError",
    "IdentityProvider",
    "Profile",
    "RoleMap",
    "Session",
    "TokenError",
    "TokenMinter",
    "verifier_challenge",
]
