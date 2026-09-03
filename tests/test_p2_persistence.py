"""P2-1 persistence: backend consistency + restart survival.

Two things are being proven here:

1. **Backend consistency matrix** — the same SessionStore/TokenService state
   machine produces byte-identical outcomes whether the backend is Memory or
   SQLite. The state machine lives in exactly one place (`app/store.py`);
   these tests are the tripwire that catches a backend-specific fork.

2. **Restart survival** — the point of P2-1. A SQLite-backed store survives
   process death: proposals stay confirmable, CONFIRMING orphans are swept to
   UNKNOWN by the startup pass, and bearer tokens keep resolving. Under the
   old in-memory store a restart silently invalidated every session and lost
   the UNKNOWN recovery path entirely.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from app.auth import TokenService
from app.persistence import MemoryBackend, SqliteBackend, build_backend
from app.store import (
    CONFIRMING,
    PROPOSED,
    SUCCEEDED,
    UNKNOWN,
    SessionStore,
)


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def sqlite_factory(tmp_path):
    path = tmp_path / "state.db"
    return lambda: SqliteBackend(path)


def memory_factory(_tmp_path):
    return lambda: MemoryBackend()


BACKEND_FACTORIES = [memory_factory, sqlite_factory]
BACKEND_NAMES = ["memory", "sqlite"]


@pytest.fixture(params=BACKEND_FACTORIES, ids=BACKEND_NAMES)
def backend(request, tmp_path):
    return request.param(tmp_path)()


@pytest.fixture(params=BACKEND_FACTORIES, ids=BACKEND_NAMES)
def store(request, tmp_path):
    return SessionStore(backend=request.param(tmp_path)())


# ---------------------------------------------------------------------------
# 1. Backend consistency matrix — same assertions, both engines
# ---------------------------------------------------------------------------


def test_full_action_lifecycle_is_backend_independent(store):
    """propose → supersede → confirm → finish produces identical outcomes."""
    s = "sess-1"
    first = store.propose(s, "create_return", {"order_id": "AT-10092"})
    second = store.propose(s, "create_return", {"order_id": "AT-10092"})

    # Second proposal supersedes the first — never silently executes the old one.
    assert store.get_action(s, first["action_id"])["state"] == "SUPERSEDED"
    assert store.get_action(s, second["action_id"])["state"] == PROPOSED

    action, err = store.begin_confirm(s, second["action_id"])
    assert err is None and action["state"] == CONFIRMING

    out = store.finish(s, second["action_id"], SUCCEEDED, {"ticket": "RT-1"})
    assert out["state"] == SUCCEEDED and out["result"] == {"ticket": "RT-1"}

    # Idempotent replay: re-finish returns the original outcome.
    replay = store.finish(s, second["action_id"], SUCCEEDED, {"ticket": "RT-2"})
    assert replay["result"] == {"ticket": "RT-1"}

    # Repeat confirm is rejected.
    again, reason = store.begin_confirm(s, second["action_id"])
    assert again is None and reason == "already_succeeded"


def test_snapshot_and_handoff_survive_backend_roundtrip(store):
    s = "sess-2"
    store.log(s, {"event": "user_message", "text": "AT-10092 的退货进度？"})
    store.propose(s, "create_return", {"order_id": "AT-10092"})
    # The API layer logs this on propose; mirror it so the handoff payload has attempts.
    store.log(s, {"event": "return_proposed", "order": "AT-10092"})

    record = store.set_handoff(s, "user_request", "客户要求人工跟进退货")
    payload = record["payload"]
    assert payload["order_ids"] == ["AT-10092"]
    assert payload["attempts"] and payload["attempts"][0]["event"] == "return_proposed"

    snap = store.snapshot(s)
    assert snap["handoff"]["id"] == record["id"]
    assert any(e["event"] == "user_message" for e in snap["events"])
    assert snap["pending_action"]["state"] == PROPOSED


def test_token_lifecycle_is_backend_independent(backend):
    svc = TokenService(backend=backend, max_sessions_per_customer=2)
    t1 = svc.issue("CUST-001", "s1")
    svc.issue("CUST-001", "s2")
    t3 = svc.issue("CUST-001", "s3")  # evicts t1 (oldest)

    assert svc.resolve(t1) is None
    assert svc.resolve(t3) is not None
    assert svc.resolve(t3).customer_id == "CUST-001"
    assert svc.resolve(t3).session_id == "s3"

    # Customer cap does not affect other customers.
    t_other = svc.issue("CUST-002", "s9")
    assert svc.resolve(t_other) is not None


def test_token_expiry_deletes_row(backend):
    svc = TokenService(backend=backend, ttl_seconds=0)
    tok = svc.issue("CUST-001", "s1")
    time.sleep(0.01)
    assert svc.resolve(tok) is None
    # Expired token was deleted, not merely masked.
    assert tok not in backend.load_tokens()


# ---------------------------------------------------------------------------
# 2. Restart survival — the reason P2-1 exists
# ---------------------------------------------------------------------------


def test_proposal_survives_restart(tmp_path):
    path = tmp_path / "state.db"
    store_a = SessionStore(backend=SqliteBackend(path))
    s = "sess-restart"
    action = store_a.propose(s, "create_return", {"order_id": "AT-10092"})
    del store_a  # process "dies"

    store_b = SessionStore(backend=SqliteBackend(path))
    after = store_b.get_action(s, action["action_id"])
    assert after is not None
    assert after["state"] == PROPOSED
    assert after["payload"] == {"order_id": "AT-10092"}

    # Still confirmable after the restart, and confirmation binds the same id.
    confirmed, err = store_b.begin_confirm(s, action["action_id"])
    assert err is None and confirmed["action_id"] == action["action_id"]
    assert store_b.finish(s, action["action_id"], SUCCEEDED, {"ticket": "RT-9"})
    assert store_b.get_action(s, action["action_id"])["state"] == SUCCEEDED


def test_orphaned_confirming_swept_to_unknown_after_restart(tmp_path):
    """CONFIRMING + crash → startup sweep must land on UNKNOWN, not stay stuck.

    This is the exact hole the review flagged: with in-memory state the
    recovery path had no data to recover *from*.
    """
    path = tmp_path / "state.db"
    store_a = SessionStore(backend=SqliteBackend(path))
    s = "sess-crash"
    action = store_a.propose(s, "create_return", {"order_id": "AT-10092"})
    store_a.begin_confirm(s, action["action_id"])

    # Backdate dispatched_at so the startup sweep sees it as timed out.
    data = store_a._backend.load(s)
    data["actions"][action["action_id"]]["dispatched_at"] = time.time() - 9999
    store_a._backend.save(s, data)
    del store_a

    store_b = SessionStore(backend=SqliteBackend(path))
    swept = store_b.sweep_stale_confirming()  # what main.lifespan runs at startup
    assert action["action_id"] in swept

    after = store_b.get_action(s, action["action_id"])
    assert after["state"] == UNKNOWN
    assert after["error"]["code"] == "CONFIRM_TIMEOUT"

    # Recovery is read-only: UNKNOWN cannot be re-confirmed.
    rejected, reason = store_b.begin_confirm(s, action["action_id"])
    assert rejected is None and reason == "already_unknown"


def test_succeeded_result_survives_restart_for_readback(tmp_path):
    path = tmp_path / "state.db"
    store_a = SessionStore(backend=SqliteBackend(path))
    s = "sess-readback"
    action = store_a.propose(s, "create_return", {"order_id": "AT-10092"})
    store_a.begin_confirm(s, action["action_id"])
    store_a.finish(s, action["action_id"], SUCCEEDED, {"ticket": "RT-42", "refund_amount": 499.0})
    del store_a

    store_b = SessionStore(backend=SqliteBackend(path))
    after = store_b.get_action(s, action["action_id"])
    assert after["state"] == SUCCEEDED
    assert after["result"]["ticket"] == "RT-42"
    assert after["result"]["refund_amount"] == 499.0


def test_tokens_survive_restart(tmp_path):
    path = tmp_path / "state.db"
    svc_a = TokenService(backend=SqliteBackend(path))
    tok = svc_a.issue("CUST-001", "s1")
    del svc_a

    svc_b = TokenService(backend=SqliteBackend(path))
    resolved = svc_b.resolve(tok)
    assert resolved is not None
    assert resolved.customer_id == "CUST-001"
    assert resolved.session_id == "s1"


# ---------------------------------------------------------------------------
# 3. build_backend configuration
# ---------------------------------------------------------------------------


def test_build_backend_modes(tmp_path, monkeypatch):
    assert isinstance(build_backend("memory"), MemoryBackend)
    path = tmp_path / "x.db"
    assert isinstance(build_backend("sqlite", path=str(path)), SqliteBackend)

    monkeypatch.setenv("PERSISTENCE", "memory")
    assert isinstance(build_backend(), MemoryBackend)
    monkeypatch.setenv("PERSISTENCE", "disk")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "y.db"))
    assert isinstance(build_backend(), SqliteBackend)

    with pytest.raises(ValueError):
        build_backend("redis")


def test_memory_backend_isolation_on_copy():
    """MemoryBackend.load returns a deep copy — mutating it must not corrupt the store."""
    b = MemoryBackend()
    b.save("s", {"actions": {"a1": {"state": "PROPOSED"}}, "events": [], "handoff": None})
    loaded = b.load("s")
    loaded["actions"]["a1"]["state"] = "HACKED"
    assert b.load("s")["actions"]["a1"]["state"] == "PROPOSED"


# ---------------------------------------------------------------------------
# 4. LangGraph checkpointer (conversation memory) follows the same mode
# ---------------------------------------------------------------------------


def test_checkpointer_follows_persistence_mode(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.checkpoint.sqlite import SqliteSaver

    from app.agent import build_checkpointer

    monkeypatch.setenv("PERSISTENCE", "memory")
    assert isinstance(build_checkpointer(), MemorySaver)

    monkeypatch.setenv("PERSISTENCE", "sqlite")
    monkeypatch.setenv("CHECKPOINT_DB_PATH", str(tmp_path / "ckpt.db"))
    assert isinstance(build_checkpointer(), SqliteSaver)


def test_conversation_checkpoint_survives_restart(tmp_path, monkeypatch):
    """Dialogue memory must be in the same crash domain as the action store.

    Otherwise a restart would leave a confirmable proposal whose conversation
    the agent no longer remembers.
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    db = str(tmp_path / "ckpt.db")
    cfg = {"configurable": {"thread_id": "sess-dialog", "checkpoint_ns": ""}}

    saver_a = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
    checkpoint = {
        "v": 4,
        "id": "ckpt-1",
        "ts": "2026-09-02T00:00:00+00:00",
        "channel_values": {"messages": ["退货方案已发送"]},
        "channel_versions": {},
        "versions_seen": {},
    }
    saver_a.put(cfg, checkpoint, {"source": "loop", "step": 1, "writes": {}}, {})
    saver_a.conn.commit()
    del saver_a  # process "dies"

    saver_b = SqliteSaver(sqlite3.connect(db, check_same_thread=False))
    restored = saver_b.get(cfg)
    assert restored is not None
    assert restored["channel_values"]["messages"] == ["退货方案已发送"]
