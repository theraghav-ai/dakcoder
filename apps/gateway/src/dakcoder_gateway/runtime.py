"""The gateway's front for a hosted runtime (host-plan §3, Phase 1).

The runtime is never published. A caller reaches it through
``/v1/runtime/{path}`` here: the gateway verifies the caller's JWT, as it does
on every route, then forwards the request with the runtime's own token and the
caller's ``sub`` in ``X-Dakcoder-Caller``. A runtime started with ``--hosted``
believes that header only alongside its token, so the one party that can say
who a caller is is the one that checked. The runtime never sees a user's token
and never holds the key that signs one.

What is not forwarded, on purpose:

* The caller's ``Authorization``. Their JWT is ours to verify, not the
  runtime's to hold.
* Any ``X-Dakcoder-*`` header from the client. A client naming its own caller
  would be the whole attack.
* Everything else not on ``REQUEST_HEADERS``. An allow-list, because a header
  nobody decided to forward is one nobody reviewed.

What is refused: any path outside the runtime's ``v1/`` API, and any with an
empty, ``.`` or ``..`` segment, since a path that normalises to somewhere else
is a way around the first rule. Also ``POST v1/credential``, which is how the
extension hands a local runtime the developer's gateway token and means nothing
for a hosted one (§6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import httpx

from dakcoder_shared.contract import CALLER_HEADER

__all__ = ["RuntimeProxy", "RuntimeRefused", "RuntimeUnavailable"]

#: Headers a client may send the runtime. Lower case.
REQUEST_HEADERS = ("accept", "content-type", "last-event-id")

#: Headers the runtime's answer keeps on the way back. `x-accel-buffering`
#: matters: without it nginx holds a stream until it ends.
RESPONSE_HEADERS = ("content-type", "cache-control", "x-accel-buffering", "retry-after")

#: Routes a hosted caller may not reach, as (method, path).
REFUSED = frozenset({("POST", "v1/credential")})


class RuntimeRefused(Exception):
    """A path this gateway does not forward. Answered as if it did not exist."""


class RuntimeUnavailable(Exception):
    """The runtime did not answer. A 502: ours, not the caller's."""


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
                "the runtime's token is required: a hosted runtime answers nothing without it"
            )
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            # No read timeout: the event stream is meant to stay open for a
            # whole run, with keep-alive frames every fifteen seconds.
            timeout=httpx.Timeout(10.0, read=None),
            # The corporate proxy must never sit between the gateway and a
            # loopback service (see deploy/start.sh).
            trust_env=False,
        )

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
        """Send one request on the caller's behalf. The response is a stream;
        ``relay`` reads it and closes it."""
        self.check(method, path)
        forwarded = {name: value for name in REQUEST_HEADERS if (value := headers.get(name))}
        forwarded["authorization"] = f"Bearer {self._token}"
        forwarded[CALLER_HEADER] = sub
        url = f"/{path}?{query}" if query else f"/{path}"
        request = self._client.build_request(
            method, url, content=body or None, headers=forwarded
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise RuntimeUnavailable(f"the runtime did not answer: {exc}") from exc

    @staticmethod
    def response_headers(response: httpx.Response) -> dict[str, str]:
        return {
            name: value for name in RESPONSE_HEADERS if (value := response.headers.get(name))
        }

    @staticmethod
    async def relay(response: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
