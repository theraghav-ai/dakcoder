"""The control plane's registry: leases, which session is on which, deliveries.

SQLite, from the standard library. It is one file on the control plane's own
volume, it survives a restart, and it needs no service of its own. What it holds
is small: the sessions themselves (journals, transcripts, plans) stay on each
lease's volume, where the runtime writes them (host-plan §9.2).

Every read that a caller can reach takes the caller's ``sub`` and filters on it.
A lookup that forgets the owner is the cross-tenant leak §8 warns about, so the
methods that return one caller's rows are the only ones the routes use.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = ["Delivery", "Lease", "SessionRow", "Store"]

_SCHEMA = """
create table if not exists leases (
    id text primary key,
    owner text not null,
    repo_url text not null,
    ref text not null,
    path text not null,
    created_at real not null,
    expires_at real not null,
    last_used_at real not null
);
create index if not exists leases_owner on leases(owner);
create table if not exists sessions (
    id text primary key,
    lease_id text not null,
    owner text not null,
    branch text not null,
    task text not null,
    created_at real not null,
    summary text not null default '{}'
);
create index if not exists sessions_owner on sessions(owner);
create table if not exists deliveries (
    session_id text primary key,
    branch text not null,
    commit_sha text not null,
    mr_iid integer,
    mr_url text not null default '',
    updated_at real not null
);
"""


@dataclass(frozen=True)
class Lease:
    id: str
    owner: str
    repo_url: str
    ref: str
    path: str
    created_at: float
    expires_at: float
    last_used_at: float

    @property
    def repo(self) -> Path:
        return Path(self.path)

    def public(self) -> dict[str, Any]:
        """What a caller is shown: never the path on the server."""
        return {
            "id": self.id,
            "repo_url": self.repo_url,
            "ref": self.ref,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_used_at": self.last_used_at,
        }


@dataclass(frozen=True)
class SessionRow:
    id: str
    lease_id: str
    owner: str
    branch: str
    task: str
    created_at: float
    summary: dict[str, Any]


@dataclass(frozen=True)
class Delivery:
    session_id: str
    branch: str
    commit_sha: str
    mr_iid: int | None
    mr_url: str
    updated_at: float

    def public(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def _all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _run(self, sql: str, *args: Any) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # -- leases -------------------------------------------------------------

    def add_lease(self, lease: Lease) -> None:
        self._run(
            "insert into leases values (?, ?, ?, ?, ?, ?, ?, ?)",
            lease.id, lease.owner, lease.repo_url, lease.ref, lease.path,
            lease.created_at, lease.expires_at, lease.last_used_at,
        )

    def lease(self, lease_id: str, owner: str) -> Lease | None:
        rows = self._all("select * from leases where id = ? and owner = ?", lease_id, owner)
        return Lease(**dict(rows[0])) if rows else None

    def leases(self, owner: str) -> list[Lease]:
        rows = self._all("select * from leases where owner = ? order by created_at", owner)
        return [Lease(**dict(r)) for r in rows]

    def all_leases(self) -> list[Lease]:
        """For the reaper only. Every other reader names an owner."""
        return [Lease(**dict(r)) for r in self._all("select * from leases")]

    def touch_lease(self, lease_id: str, now: float | None = None) -> None:
        self._run("update leases set last_used_at = ? where id = ?", now or time.time(), lease_id)

    def remove_lease(self, lease_id: str) -> None:
        self._run("delete from leases where id = ?", lease_id)

    # -- sessions -----------------------------------------------------------

    def add_session(self, row: SessionRow) -> None:
        self._run(
            "insert into sessions values (?, ?, ?, ?, ?, ?, ?)",
            row.id, row.lease_id, row.owner, row.branch, row.task, row.created_at,
            json.dumps(row.summary),
        )

    def session(self, session_id: str, owner: str) -> SessionRow | None:
        rows = self._all("select * from sessions where id = ? and owner = ?", session_id, owner)
        return _session(rows[0]) if rows else None

    def sessions(self, owner: str, lease_id: str | None = None) -> list[SessionRow]:
        if lease_id is None:
            rows = self._all(
                "select * from sessions where owner = ? order by created_at desc", owner
            )
        else:
            rows = self._all(
                "select * from sessions where owner = ? and lease_id = ? "
                "order by created_at desc",
                owner, lease_id,
            )
        return [_session(r) for r in rows]

    def lease_sessions(self, lease_id: str) -> list[SessionRow]:
        """For release and the reaper, which act on a lease already owned."""
        rows = self._all("select * from sessions where lease_id = ?", lease_id)
        return [_session(r) for r in rows]

    def remember(self, session_id: str, summary: dict[str, Any]) -> None:
        """The last summary a runner gave for a session, for listing it while its
        runner is stopped."""
        self._run(
            "update sessions set summary = ? where id = ?", json.dumps(summary), session_id
        )

    def forget_session(self, session_id: str) -> None:
        self._run("delete from sessions where id = ?", session_id)

    # -- deliveries ---------------------------------------------------------

    def delivery(self, session_id: str) -> Delivery | None:
        rows = self._all("select * from deliveries where session_id = ?", session_id)
        return Delivery(**dict(rows[0])) if rows else None

    def save_delivery(self, delivery: Delivery) -> None:
        self._run(
            "insert or replace into deliveries values (?, ?, ?, ?, ?, ?)",
            delivery.session_id, delivery.branch, delivery.commit_sha, delivery.mr_iid,
            delivery.mr_url, delivery.updated_at,
        )


def _session(row: sqlite3.Row) -> SessionRow:
    data = dict(row)
    data["summary"] = json.loads(data["summary"] or "{}")
    return SessionRow(**data)
