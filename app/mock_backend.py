"""Mock backend layer for orders and returns.

This module defines the ONLY interface the agent's tools talk to.
To swap in a real integration (e.g. a real OMS REST API), implement
the same `OrderAPI` protocol — no agent/tool code needs to change.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

DATA_DIR = Path(__file__).parent / "data"


class OrderAPIError(Exception):
    """Raised for all backend failures. `code` maps to a stable contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class OrderAPI(Protocol):
    def get_order(self, order_id: str) -> dict[str, Any]: ...

    def create_return(self, order_id: str, reason: str) -> dict[str, Any]: ...


class MockOrderAPI:
    """In-memory mock backed by data/orders.json.

    Simulates realistic failures so failure handling can be exercised:
      - ORDER_NOT_FOUND    -> unknown order id
      - ORDER_ALREADY_RETURNED -> return already in progress
      - RETURN_NOT_ALLOWED -> e.g. custom engraved items (policy)
      - BACKEND_TIMEOUT    -> transient failure injected for order AT-10099
    """

    def __init__(self, data_path: Path | None = None):
        self._lock = threading.Lock()
        path = data_path or DATA_DIR / "orders.json"
        with open(path, encoding="utf-8") as f:
            self._db: dict[str, dict] = {
                o["order_id"]: o for o in json.load(f)["orders"]
            }
        self._returns: dict[str, dict] = {}

    # -- read ---------------------------------------------------------------

    def get_order(self, order_id: str) -> dict[str, Any]:
        order = self._db.get(order_id)
        if order is None:
            raise OrderAPIError("ORDER_NOT_FOUND", f"订单号 {order_id} 不存在，请核对后重试。")
        return order

    # -- write (state-changing) ----------------------------------------------

    def validate_return(self, order_id: str, reason: str) -> dict[str, Any]:
        """Pre-checks a return request WITHOUT writing anything.

        Returns the proposal payload the user must confirm.
        """
        order = self.get_order(order_id)  # may raise ORDER_NOT_FOUND

        if order.get("return_status"):
            raise OrderAPIError(
                "ORDER_ALREADY_RETURNED",
                f"订单 {order_id} 已有退货流程在处理中（{order['return_status']}），请勿重复申请。",
            )

        # Custom engraved items are non-returnable unless quality issue.
        custom = any("定制" in i["name"] for i in order["items"])
        if custom and "质量" not in reason and "坏了" not in reason and "故障" not in reason:
            raise OrderAPIError(
                "RETURN_NOT_ALLOWED",
                "定制类商品（刻字手机壳）不支持无理由退货，仅质量问题可退换。"
                "如确有质量问题，请描述具体故障，我们可以为您走质量退换流程。",
            )

        refund = sum(i["price"] * i["qty"] for i in order["items"])
        return {
            "order_id": order_id,
            "reason": reason,
            "items": [i["name"] for i in order["items"]],
            "refund_amount": refund,
            "policy": "审核通过后 3-5 个工作日内原路退回",
        }

    def create_return(self, order_id: str, reason: str) -> dict[str, Any]:
        """Executes the return request. Called ONLY after user confirmation."""
        proposal = self.validate_return(order_id, reason)

        if order_id == "AT-10099":
            # Injected transient failure to demo failure handling.
            raise OrderAPIError("BACKEND_TIMEOUT", "退货系统暂时不可用，请稍后重试或转人工处理。")

        with self._lock:
            ticket = "RT-" + uuid.uuid4().hex[:8].upper()
            self._returns[ticket] = {**proposal, "created_at": time.strftime("%Y-%m-%d %H:%M")}
            order = self._db[order_id]
            order["return_status"] = f"退货已受理-{ticket}"
        return {
            "return_ticket": ticket,
            "order_id": order_id,
            "refund_amount": proposal["refund_amount"],
            "status": "退货已受理，等待寄回",
            "note": "请将商品寄回至包装上的退货地址，质检通过后 3-5 个工作日退款。",
        }


# Single shared instance (module-level, same contract as a real client).
order_api: OrderAPI = MockOrderAPI()
