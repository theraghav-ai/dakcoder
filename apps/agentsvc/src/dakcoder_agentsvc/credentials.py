"""A runner's credential at the gateway: minted for its lease's owner (host-plan §8).

A runner calls the model through the gateway's ``/v1/llm``, and whoever its
token names is who quota and the ledger charge. One service token for every
runner would charge one account for every hosted run. So each runner gets a
token *delegated for its lease's owner*: the control plane asks the gateway's
``/v1/auth/delegate`` with its own ``delegate``-scoped token, and gets back one
that names the owner and may be used for model traffic and nothing else (a
token stolen from inside a runner cannot act as the owner anywhere else).

Delegated tokens expire. A runner can outlive its token, so the reaper pushes a
fresh one through the runtime's ``POST /v1/credential`` before the old one runs
out, the same route the extension uses for the same reason.

``static`` is the phase 2 arrangement, kept as a fallback: one token for every
runner, and every hosted run charged to whoever it names.
"""

from __future__ import annotations

import logging
import math
import time

import httpx

from .runners import RunnerFailed

__all__ = ["Credentials"]

log = logging.getLogger(__name__)


class Credentials:
    def __init__(
        self,
        gateway_url: str,
        *,
        delegator_jwt: str = "",
        static: str = "",
        hours: float = 12.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock=time.time,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.delegator_jwt = delegator_jwt
        self.static = static
        self.hours = hours
        self.clock = clock
        self._transport = transport
        if not delegator_jwt and static:
            log.warning(
                "runners share one gateway token: every hosted run is charged to the "
                "account it names. Set DAKCODER_AGENTSVC_GATEWAY_JWT to charge each owner."
            )

    async def for_owner(self, sub: str) -> tuple[str, float]:
        """A token for ``sub``'s runner, and when it expires (epoch seconds)."""
        if not self.delegator_jwt:
            if self.static:
                return self.static, math.inf
            raise RunnerFailed("no gateway credential for runners is configured")
        async with httpx.AsyncClient(
            base_url=self.gateway_url, transport=self._transport, timeout=15.0, trust_env=False
        ) as http:
            try:
                response = await http.post(
                    "/v1/auth/delegate",
                    json={"sub": sub, "hours": self.hours},
                    headers={"Authorization": f"Bearer {self.delegator_jwt}"},
                )
            except httpx.HTTPError as exc:
                raise RunnerFailed(f"the gateway did not answer a delegation: {exc}") from exc
        if response.status_code != 200:
            raise RunnerFailed(f"the gateway refused a delegation: {response.status_code}")
        body = response.json()
        return str(body["access_token"]), self.clock() + float(body.get("expires_in", 0))
