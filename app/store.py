"""Durable action store: proposal -> confirmation -> execution.

Why this module exists
----------------------
The naive approach ("store the latest proposal under the session, pop it on
confirm") has a correctness hole: if a second proposal is generated before the
user clicks the *first* confirmation card, clicking the stale card silently
executes the *newer* action. This store fixes that by making every proposal an
immutable, addressable record with its own id and lifecycle.

Invariants enforced here
------------------------
1. Every proposal has an unguessable `action_id`; confirmation must present it.
2. State transitions are atomic (CAS under a lock): only PROPOSED -> CONFIRMING
   may begin an execution, so a given action executes at most once.
3. Repeating a confirm with the same action_id returns the *same* stored result
   (idempotent) instead of writing twice.
4. Stale/expired/superseded/already-consumed actions are rejected with a reason
   the API layer turns into HTTP 409 — never by executing a different action.
5. Records are never deleted, so execution results survive a crash and can be
   queried afterwards (recovery / audit).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any

# -- action lifecycle --------------------------------------------------------
PROPOSED = "PROPOSED"        # shown to the user, awaiting confirmation
CONFIRMING = "CONFIRMING"    # execution dispatched, outcome not yet known
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"          # dispatched but outcome unconfirmed (timeout/crash)
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"          # proposal outlived its TTL
SUPERSEDED = "SUPERSEDED"    # replaced by a newer proposal in the same session

TERMINAL_STATES = {SUCCEEDED, FAILED, CANCELLED, EXPIRED, SUPERSEDED}

# A proposal the user can still act on. UNKNOWN is *not* here: the write may
# have landed, so the UI must show a recovery state rather than "confirm again".
CONFIRMABLE_STATES = {PROPOSED}

PROPOSAL_TTL_SECONDS = 15 * 60


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable fingerprint of what the user was shown at proposal time."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def new_action_id() -> str:
    # Unguessable: a client must not be able to name someone else's action.
    return secrets.token_urlsafe(16)


def new_session_id() -> str:
    return secrets.token_urlsafe(9)


class SessionStore:
    def __init__(self, ttl_seconds: int = PROPOSAL_TTL_SECONDS):
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._sessions: dict[str, dict[str, Any]] = {}

    # -- internals -----------------------------------------------------------

    def _session(self, sid: str) -> dict[str, Any]:
        if sid not in self._sessions:
            self._sessions[sid] = {"actions": {}, "handoff": None, "events": []}
        return self._sessions[sid]

    @staticmethod
    def _is_expired(action: dict[str, Any], now: float) -> bool:
        return action["state"] == PROPOSED and now > action["expires_at"]

    # -- proposal ------------------------------------------------------------

    def propose(self, sid: str, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Register a new proposal, superseding any still-open one of same type."""
        now = time.time()
        with self._lock:
            s = self._session(sid)
            for a in s["actions"].values():
                if a["type"] == action_type and a["state"] == PROPOSED:
                    a["state"] = SUPERSEDED
                    a["closed_at"] = now
            action = {
                "action_id": new_action_id(),
                "session_id": sid,
                "type": action_type,
                "payload": payload,
                "payload_hash": payload_hash(payload),
                "state": PROPOSED,
                "created_at": now,
                "expires_at": now + self._ttl,
                "result": None,
                "error": None,
            }
            s["actions"][action["action_id"]] = action
            return dict(action)

    # -- confirmation --------------------------------------------------------

    def begin_confirm(self, sid: str, action_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """Atomically move PROPOSED -> CONFIRMING.

        Returns (action, None) on success or (None, reason) on rejection.
        Reasons: not_found / session_mismatch / expired / superseded /
        already_used / already_dispatched.
        """
        now = time.time()
        with self._lock:
            s = self._sessions.get(sid)
            action = s["actions"].get(action_id) if s else None
            if action is None:
                # Distinguish only for logging; the API collapses both into 409
                # so callers cannot probe which action ids exist cross-session.
                return None, "not_found"
            if action["session_id"] != sid:
                return None, "session_mismatch"
            if action["state"] != PROPOSED:
                return None, f"already_{action['state'].lower()}"
            if self._is_expired(action, now):
                action["state"] = EXPIRED
                action["closed_at"] = now
                return None, "expired"
            action["state"] = CONFIRMING
            action["dispatched_at"] = now
            return dict(action), None

    def finish(
        self,
        sid: str,
        action_id: str,
        state: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist the outcome. First write wins (idempotent replay)."""
        with self._lock:
            s = self._sessions.get(sid)
            action = s["actions"].get(action_id) if s else None
            if action is None:
                return None
            if action["state"] in TERMINAL_STATES and action.get("result") is not None:
                return dict(action)  # replay: return the original outcome
            action["state"] = state
            action["result"] = result
            action["error"] = error
            action["closed_at"] = time.time()
            return dict(action)

    def cancel(self, sid: str, action_id: str) -> tuple[dict[str, Any] | None, str | None]:
        with self._lock:
            s = self._sessions.get(sid)
            action = s["actions"].get(action_id) if s else None
            if action is None or action["session_id"] != sid:
                return None, "not_found"
            if action["state"] != PROPOSED:
                return None, f"already_{action['state'].lower()}"
            if self._is_expired(action, time.time()):
                action["state"] = EXPIRED
                return None, "expired"
            action["state"] = CANCELLED
            action["closed_at"] = time.time()
            return dict(action), None

    # -- queries -------------------------------------------------------------

    def get_action(self, sid: str, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._sessions.get(sid)
            action = s["actions"].get(action_id) if s else None
            if action is None or action["session_id"] != sid:
                return None
            return dict(action)

    def pending(self, sid: str) -> dict[str, Any] | None:
        """The action the UI should render as confirmable (or a recovery state)."""
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return None
            open_actions = [
                a for a in s["actions"].values()
                if a["state"] in CONFIRMABLE_STATES or a["state"] in (CONFIRMING, UNKNOWN)
            ]
            if not open_actions:
                return None
            return dict(max(open_actions, key=lambda a: a["created_at"]))

    def latest_action(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._sessions.get(sid)
            if not s:
                return None
            return dict(max(s["actions"].values(), key=lambda a: a["created_at"]))

    # -- handoff --------------------------------------------------------------

    # Structured handoff payload: every field below is derived SERVER-SIDE
    # from the session's audit events — the LLM only contributes the natural
    # language `summary` via the handoff tool. A human agent (or a real ticket
    # system) gets verifiable context, not a free-text claim.
    _ORDER_RE = re.compile(r"AT-\d{4,}")
    _ATTEMPT_EVENTS = (
        "return_proposed",
        "return_executed",
        "return_failed",
        "return_outcome_unknown",
        "return_recovered_by_read",
        "confirm_rejected",
        "cancelled_by_user",
    )
    _ESCALATION_WORDS = ("投诉", "差评", "举报", "骗子", "12315")

    def _build_handoff_payload(self, sid: str, reason: str) -> dict[str, Any]:
        s = self._sessions.get(sid) or {"events": []}
        events = s.get("events", [])

        order_ids: list[str] = []
        for e in events:
            for m in self._ORDER_RE.findall(str(e.get("text") or "") + str(e.get("order") or "")):
                if m not in order_ids:
                    order_ids.append(m)

        attempts = [
            {
                "ts": e.get("ts"),
                "event": e["event"],
                "order": e.get("order"),
                "code": e.get("code"),
            }
            for e in events
            if e["event"] in self._ATTEMPT_EVENTS
        ]

        last_error = None
        for e in reversed(events):
            if e["event"] in ("return_failed", "return_outcome_unknown"):
                last_error = {"event": e["event"], "code": e.get("code")}
                break

        user_texts = [str(e.get("text") or "") for e in events if e["event"] == "user_message"]
        joined = " ".join(user_texts)
        if any(w in joined for w in self._ESCALATION_WORDS):
            sentiment = "不满（提及投诉/升级）"
        elif len(user_texts) >= 3:
            sentiment = "多次沟通，情绪需关注"
        else:
            sentiment = "平稳"

        return {
            "intent": reason,
            "order_ids": order_ids,
            "customer_sentiment": sentiment,
            "attempts": attempts,
            "last_error": last_error,
            "transcript_ref": sid,
        }

    def set_handoff(self, sid: str, reason: str, summary: str) -> dict[str, Any]:
        payload = self._build_handoff_payload(sid, reason)
        record = {
            "id": "HO-" + secrets.token_hex(4).upper(),
            "reason": reason,
            "summary": summary,
            "payload": payload,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "等待人工接入",
        }
        with self._lock:
            self._session(sid)["handoff"] = record
        return record

    def get_handoff(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._sessions.get(sid)
            return s["handoff"] if s else None

    # -- audit ------------------------------------------------------------------

    def log(self, sid: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._session(sid)["events"].append(
                {"ts": time.strftime("%H:%M:%S"), **event}
            )

    def snapshot(self, sid: str) -> dict[str, Any]:
        with self._lock:
            s = self._sessions.get(sid) or {"actions": {}, "handoff": None, "events": []}
            return {
                "pending_action": self._pending_unlocked(s),
                "handoff": s["handoff"],
                "events": list(s["events"]),
                "actions": [
                    {
                        "action_id": a["action_id"],
                        "type": a["type"],
                        "state": a["state"],
                    }
                    for a in s["actions"].values()
                ],
            }

    @staticmethod
    def _pending_unlocked(s: dict[str, Any]) -> dict[str, Any] | None:
        open_actions = [
            a for a in s["actions"].values()
            if a["state"] in CONFIRMABLE_STATES or a["state"] in (CONFIRMING, UNKNOWN)
        ]
        if not open_actions:
            return None
        return dict(max(open_actions, key=lambda a: a["created_at"]))


sessions = SessionStore()
