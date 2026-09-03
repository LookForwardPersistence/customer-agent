"""Pluggable persistence for agent state.

Why a backend abstraction instead of two stores
-----------------------------------------------
The action state machine (`app/store.py`) enforces at-most-once execution via
compare-and-set. Re-implementing that logic once per storage engine would be the
classic way to break it: two copies drift, and the drift shows up as a
double-executed return. So the state machine stays in exactly one place and this
module only provides *storage* — load / save a session's JSON, plus the token
table.

Design notes
------------
- `MemoryBackend` is the previous behaviour, kept for tests and for running with
  no filesystem access.
- `SqliteBackend` uses stdlib `sqlite3` — no new dependency. A session is one
  row holding its JSON, so a crash cannot tear a write: SQLite makes each
  single-statement write atomic, and WAL + `synchronous=FULL` makes committed
  data durable.
- Connections are per-thread (`threading.local`). FastAPI runs sync endpoints in
  a threadpool, and sharing one connection across threads is not safe.
- In-process atomicity for read-modify-write sequences (propose → confirm) comes
  from `SessionStore`'s lock. Cross-process CAS is deliberately NOT implemented:
  this runs as a single uvicorn worker. Multi-worker deployment needs either the
  conditional-UPDATE form or a Redis-backed store — the `StateBackend` protocol
  is the seam.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Protocol

DEFAULT_DB_PATH = ".data/agent_state.db"


def _empty_session() -> dict[str, Any]:
    return {"actions": {}, "handoff": None, "events": []}


class StateBackend(Protocol):
    """Storage for session state and bearer tokens."""

    # -- sessions ------------------------------------------------------------
    def load(self, sid: str) -> dict[str, Any] | None:
        """Return the session blob, or None if it does not exist."""

    def save(self, sid: str, data: dict[str, Any]) -> None: ...

    def all_sessions(self) -> dict[str, dict[str, Any]]: ...

    def delete(self, sid: str) -> None: ...

    def clear(self) -> None: ...

    # -- tokens --------------------------------------------------------------
    def load_tokens(self) -> dict[str, dict[str, Any]]: ...

    def save_token(self, token: str, customer_id: str, session_id: str, issued_at: float) -> None: ...

    def delete_token(self, token: str) -> None: ...

    def clear_tokens(self) -> None: ...


class MemoryBackend:
    """In-process storage. Fast, and empty on restart."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}

    def load(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._sessions.get(sid)
            return json.loads(json.dumps(data)) if data else None

    def save(self, sid: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._sessions[sid] = data

    def all_sessions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: json.loads(json.dumps(v)) for k, v in self._sessions.items()}

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def load_tokens(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return json.loads(json.dumps(self._tokens))

    def save_token(self, token: str, customer_id: str, session_id: str, issued_at: float) -> None:
        with self._lock:
            self._tokens[token] = {
                "customer_id": customer_id,
                "session_id": session_id,
                "issued_at": issued_at,
            }

    def delete_token(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)

    def clear_tokens(self) -> None:
        with self._lock:
            self._tokens.clear()


class SqliteBackend:
    """Durable storage: survives restarts and crashes."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH):
        self._path = str(path)
        parent = Path(self._path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    # -- connection ----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=15.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self._conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    sid        TEXT PRIMARY KEY,
                    data       TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tokens (
                    token       TEXT PRIMARY KEY,
                    customer_id TEXT NOT NULL,
                    session_id  TEXT NOT NULL,
                    issued_at   REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tokens_customer ON tokens(customer_id)")
            self._initialized = True

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT for multi-statement atomicity."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- sessions ------------------------------------------------------------

    def load(self, sid: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT data FROM sessions WHERE sid = ?", (sid,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, sid: str, data: dict[str, Any]) -> None:
        self._conn().execute(
            "INSERT INTO sessions (sid, data, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(sid) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            (sid, json.dumps(data, ensure_ascii=False, default=str), time.time()),
        )

    def all_sessions(self) -> dict[str, dict[str, Any]]:
        rows = self._conn().execute("SELECT sid, data FROM sessions").fetchall()
        return {sid: json.loads(data) for sid, data in rows}

    def delete(self, sid: str) -> None:
        self._conn().execute("DELETE FROM sessions WHERE sid = ?", (sid,))

    def clear(self) -> None:
        self._conn().execute("DELETE FROM sessions")

    # -- tokens --------------------------------------------------------------

    def load_tokens(self) -> dict[str, dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT token, customer_id, session_id, issued_at FROM tokens"
        ).fetchall()
        return {
            t: {"customer_id": c, "session_id": s, "issued_at": i}
            for t, c, s, i in rows
        }

    def save_token(self, token: str, customer_id: str, session_id: str, issued_at: float) -> None:
        self._conn().execute(
            "INSERT INTO tokens (token, customer_id, session_id, issued_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(token) DO UPDATE SET customer_id = excluded.customer_id, "
            "session_id = excluded.session_id, issued_at = excluded.issued_at",
            (token, customer_id, session_id, issued_at),
        )

    def delete_token(self, token: str) -> None:
        self._conn().execute("DELETE FROM tokens WHERE token = ?", (token,))

    def clear_tokens(self) -> None:
        self._conn().execute("DELETE FROM tokens")

    def close(self) -> None:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None


def build_backend(persistence: str | None = None, path: str | None = None) -> StateBackend:
    """Pick the backend from configuration.

    `PERSISTENCE=sqlite` (default) uses a file; `PERSISTENCE=memory` opts out.
    """
    mode = (persistence if persistence is not None else os.environ.get("PERSISTENCE", "sqlite"))
    mode = mode.strip().lower()
    if mode == "memory":
        return MemoryBackend()
    if mode in ("sqlite", "disk", "file"):
        return SqliteBackend(path or os.environ.get("SQLITE_PATH") or DEFAULT_DB_PATH)
    raise ValueError(f"unknown PERSISTENCE mode: {mode!r} (use 'sqlite' or 'memory')")
