"""The gateway's front for the hosted side (host-plan §3).

What sits behind ``/v1/runtime/{path}`` is either one hosted runtime (Phase 1)
or the control plane that routes to one per workspace (Phase 2). Either way it
is never published. The gateway verifies the caller's JWT, as it does on every
route, and forwards the request with the upstream's own token and the caller's
``sub`` in ``X-Dakcoder-Caller`` (``dakcoder_shared.forwarding``). The upstream
believes that header only alongside its token, so the one party that can say
who a caller is is the one that checked. Nothing behind the gateway ever sees a
user's token or holds the key that signs one.

On top of the forwarding rule, a path policy. Refused: any path outside the
``v1/`` API, and any with an empty, ``.`` or ``..`` segment, since a path that
normalises to somewhere else is a way around the first rule. Also
``POST v1/credential``, which is how the extension hands a *local* runtime the
developer's gateway token and means nothing for a hosted one (§6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import httpx

from dakcoder_shared.forwarding import Unreachable, Upstream

__all__ = ["RuntimeProxy", "RuntimeRefused", "RuntimeUnavailable"]

#: Routes a hosted caller may not reach, as (method, path).
REFUSED = frozenset({("POST", "v1/credential")})

#: The upstream did not answer.
RuntimeUnavailable = Unreachable


class RuntimeRefused(Exception):
    """A path this gateway does not forward. Answered as if it did not exist."""


class RuntimeProxy:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError(
                "the upstream's token is required: a hosted runtime answers nothing without it"
            )
        self._upstream = Upstream(base_url, token, transport=transport)

    @staticmethod
    def check(method: str, path: str) -> None:
        segments = path.split("/")
        if (
            not path.startswith("v1/")
            or "\\" in path
            or any(segment in ("", ".", "..") for segment in segments)
            or (method.upper(), path) in REFUSED
        ):
            raise RuntimeRefused(path)

    async def open(
        self,
        method: str,
        path: str,
        *,
        query: str,
        body: bytes,
        headers: Mapping[str, str],
        sub: str,
    ) -> httpx.Response:
        self.check(method, path)
        return await self._upstream.open(
            method, path, sub=sub, query=query, body=body, headers=headers
        )

    response_headers = staticmethod(Upstream.response_headers)

    @staticmethod
    def relay(response: httpx.Response) -> AsyncIterator[bytes]:
        return Upstream.relay(response)

    async def aclose(self) -> None:
        await self._upstream.aclose()
