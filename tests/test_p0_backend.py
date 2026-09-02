"""P0-2 核心：MockOrderAPI 幂等、指纹与 UNKNOWN 恢复路径。"""

import pytest

from app.mock_backend import MockOrderAPI, OrderAPIError


def test_idempotency_key_returns_same_ticket():
    api = MockOrderAPI()
    key = "action-123"
    r1 = api.create_return("AT-10092", "不想要了", idempotency_key=key)
    r2 = api.create_return("AT-10092", "不想要了", idempotency_key=key)
    assert r1["return_ticket"] == r2["return_ticket"]


def test_stale_fingerprint_is_refused():
    api = MockOrderAPI()
    proposal = api.validate_return("AT-10092", "不想要了")
    fingerprint = proposal["fingerprint"]

    # 模拟后端状态变化：商品价格改变，导致指纹变化（但不触发已退货）
    api._db["AT-10092"]["items"][0]["price"] = 999.0

    with pytest.raises(OrderAPIError) as exc:
        api.create_return("AT-10092", "不想要了", idempotency_key="new", expected_fingerprint=fingerprint)
    assert exc.value.code == "STALE_PROPOSAL"


def test_transient_timeout_can_be_recovered_by_read():
    api = MockOrderAPI()
    with pytest.raises(OrderAPIError) as exc:
        api.create_return("AT-10099", "不想要了", idempotency_key="k")
    assert exc.value.code == "BACKEND_TIMEOUT"
    assert exc.value.transient is True

    # 模拟超时后实际已受理（测试恢复查询能力）
    api._db["AT-10099"]["return_status"] = "退货已受理-RT-FAKE"
    api._returns["RT-FAKE"] = {"refund_amount": 199.0}
    recovered = api.get_return("AT-10099")
    assert recovered is not None
    assert recovered["return_ticket"] == "RT-FAKE"


def test_create_return_does_not_blindly_retry_after_timeout():
    api = MockOrderAPI()
    with pytest.raises(OrderAPIError) as exc:
        api.create_return("AT-10099", "不想要了", idempotency_key="k")
    assert exc.value.code == "BACKEND_TIMEOUT"
    # 未做盲重试：订单应保持未退货状态
    assert api._db["AT-10099"].get("return_status") is None
