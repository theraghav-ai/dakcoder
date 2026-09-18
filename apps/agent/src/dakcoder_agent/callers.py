"""Who is calling the runtime, and which sessions are theirs (host-plan §8).

A loopback runtime has exactly one caller, the developer whose extension
spawned it, and proves it with the loopback token. A hosted runtime has many,
and every session route has to answer only for the caller's own sessions. A
missing filter there is a cross-tenant leak, which is why the check lives in one
place (``create_app``'s ``owned`` dependency) and a test walks the route table
to prove every session route uses it.

How a caller is authenticated is an ``Authenticator``: anything that turns a
request's headers into a ``Caller`` or raises ``Unauthorised``. There are two.

``loopback_token``, the default, is the extension talking to the runtime it
spawned.

``gateway_forwarded`` is a hosted runtime behind the gateway. The runtime never
sees a user's token and never holds the key that signs one: the gateway
verifies the caller's JWT, then forwards the request with the runtime's own
token and the caller's ``sub`` in ``X-Dakcoder-Caller``. The token proves the
gateway is the one saying who the caller is. It is not a signing key, so
reading it lets something call this runtime as anyone, and nothing more, and
anything able to read it from inside this process already holds every session
the process does.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from dakcoder_shared.contract import CALLER_HEADER

__all__ = [
    "CALLER_HEADER",
    "LOCAL",
    "Authenticator",
    "Caller",
    "Unauthorised",
    "gateway_forwarded",
    "loopback_token",
]

class Owned(Protocol):
    owner: str


@dataclass(frozen=True, slots=True)
class Caller:
    #: The caller's ``sub``. Empty for the local developer.
    sub: str = ""

    @property
    def local(self) -> bool:
        return not self.sub

    def owns(self, thing: Owned) -> bool:
        return thing.owner == self.sub


#: The one caller a loopback runtime has.
LOCAL = Caller()


class Unauthorised(Exception):
    """The request did not prove who it is from. Answered with 401."""


#: Turns a request's headers into a ``Caller``, or raises ``Unauthorised``.
Authenticator = Callable[[Mapping[str, str]], Caller]


def loopback_token(token: Callable[[], str]) -> Authenticator:
    """The loopback token: proves the caller is the extension that spawned us.

    Bound to 127.0.0.1, so this is not defending against the network. It is
    defending against *other processes on the same machine*, which on a
    developer laptop includes every npm postinstall script and browser
    extension that can reach localhost. ``secrets.compare_digest`` because a
    timing side channel on a local socket is entirely practical.

    ``token`` is read per request rather than captured, as it always was.
    """

    def authenticate(headers: Mapping[str, str]) -> Caller:
        _require_token(headers, token(), "invalid loopback token")
        return LOCAL

    return authenticate


def gateway_forwarded(token: Callable[[], str]) -> Authenticator:
    """A hosted runtime: every request comes through the gateway.

    Both parts are required. The token without a caller is refused rather than
    treated as the local developer, because a hosted runtime has no local
    developer, and the sessions a local caller owns (any written before this
    runtime was hosted) must stay out of every hosted caller's reach.
    """

    def authenticate(headers: Mapping[str, str]) -> Caller:
        _require_token(headers, token(), "only the gateway may call a hosted runtime")
        sub = (headers.get(CALLER_HEADER) or "").strip()
        if not sub:
            raise Unauthorised("the gateway did not say who the caller is")
        return Caller(sub=sub)

    return authenticate


def _require_token(headers: Mapping[str, str], token: str, refusal: str) -> None:
    authorization = headers.get("authorization") or ""
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {token}"):
        raise Unauthorised(refusal)
