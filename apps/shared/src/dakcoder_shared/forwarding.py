"""Forward a verified caller's request to the service behind us.

Two hops use this: the gateway in front of the control plane (or a hosted
runtime), and the control plane in front of each workspace's runner. Both follow
one rule. The upstream receives *our* token for it and the caller the previous
hop verified, in ``X-Dakcoder-Caller``, and nothing else from the client that
nobody decided to forward. In particular never the client's own
``Authorization`` or any ``X-Dakcoder-*`` header: a client naming its own caller
is the whole attack.

One implementation, so the two hops cannot drift apart on that rule.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from .contract import CALLER_HEADER

__all__ = ["REQUEST_HEADERS", "RESPONSE_HEADERS", "Unreachable", "Upstream"]

#: Headers a client may send upstream. Lower case.
REQUEST_HEADERS = ("accept", "content-type", "last-event-id")

#: Headers an upstream's answer keeps on the way back. `x-accel-buffering`
#: matters: without it nginx holds a stream until it ends.
RESPONSE_HEADERS = ("content-type", "cache-control", "x-accel-buffering", "retry-after")


class Unreachable(Exception):
    """The upstream did not answer. A 502: ours, not the caller's."""


class Upstream:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise ValueError("an upstream's token is required: it answers nothing without one")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            # No read timeout: an event stream stays open for a whole run, with
            # keep-alive frames every fifteen seconds.
            timeout=httpx.Timeout(10.0, read=None),
            # A corporate proxy must never sit between two services on one host.
            trust_env=False,
        )

    def _headers(self, incoming: Mapping[str, str], sub: str) -> dict[str, str]:
        forwarded = {name: value for name in REQUEST_HEADERS if (value := incoming.get(name))}
        forwarded["authorization"] = f"Bearer {self._token}"
        forwarded[CALLER_HEADER] = sub
        return forwarded

    async def open(
        self,
        method: str,
        path: str,
        *,
        sub: str,
        query: str = "",
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send one request for ``sub``. The response is a stream: ``relay`` it,
        or read it and ``aclose`` it."""
        url = f"/{path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        request = self._client.build_request(
            method, url, content=body or None, headers=self._headers(headers or {}, sub)
        )
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise Unreachable(f"{self.base_url} did not answer: {exc}") from exc

    async def call(
        self, method: str, path: str, *, sub: str, json: Any = None, query: str = ""
    ) -> tuple[int, Any]:
        """A whole JSON exchange, for a service talking to its upstream itself."""
        body = b"" if json is None else httpx.Request("POST", "/", json=json).content
        headers = {"content-type": "application/json"} if json is not None else {}
        response = await self.open(method, path, sub=sub, query=query, body=body, headers=headers)
        try:
            await response.aread()
        finally:
            await response.aclose()
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {"error": response.text[:500]}

    @staticmethod
    def response_headers(response: httpx.Response) -> dict[str, str]:
        return {
            name: value for name in RESPONSE_HEADERS if (value := response.headers.get(name))
        }

    @staticmethod
    async def relay(response: httpx.Response) -> AsyncIterator[bytes]:
        """The body, decoded. Decoded because ``Content-Encoding`` is not among
        the headers passed back: relaying the raw bytes of a compressed answer
        without it would hand the client something it cannot read."""
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
