"""In-process session store: pending actions, handoff records.

Kept separate from LangGraph's message memory so the confirmation
state machine is explicit and easy to reason about / test.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any


class SessionStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}

    def _session(self, sid: str) -> dict[str, Any]:
        if sid not in self._data:
            self._data[sid] = {
                "pending_action": None,   # proposed-but-unconfirmed action
                "handoff": None,          # handoff record with retained context
                "events": [],             # lightweight audit log
            }
        return self._data[sid]

    # -- pending action (confirm / cancel state machine) ----------------------

    def set_pending(self, sid: str, action: dict[str, Any]) -> None:
        with self._lock:
            self._session(sid)["pending_action"] = {
                **action, "proposed_at": time.strftime("%Y-%m-%d %H:%M:%S")
            }

    def pop_pending(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            s = self._session(sid)
            pa = s["pending_action"]
            s["pending_action"] = None
            return pa

    def get_pending(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._session(sid).get("pending_action")

    # -- handoff ----------------------------------------------------------------

    def set_handoff(self, sid: str, reason: str, context: str) -> dict[str, Any]:
        record = {
            "id": "HO-" + uuid.uuid4().hex[:8].upper(),
            "reason": reason,
            "context": context,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "等待人工接入",
        }
        with self._lock:
            self._session(sid)["handoff"] = record
        return record

    def get_handoff(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._session(sid).get("handoff")

    # -- audit ------------------------------------------------------------------

    def log(self, sid: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._session(sid)["events"].append(
                {"ts": time.strftime("%H:%M:%S"), **event}
            )

    def snapshot(self, sid: str) -> dict[str, Any]:
        with self._lock:
            s = self._session(sid)
            return {
                "pending_action": s["pending_action"],
                "handoff": s["handoff"],
                "events": list(s["events"]),
            }


sessions = SessionStore()
