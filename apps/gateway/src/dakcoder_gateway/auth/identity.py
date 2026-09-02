"""The identity provider, behind a port.

Part A §15.2's last design point: ``IdentityProvider`` with ``exchange()``,
``profile()`` and ``groups()``. GitLab is adapter one; an India Post SSO OIDC
adapter is a config swap rather than a rewrite, and the tenant extractor stays a
single function.

That is not architecture for its own sake. The thing being replaced is a shared
bearer token plus a client-supplied ``X-Postgen-User`` header — anyone holding
the token can claim any identity, so there is no attribution, no meaningful
quota, and no audit trail worth the name. Whatever replaces it will outlive the
choice of IdP, and the seam is where that choice lives.

**The gateway does the exchange, never the extension.** Extension code is
inspectable, so a client secret in it is an announcement rather than a control.
The extension performs PKCE as a public client and hands the gateway a code; the
gateway holds the secret, exchanges it, and is the only party that ever sees a
GitLab token or reads group membership.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = ["GitLabIdentity", "IdentityError", "IdentityProvider", "Profile"]


class IdentityError(Exception):
    """The provider refused, or could not be reached.

    One class for both, with ``retryable`` telling them apart. A refused code and
    an unreachable GitLab need very different responses from the caller — retry
    one, never the other — and collapsing them is how a sign-in failure becomes
    an infinite retry loop.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Profile:
    """Who the developer is, as the IdP reports them."""

    sub: str
    username: str
    name: str = ""
    email: str = ""
    #: Group paths, e.g. "it-2.0-common/platform". Mapped to roles by config.
    groups: tuple[str, ...] = ()
    #: True when the account is blocked, deactivated or otherwise not in good
    #: standing. Checked on every refresh, which is what makes revocation real:
    #: a blocked account loses access within one token lifetime and nobody has
    #: to run a deprovisioning step.
    active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


class IdentityProvider(Protocol):
    async def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        """Trade an authorization code for the provider's own access token."""
        ...

    async def profile(self, access_token: str) -> Profile:
        """Who that token belongs to, including group membership."""
        ...

    async def recheck(self, sub: str, access_token: str = "") -> Profile:
        """Re-read the account, for a refresh.

        Part of the protocol rather than an optional attribute discovered with
        ``getattr``. It was optional, no production adapter implemented it, and
        the only implementation in the tree was the test fake — so
        ``/v1/auth/refresh`` answered 501 in production and 200 in CI, every
        session died at the fifteen-minute access-token TTL, and the developer
        was sent through a full browser OAuth flow four times an hour (BUG GW-1).
        A protocol member makes the absence a type error instead of a runtime
        surprise nobody could see from the tests.
        """
        ...


class GitLabIdentity:
    """The GitLab adapter.

    Scopes are ``openid profile email read_api``. ``read_api`` is needed for
    ``/api/v4/groups?min_access_level=…`` and nothing here needs write scope —
    which matters, because a token that can only read is a token whose leak is a
    disclosure rather than a compromise.
    """

    SCOPES = "openid profile email read_api"

    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        http: Any = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._http = http

    def authorize_url(self, redirect_uri: str, challenge: str, state: str) -> str:
        from urllib.parse import urlencode

        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "state": state,
                "scope": self.SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.base_url}/oauth/authorize?{query}"

    async def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        data = await self._post("/oauth/token", payload)
        token = data.get("access_token")
        if not token:
            raise IdentityError("GitLab returned no access token for that code")
        return str(token)

    async def profile(self, access_token: str) -> Profile:
        user = await self._get("/api/v4/user", access_token)
        groups = await self._get(
            "/api/v4/groups", access_token, params={"min_access_level": 10, "per_page": 100}
        )
        paths = tuple(str(g.get("full_path", "")) for g in groups if g.get("full_path"))

        state = str(user.get("state", "active")).lower()
        return Profile(
            sub=f"gitlab:{user['id']}",
            username=str(user.get("username", "")),
            name=str(user.get("name", "")),
            email=str(user.get("email", "")),
            groups=paths,
            # GitLab reports "active", "blocked", "deactivated", "ldap_blocked".
            # Anything that is not plainly active is treated as not active: an
            # unknown state must not read as good standing.
            active=state == "active",
            raw={"id": user.get("id"), "state": state},
        )

    async def recheck(self, sub: str, access_token: str = "") -> Profile:
        """Re-read the account with the provider token captured at sign-in.

        Re-asking is the entire reason the access token is short-lived: a blocked
        or deactivated account loses access within one token lifetime and nobody
        has to run a deprovisioning step. It is the *user's* own token rather
        than a service credential, which keeps the gateway free of an
        administrative GitLab token — the blast radius of a leak here stays the
        one account.

        The identity is verified rather than assumed: if the token now answers
        for a different account, the session it belongs to is not this one.
        """
        if not access_token:
            raise IdentityError(
                "no provider credential is held for that session; sign in again"
            )
        profile = await self.profile(access_token)
        if profile.sub != sub:
            raise IdentityError(
                "the stored credential no longer identifies that account; sign in again"
            )
        return profile

    # -- transport ---------------------------------------------------------

    async def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        # trust_env=False for the same reason as the model client: a developer's
        # proxy variables must not silently redirect an OAuth exchange.
        return httpx.AsyncClient(timeout=self.timeout, trust_env=False, http2=False)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        client = await self._client()
        try:
            response = await client.post(f"{self.base_url}{path}", data=payload)
        except Exception as exc:  # noqa: BLE001 - transport failures are retryable
            raise IdentityError(f"could not reach GitLab: {exc}", retryable=True) from exc
        return self._decode(response, path)

    async def _get(
        self, path: str, token: str, params: dict[str, Any] | None = None
    ) -> Any:
        client = await self._client()
        try:
            response = await client.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        except Exception as exc:  # noqa: BLE001
            raise IdentityError(f"could not reach GitLab: {exc}", retryable=True) from exc
        return self._decode(response, path)

    @staticmethod
    def _decode(response: Any, path: str) -> Any:
        if response.status_code >= 500:
            raise IdentityError(
                f"GitLab returned {response.status_code} for {path}", retryable=True
            )
        if response.status_code >= 400:
            # The body is not included. It can carry the authorization code, and
            # this message reaches logs and, through the extension, a screen.
            raise IdentityError(f"GitLab rejected {path} with {response.status_code}")
        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise IdentityError(f"GitLab sent a non-JSON response for {path}") from exc
