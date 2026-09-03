"""P0-3 并发退货：两个会话/两个 action 不能为同一订单创建两张退货单。

action_id 幂等只保证“同一动作不重复”；订单级业务唯一性（一个订单同时
最多一笔活动退货）必须由写入侧原子检查保证。
"""

import threading

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.mock_backend import MockOrderAPI, OrderAPIError
from app.store import sessions
from app import auth


@pytest.fixture
def client():
    return TestClient(main_module.app)


@pytest.fixture(autouse=True)
def reset():
    sessions._sessions.clear()
    auth.tokens.clear()
    main_module.order_api = MockOrderAPI()
    yield
    sessions._sessions.clear()
    auth.tokens.clear()


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


# -- mock 层：并发 create_return --------------------------------------------

def test_backend_concurrent_create_return_single_ticket():
    """不同 idempotency key（= 不同 action）并发写同一订单：只允许一张单。"""
    api = MockOrderAPI()
    outcomes: list[str] = []
    barrier = threading.Barrier(8)

    def worker(i: int):
        barrier.wait()  # maximise contention
        try:
            r = api.create_return("AT-10092", "不想要了", idempotency_key=f"k{i}")
            outcomes.append(r["return_ticket"])
        except OrderAPIError as e:
            outcomes.append(e.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tickets = [o for o in outcomes if o.startswith("RT-")]
    rejected = [o for o in outcomes if o == "ORDER_ALREADY_RETURNED"]
    assert len(tickets) == 1, f"expected exactly 1 ticket, got {outcomes}"
    assert len(rejected) == 7, f"others must be ORDER_ALREADY_RETURNED, got {outcomes}"
    assert len(api._returns) == 1  # 内存中也只有一张退货记录


# -- API 层：两个会话并发 confirm 同一订单 -----------------------------------

def test_two_sessions_concurrent_confirm_single_return(client):
    s1 = client.post("/api/session/new", json={"customer_id": "CUST-001"}).json()
    s2 = client.post("/api/session/new", json={"customer_id": "CUST-001"}).json()
    api = main_module.order_api

    # 两个会话各自生成针对同一订单的合法方案（此时还没有退货）
    p1 = api.validate_return("AT-10092", "不想要了")
    a1 = sessions.propose(s1["session_id"], "create_return", p1)
    p2 = api.validate_return("AT-10092", "不想要了")
    a2 = sessions.propose(s2["session_id"], "create_return", p2)

    results: list[dict] = []
    barrier = threading.Barrier(2)

    def confirm(tok: str, aid: str):
        barrier.wait()
        r = client.post("/api/session/confirm", json={"action_id": aid}, headers=_hdr(tok))
        results.append(r.json()["action"])

    t1 = threading.Thread(target=confirm, args=(s1["token"], a1["action_id"]))
    t2 = threading.Thread(target=confirm, args=(s2["token"], a2["action_id"]))
    t1.start(); t2.start(); t1.join(); t2.join()

    states = sorted(a["state"] for a in results)
    assert states == ["FAILED", "SUCCEEDED"], f"one must win, one must be rejected: {states}"
    # 落败方拿到明确的业务错误
    failed = next(a for a in results if a["state"] == "FAILED")
    assert failed["error"]["code"] == "ORDER_ALREADY_RETURNED"
    # 全局只存在一张退货单
    assert len(api._returns) == 1
    order = api.get_order("AT-10092")
    assert order["return_status"].count("RT-") == 1
