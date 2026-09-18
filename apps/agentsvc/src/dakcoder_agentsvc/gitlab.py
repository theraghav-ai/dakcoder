"""Merge requests, through GitLab's REST API (host-plan §7.4).

Idempotent per branch: a second delivery of one session updates the merge
request the first opened, and never opens another. Looked up by source branch
as well as by the id the registry remembers, so a registry lost between the two
calls still finds the one already open.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

__all__ = ["GitLab", "GitLabError"]


class GitLabError(Exception):
    pass


class GitLab:
    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v4",
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=30.0,
            trust_env=False,
        )

    async def _json(self, method: str, url: str, **kw: Any) -> Any:
        try:
            response = await self._client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise GitLabError(f"GitLab did not answer: {exc}") from exc
        if response.status_code >= 400:
            raise GitLabError(f"GitLab refused {method} {url}: {response.status_code} {response.text[:300]}")
        return response.json()

    async def merge_request(
        self,
        project: str,
        *,
        source: str,
        target: str,
        title: str,
        description: str,
        iid: int | None = None,
    ) -> dict[str, Any]:
        """Open, or update, the merge request from ``source``. ``{iid, web_url}``."""
        base = f"/projects/{quote(project, safe='')}/merge_requests"
        if iid is None:
            open_ = await self._json("GET", base, params={"source_branch": source, "state": "opened"})
            if open_:
                iid = int(open_[0]["iid"])
        fields = {"title": title, "description": description}
        if iid is not None:
            mr = await self._json("PUT", f"{base}/{iid}", json=fields)
        else:
            mr = await self._json(
                "POST", base,
                json={**fields, "source_branch": source, "target_branch": target,
                      "remove_source_branch": True},
            )
        return {"iid": int(mr["iid"]), "web_url": str(mr["web_url"])}

    async def aclose(self) -> None:
        await self._client.aclose()
