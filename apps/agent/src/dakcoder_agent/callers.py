"""Who is calling the runtime, and which sessions are theirs (host-plan §8).

A loopback runtime has exactly one caller, the developer whose extension
spawned it, and proves it with the loopback token. A hosted runtime has many,
and every session route has to answer only for the caller's own sessions. A
missing filter there is a cross-tenant leak, which is why the check lives in one
place (``create_app``'s ``owned`` dependency) and a test walks the route table
to prove every session route uses it.

How a hosted caller is authenticated is an ``Authenticator``: anything that
turns an ``Authorization`` header into a ``Caller`` or raises ``Unauthorised``.
The loopback token is the default and the only one so far.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

__all__ = ["LOCAL", "Authenticator", "Caller", "Unauthorised", "loopback_token"]


class Owned(Protocol):
    owner: str


@dataclass(frozen=True, slots=True)
class Caller:
    #: The caller's ``sub``. Empty for the local developer.
    sub: str = ""

    def owns(self, thing: Owned) -> bool:
        return thing.owner == self.sub


#: The one caller a loopback runtime has.
LOCAL = Caller()


class Unauthorised(Exception):
    """The request did not prove who it is from. Answered with 401."""


#: Turns an ``Authorization`` header into a ``Caller``, or raises ``Unauthorised``.
Authenticator = Callable[[str | None], Caller]


def loopback_token(token: Callable[[], str]) -> Authenticator:
    """The loopback token: proves the caller is the extension that spawned us.

    Bound to 127.0.0.1, so this is not defending against the network. It is
    defending against *other processes on the same machine*, which on a
    developer laptop includes every npm postinstall script and browser
    extension that can reach localhost. ``secrets.compare_digest`` because a
    timing side channel on a local socket is entirely practical.

    ``token`` is read per request rather than captured, as it always was.
    """

    def authenticate(authorization: str | None) -> Caller:
        expected = f"Bearer {token()}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            raise Unauthorised("invalid loopback token")
        return LOCAL

    return authenticate
