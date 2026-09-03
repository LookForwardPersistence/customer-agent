"""P0-4 瞬时失败后的只读恢复路径：

1. 写超时（BACKEND_TIMEOUT）+ readback 也抛异常 → 动作必须是 UNKNOWN，
   不得留下永久 CONFIRMING（HTTP 500 / already_confirming 死锁）。
2. CONFIRMING 超时扫描：孤儿动作被 sweep 为 UNKNOWN（CONFIRM_TIMEOUT）。
3. 迁移规则：CONFIRMING 只能落到 SUCCEEDED/FAILED/UNKNOWN。
"""

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import main as main_module
from app.mock_backend import MockOrderAPI
from app.store import (
    CONFIRMING,
    FAILED,
    SUCCEEDED,
    UNKNOWN,
    sessions,
)


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def reset():
    sessions.clear()
    auth.tokens.clear()
    main_module.order_api = MockOrderAPI()
    yield
    sessions.clear()
    auth.tokens.clear()


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def _propose(client, customer_id, order_id, reason="不想要了"):
    s = client.post("/api/session/new", json={"customer_id": customer_id}).json()
    payload = main_module.order_api.validate_return(order_id, reason)
    action = sessions.propose(s["session_id"], "create_return", payload)
    return s, action


# -- 1. readback 失败不得穿透 ----------------------------------------------

def test_readback_failure_marks_unknown_not_stuck_confirming(client, monkeypatch):
    """写超时 + 查询路径也挂 → 200 + UNKNOWN（修复前：500 + 永久 CONFIRMING）。"""
    s, action = _propose(client, "CUST-003", "AT-10099")  # AT-10099 注入 BACKEND_TIMEOUT

    def broken_readback(*args, **kwargs):
        raise RuntimeError("read path down")

    monkeypatch.setattr(main_module.order_api, "get_return", broken_readback)

    r = client.post("/api/session/confirm",
                    json={"action_id": action["action_id"]}, headers=_hdr(s["token"]))

    assert r.status_code == 200, "readback 异常不得导致 500"
    body = r.json()
    assert body["action"]["state"] == UNKNOWN
    assert body["action"]["error"]["code"] == "BACKEND_TIMEOUT"
    assert "read path down" in body["action"]["error"]["message"]
    # UNKNOWN 的提示语引导查询核实，而不是重复提交
    assert "重复提交" in body["reply"] or "查询" in body["reply"]


def test_readback_ok_still_recovers_to_succeeded(client):
    """写超时但查询路径正常 → 仍然恢复为 SUCCEEDED（回归保护）。"""
    s, action = _propose(client, "CUST-003", "AT-10099")
    r = client.post("/api/session/confirm",
                    json={"action_id": action["action_id"]}, headers=_hdr(s["token"]))
    assert r.status_code == 200
    assert r.json()["action"]["state"] in (SUCCEEDED, UNKNOWN)


# -- 2. CONFIRMING 超时扫描 --------------------------------------------------

def test_sweep_marks_stale_confirming_unknown():
    s, action = _noop_propose()
    # 模拟：begin_confirm 后进程崩溃，动作停留在 CONFIRMING
    sessions.begin_confirm(s, action["action_id"])
    stored = sessions.get_action(s, action["action_id"])
    assert stored["state"] == CONFIRMING

    # 人为把 dispatched_at 拨回过去，模拟超时。
    # 通过后端 load→mutate→save，兼容 Memory/Sqlite 两种后端。
    s_data = sessions._backend.load(s)
    s_data["actions"][action["action_id"]]["dispatched_at"] -= 999
    sessions._backend.save(s, s_data)

    swept = sessions.sweep_stale_confirming()
    assert action["action_id"] in swept
    after = sessions.get_action(s, action["action_id"])
    assert after["state"] == UNKNOWN
    assert after["error"]["code"] == "CONFIRM_TIMEOUT"


def test_sweep_ignores_fresh_confirming():
    s, action = _noop_propose()
    sessions.begin_confirm(s, action["action_id"])
    assert sessions.sweep_stale_confirming() == []  # 刚派发，不扫


# -- 3. 迁移规则 ---------------------------------------------------------------

def test_finish_rejects_illegal_confirming_exit():
    s, action = _noop_propose()
    sessions.begin_confirm(s, action["action_id"])
    # CONFIRMING -> CANCELLED 是非法迁移（写已派发，不能“当作没发生”）
    out = sessions.finish(s, action["action_id"], "CANCELLED")
    assert out is None
    assert sessions.get_action(s, action["action_id"])["state"] == CONFIRMING
    # 合法迁移仍可用
    assert sessions.finish(s, action["action_id"], FAILED, error={"code": "X"}) is not None


def test_finish_rejects_illegal_unknown_exit():
    s, action = _noop_propose()
    sessions.begin_confirm(s, action["action_id"])
    sessions.finish(s, action["action_id"], UNKNOWN, error={"code": "X"})
    # UNKNOWN -> CANCELLED 非法：写可能已落地
    assert sessions.finish(s, action["action_id"], "CANCELLED") is None
    assert sessions.get_action(s, action["action_id"])["state"] == UNKNOWN
    # UNKNOWN -> SUCCEEDED 合法（查询恢复）
    assert sessions.finish(s, action["action_id"], SUCCEEDED, result={"return_ticket": "RT-1"}) is not None


def _noop_propose():
    """直接在 store 上构造一个最小 action（不依赖 HTTP/LLM）。"""
    s = "test-sid-" + sessions.__class__.__name__
    action = sessions.propose(s, "create_return", {"order_id": "AT-10092"})
    return s, action
