"""Machine callers, for the client-credentials grant (host-plan §8).

A JSON file an operator maintains: one entry per client, with the SHA-256 of its
secret (never the secret), the scopes it may be given, and the named person who
answers for it. The same rule as the repository allowlist: a credential nobody
owns is one nobody will ever revoke.

Kept separate from sign-in on purpose. Machine tokens carry a ``client:``
subject and the ``machine`` role, so the ledger tells them apart from people
and a client can be revoked by deleting one line, without touching anyone's
session.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from .scopes import SCOPES

__all__ = ["Client", "Clients", "digest"]


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Client:
    client_id: str
    secret_sha256: str
    scopes: tuple[str, ...]
    owner: str


class Clients:
    def __init__(self, clients: list[Client]) -> None:
        self._clients = {c.client_id: c for c in clients}

    @classmethod
    def load(cls, path: Path | None) -> "Clients | None":
        if path is None or not path.is_file():
            return None
        clients = []
        for raw in json.loads(path.read_text(encoding="utf-8")):
            client_id = str(raw.get("client_id") or "")
            if not client_id or not raw.get("owner") or not raw.get("secret_sha256"):
                raise ValueError(f"client {client_id!r} needs a client_id, an owner and a secret_sha256")
            scopes = tuple(raw.get("scopes") or ())
            unknown = [s for s in scopes if s not in SCOPES]
            if unknown or not scopes:
                raise ValueError(f"client {client_id!r}: unknown or missing scopes {unknown}")
            clients.append(Client(client_id, str(raw["secret_sha256"]).lower(), scopes, str(raw["owner"])))
        return cls(clients)

    def verify(self, client_id: str, secret: str) -> Client | None:
        client = self._clients.get(client_id)
        # Compared even for an unknown client, so the time it takes does not
        # say which client ids exist.
        expected = client.secret_sha256 if client else "0" * 64
        if not secrets.compare_digest(digest(secret), expected) or client is None:
            return None
        return client
