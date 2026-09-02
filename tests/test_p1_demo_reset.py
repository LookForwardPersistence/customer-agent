"""P1 demo reset：新会话签发时重置 mock 订单数据（刷新浏览器 = 干净的演示状态）。

直接在 MockOrderAPI 上构造"已退货"状态，避免走 LLM 的非确定性；
与其他测试一致，统一操作 `app.main` 命名空间里的 order_api 实例。
"""

from app import main as main_module
from app.mock_backend import MockOrderAPI
from fastapi.testclient import TestClient


def setup_function():
    # 其他测试文件也会替换该实例且不恢复，这里显式重置保证隔离
    main_module.order_api = MockOrderAPI()


def test_reset_restores_returned_order():
    """已退货的订单在新会话签发后回到初始状态。"""
    # 构造：AT-10092 已完成退货（模拟上一轮演示的遗留状态）
    result = main_module.order_api.create_return(
        "AT-10092", "不想要了", idempotency_key="reset-test-1"
    )
    assert result["return_ticket"].startswith("RT-")
    assert main_module.order_api.get_order("AT-10092")["return_status"]  # 已有退货

    # 刷新浏览器 = 新会话签发 -> mock 后端重置
    c = TestClient(main_module.app)
    r = c.post("/api/session/new", json={"customer_id": "CUST-001"})
    assert r.status_code == 200

    order = main_module.order_api.get_order("AT-10092")
    assert not order.get("return_status"), "reset 应清除退货状态"


def test_reset_clears_idempotency_and_keeps_initial_dataset():
    c = TestClient(main_module.app)
    # 切换客户同样走 reset；连做两次确认幂等记录也被清除
    assert c.post("/api/session/new", json={"customer_id": "CUST-003"}).status_code == 200
    assert c.post("/api/session/new", json={"customer_id": "CUST-001"}).status_code == 200

    # 初始数据集原样恢复：AT-10092 干净、AT-10050 仍为退货处理中
    assert not main_module.order_api.get_order("AT-10092").get("return_status")
    assert main_module.order_api.get_order("AT-10050")["return_status"]
    # 幂等记录已清空：同 key 重复确认不会返回旧工单
    again = main_module.order_api.create_return("AT-10092", "不想要了", idempotency_key="reset-test-1")
    assert again["return_ticket"].startswith("RT-")
