"""P1-1 鉴权：AuthenticatedCustomer 依赖、订单归属、token 生命周期。"""

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.main import app
from app.mock_backend import MockOrderAPI, OrderAPIError
from app.store import sessions


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset():
    sessions.clear()
    auth.tokens.clear()
    from app import main as main_module
    main_module.order_api = MockOrderAPI()
    yield
    sessions.clear()
    auth.tokens.clear()


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


# -- backend ownership ------------------------------------------------------

def test_get_order_scoped_to_owner():
    api = MockOrderAPI()
    order = api.get_order("AT-10077", customer_id="CUST-002")
    assert order["order_id"] == "AT-10077"
    with pytest.raises(OrderAPIError) as exc:
        api.get_order("AT-10077", customer_id="CUST-001")
    # Must NOT leak existence: same code as a truly unknown order.
    assert exc.value.code == "ORDER_NOT_FOUND"


def test_validate_return_scoped_to_owner():
    api = MockOrderAPI()
    with pytest.raises(OrderAPIError) as exc:
        api.validate_return("AT-10092", "不想要了", customer_id="CUST-002")
    assert exc.value.code == "ORDER_NOT_FOUND"


def test_create_return_scoped_to_owner():
    api = MockOrderAPI()
    with pytest.raises(OrderAPIError) as exc:
        api.create_return("AT-10092", "不想要了", customer_id="CUST-002")
    assert exc.value.code == "ORDER_NOT_FOUND"
    # 订单未被写入
    assert api.get_order("AT-10092").get("return_status") is None


def test_get_return_scoped_to_owner():
    api = MockOrderAPI()
    api.create_return("AT-10092", "不想要了", idempotency_key="k")
    assert api.get_return("AT-10092", customer_id="CUST-001") is not None
    assert api.get_return("AT-10092", customer_id="CUST-002") is None


# -- contextvar plumbing -----------------------------------------------------

def test_tools_respect_bound_customer():
    from app.tools import get_order_status
    import json as _json

    ctx = auth.bind_customer("CUST-001")
    try:
        data = _json.loads(get_order_status.invoke({"order_id": "AT-10086"}))
        assert data["ok"] is True

        data = _json.loads(get_order_status.invoke({"order_id": "AT-10077"}))  # 李女士的订单
        assert data["ok"] is False
        assert data["error_code"] == "ORDER_NOT_FOUND"
    finally:
        auth.unbind_customer(ctx)


# -- token lifecycle ---------------------------------------------------------

def test_token_expiry():
    svc = auth.TokenService(ttl_seconds=0)
    tok = svc.issue("CUST-001", "s1")
    import time
    time.sleep(0.01)
    assert svc.resolve(tok) is None


def test_session_cap_evicts_oldest():
    svc = auth.TokenService(max_sessions_per_customer=2)
    t1 = svc.issue("CUST-001", "s1")
    svc.issue("CUST-001", "s2")
    t3 = svc.issue("CUST-001", "s3")  # evicts t1
    assert svc.resolve(t1) is None
    assert svc.resolve(t3) is not None


def test_session_isolation_between_customers(client):
    a = client.post("/api/session/new", json={"customer_id": "CUST-001"}).json()
    b = client.post("/api/session/new", json={"customer_id": "CUST-002"}).json()

    # 各自 token 绑定各自的 session
    assert a["session_id"] != b["session_id"]
    sa = client.get("/api/session/state", headers=_hdr(a["token"])).json()
    sb = client.get("/api/session/state", headers=_hdr(b["token"])).json()
    assert sa["customer_id"] == "CUST-001"
    assert sb["customer_id"] == "CUST-002"


def test_other_customers_order_invisible_via_confirm(client):
    """CUST-002（李女士）会话中无法为 CUST-001 的订单创建方案并确认。"""
    from app import main as main_module

    s = client.post("/api/session/new", json={"customer_id": "CUST-002"}).json()
    # 直接注入 CUST-001 的订单方案（模拟被篡改的 payload）
    payload = main_module.order_api.validate_return("AT-10092", "不想要了")  # 无 scoping 时可生成
    action = sessions.propose(s["session_id"], "create_return", payload)

    r = client.post("/api/session/confirm", json={"action_id": action["action_id"]}, headers=_hdr(s["token"]))
    # 执行被订单归属校验拒绝
    assert r.status_code == 200
    assert r.json()["action"]["state"] == "FAILED"
    assert r.json()["action"]["error"]["code"] == "ORDER_NOT_FOUND"
    assert main_module.order_api.get_order("AT-10092").get("return_status") is None
