"""P0-1 / P0-2 核心：SessionStore 动作生命周期与原子确认。"""

import time

import pytest

from app.store import (
    CANCELLED,
    CONFIRMING,
    EXPIRED,
    FAILED,
    PROPOSED,
    SUCCEEDED,
    SUPERSEDED,
    UNKNOWN,
    SessionStore,
)


def test_action_id_is_unguessable_and_unique():
    store = SessionStore()
    a1 = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    a2 = store.propose("s1", "create_return", {"order_id": "AT-10086"})
    assert a1["action_id"] != a2["action_id"]
    assert len(a1["action_id"]) >= 16
    assert a1["session_id"] == "s1"
    assert a1["type"] == "create_return"
    assert a1["state"] == PROPOSED
    assert a1["payload_hash"] is not None


def test_superseded_action_returns_409():
    store = SessionStore()
    old = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    new = store.propose("s1", "create_return", {"order_id": "AT-10086"})

    # propose() returns a copy; re-fetch the stored record to see state update.
    old_fresh = store.get_action("s1", old["action_id"])
    assert old_fresh["state"] == SUPERSEDED
    assert new["state"] == PROPOSED

    action, reason = store.begin_confirm("s1", old["action_id"])
    assert action is None
    assert reason == "already_superseded"


def test_expired_action_returns_409():
    store = SessionStore(ttl_seconds=0)
    a = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    time.sleep(0.01)
    action, reason = store.begin_confirm("s1", a["action_id"])
    assert action is None
    assert reason == "expired"
    assert store.get_action("s1", a["action_id"])["state"] == EXPIRED


def test_cross_session_action_returns_409():
    store = SessionStore()
    a = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    action, reason = store.begin_confirm("s2", a["action_id"])
    assert action is None
    assert reason == "not_found"


def test_double_confirm_is_idempotent():
    store = SessionStore()
    a = store.propose("s1", "create_return", {"order_id": "AT-10092"})

    first, _ = store.begin_confirm("s1", a["action_id"])
    assert first["state"] == CONFIRMING

    second, reason = store.begin_confirm("s1", a["action_id"])
    assert second is None
    assert reason == "already_confirming"

    store.finish("s1", a["action_id"], SUCCEEDED, result={"return_ticket": "RT-123"})
    third, reason = store.begin_confirm("s1", a["action_id"])
    assert third is None
    assert reason == "already_succeeded"


def test_finish_is_idempotent():
    store = SessionStore()
    a = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    store.begin_confirm("s1", a["action_id"])
    r1 = store.finish("s1", a["action_id"], SUCCEEDED, result={"return_ticket": "RT-123"})
    r2 = store.finish("s1", a["action_id"], SUCCEEDED, result={"return_ticket": "RT-999"})
    assert r1["result"]["return_ticket"] == "RT-123"
    assert r2["result"]["return_ticket"] == "RT-123"


def test_unknown_state_is_not_confirmable():
    store = SessionStore()
    a = store.propose("s1", "create_return", {"order_id": "AT-10099"})
    store.begin_confirm("s1", a["action_id"])
    store.finish("s1", a["action_id"], UNKNOWN, error={"code": "BACKEND_TIMEOUT"})

    action, reason = store.begin_confirm("s1", a["action_id"])
    assert action is None
    assert "already_" in reason


def test_cancel_then_confirm_is_rejected():
    store = SessionStore()
    a = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    cancelled, _ = store.cancel("s1", a["action_id"])
    assert cancelled["state"] == CANCELLED

    action, reason = store.begin_confirm("s1", a["action_id"])
    assert action is None
    assert reason == "already_cancelled"


def test_action_record_is_never_deleted():
    store = SessionStore()
    a = store.propose("s1", "create_return", {"order_id": "AT-10092"})
    store.begin_confirm("s1", a["action_id"])
    store.finish("s1", a["action_id"], SUCCEEDED, result={"return_ticket": "RT-123"})
    assert store.get_action("s1", a["action_id"]) is not None
    assert len(store.snapshot("s1")["actions"]) == 1
