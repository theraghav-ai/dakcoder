"""Fakes for the gateway tests: a GitLab and a LiteLLM.

Both are configurable rather than canned, because the interesting tests are the
ones where the far side misbehaves — a refused code, an unreachable host, a
stream that drops halfway, a missing usage chunk. None of those can be rehearsed
against the real thing, and all of them will happen.
"""

from __future__ import annotations

from dakcoder_gateway.auth import IdentityError, Profile

SECRET = "a" * 48
REDIRECT = "vscode://dop.dakcoder-go/auth/callback"
VERIFIER = "v" * 43
API_KEY = "sk-the-one-shared-secret"


class FakeGitLab:
    """A GitLab that does what it is told, so the tests can say what it did."""

    def __init__(self, profile: Profile | None = None) -> None:
        self.profile_data = profile or Profile(
            sub="gitlab:7",
            username="asha",
            name="Asha R",
            email="asha@indiapost.gov.in",
            groups=("it-2.0/pension-api",),
        )
        self.codes: dict[str, str] = {"good-code": "gitlab-access-token"}
        self.exchanges: list[tuple[str, str, str]] = []
        self.unreachable = False
        self.rechecks = 0

    def authorize_url(self, redirect_uri: str, challenge: str, state: str) -> str:
        return (
            f"https://gitlab.cept.gov.in/oauth/authorize"
            f"?state={state}&code_challenge={challenge}"
        )

    async def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> str:
        if self.unreachable:
            raise IdentityError("connection refused", retryable=True)
        self.exchanges.append((code, code_verifier, redirect_uri))
        # Authorization codes are single use, as GitLab's are. A test that could
        # replay one would not notice a gateway that allowed it.
        token = self.codes.pop(code, None)
        if token is None:
            raise IdentityError("that authorization code is not valid")
        return token

    async def profile(self, access_token: str) -> Profile:
        return self.profile_data

    async def recheck(self, sub: str) -> Profile:
        self.rechecks += 1
        if self.unreachable:
            raise IdentityError("connection refused", retryable=True)
        return self.profile_data


class FakeUpstream:
    """A LiteLLM that streams what it is told to."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.status = 200
        self.error_body = b'{"error":{"message":"model not found"}}'
        self.chunks: list[str] = [
            'data: {"model":"Qwen3.8-27B","choices":[{"delta":{"content":"package "}}]}',
            'data: {"choices":[{"delta":{"content":"handler"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"usage":{"prompt_tokens":1200,"completion_tokens":340,'
            '"completion_tokens_details":{"reasoning_tokens":0}}}',
            "data: [DONE]",
        ]
        #: Index at which the connection drops, for the mid-stream tests.
        self.explode_after: int | None = None

    def stream(self, method, url, *, json=None, headers=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        return _Response(self)


class _Response:
    def __init__(self, upstream: FakeUpstream) -> None:
        self.upstream = upstream
        self.status_code = upstream.status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aread(self) -> bytes:
        return self.upstream.error_body

    async def aiter_lines(self):
        for index, chunk in enumerate(self.upstream.chunks):
            if self.upstream.explode_after is not None and index == self.upstream.explode_after:
                raise ConnectionError("the stream dropped")
            yield chunk
            # The blank line that terminates an SSE event. httpx yields it as an
            # empty string, which is exactly what the relay has to put back.
            yield ""
