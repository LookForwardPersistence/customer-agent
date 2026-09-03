"""Server-side identity: bearer tokens binding a customer to a session.

Why not trust `session_id` from the client?
- Previously anyone could read/act on someone else's session (no auth on
  `/api/session/{sid}/state`, confirm, cancel) and order lookups were by
  order id only, so any order could be probed cross-customer.

Contract:
- `POST /api/session/new {"customer_id"}` issues `{session_id, customer_id, token}`.
- Every other endpoint requires `Authorization: Bearer <token>`; session
  identity is taken from the token, never from the request body.
- Tokens are unguessable, expire after a TTL, and each customer is capped at
  a small number of concurrent sessions (oldest evicted).
- CSRF: the token travels in a header (not a cookie), so cross-site request
  forgery does not apply.
"""

from __future__ import annotations

import contextvars
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException

from .persistence import build_backend

SESSION_TTL_SECONDS = 24 * 3600
MAX_SESSIONS_PER_CUSTOMER = 5


@dataclass(frozen=True)
class AuthenticatedCustomer:
    customer_id: str
    session_id: str


class TokenService:
    """Issues and resolves bearer tokens, persisted through a StateBackend.

    Tokens live in storage rather than process memory so a restart does not
    silently invalidate every active session (and so revocation/expiry survive
    a deploy). Reads hit the backend on every request — a primary-key lookup is
    microseconds, and correctness beats a cache here.
    """

    def __init__(
        self,
        ttl_seconds: int = SESSION_TTL_SECONDS,
        max_sessions_per_customer: int = MAX_SESSIONS_PER_CUSTOMER,
        backend: Any = None,
    ):
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._cap = max_sessions_per_customer
        self._backend = backend if backend is not None else build_backend()

    def issue(self, customer_id: str, session_id: str) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._evict_expired(now)
            live = [
                (t, meta["issued_at"])
                for t, meta in self._backend.load_tokens().items()
                if meta["customer_id"] == customer_id
            ]
            # Evict oldest-first until the new session fits under the cap.
            for t, _ in sorted(live, key=lambda x: x[1])[: max(0, len(live) - (self._cap - 1))]:
                self._backend.delete_token(t)
            self._backend.save_token(token, customer_id, session_id, now)
        return token

    def resolve(self, token: str) -> AuthenticatedCustomer | None:
        now = time.time()
        with self._lock:
            meta = self._backend.load_tokens().get(token)
            if meta is None:
                return None
            if now - meta["issued_at"] > self._ttl:
                self._backend.delete_token(token)
                return None
            return AuthenticatedCustomer(meta["customer_id"], meta["session_id"])

    def _evict_expired(self, now: float) -> None:
        for t, meta in self._backend.load_tokens().items():
            if now - meta["issued_at"] > self._ttl:
                self._backend.delete_token(t)

    def clear(self) -> None:
        with self._lock:
            self._backend.clear_tokens()


tokens = TokenService()


def get_customer(authorization: str = Header(default="")) -> AuthenticatedCustomer:
    """FastAPI dependency: resolve the bearer token to a customer + session."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            401, "缺少 Bearer token。请先调用 POST /api/session/new 创建会话。"
        )
    customer = tokens.resolve(token.strip())
    if customer is None:
        raise HTTPException(401, "token 无效或已过期，请重新创建会话。")
    return customer


# Request-scoped customer for code below the HTTP layer (agent tools). Set by
# the API layer per request; tools resolve it to scope order reads/writes to
# the authenticated customer instead of trusting whatever the LLM passes.
_current_customer_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_customer_id", default=None
)


def current_customer_id() -> str | None:
    return _current_customer_id.get()


def bind_customer(customer_id: str | None) -> contextvars.Token:
    return _current_customer_id.set(customer_id)


def unbind_customer(token: contextvars.Token) -> None:
    _current_customer_id.reset(token)
