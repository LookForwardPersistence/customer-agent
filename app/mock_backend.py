"""Mock backend layer for orders and returns.

This module defines the ONLY interface the agent's tools talk to.
To swap in a real integration (e.g. a real OMS REST API), implement
the same `OrderAPI` protocol — no agent/tool code needs to change.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

DATA_DIR = Path(__file__).parent / "data"

# Policy text is part of the proposal fingerprint: if the policy the user
# agreed to changes, the proposal must be re-confirmed.
RETURN_POLICY_TEXT = "审核通过后 3-5 个工作日内原路退回"


class OrderAPIError(Exception):
    """Raised for all backend failures. `code` maps to a stable contract.

    `transient=True` means the write *may* have landed (timeout / unknown
    outcome). Callers must NOT treat it as "definitely failed" and must not
    blindly retry the POST — they should mark the action UNKNOWN and confirm
    the outcome with a read.
    """

    def __init__(self, code: str, message: str, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.transient = transient


class OrderAPI(Protocol):
    def get_order(self, order_id: str, customer_id: str | None = None) -> dict[str, Any]: ...

    def create_return(
        self,
        order_id: str,
        reason: str,
        idempotency_key: str | None = None,
        expected_fingerprint: str | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any]: ...


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
        self._path = data_path or DATA_DIR / "orders.json"
        self._reload()

    def _reload(self) -> None:
        with open(self._path, encoding="utf-8") as f:
            self._db: dict[str, dict] = {
                o["order_id"]: o for o in json.load(f)["orders"]
            }
        self._returns: dict[str, dict] = {}
        # idempotency_key -> executed result. A retried confirm must return the
        # SAME ticket rather than creating a second return.
        self._idempotency: dict[str, dict] = {}

    def reset(self) -> None:
        """Reload orders from disk, discarding all in-memory mutations.

        Demo semantics: every new session starts from the pristine dataset
        (order statuses, return tickets, idempotency records), so a browser
        refresh gives the presenter a clean slate. A real OMS obviously never
        resets — this exists only because the mock has no persistence.
        """
        with self._lock:
            self._reload()

    @property
    def known_customers(self) -> dict[str, str]:
        """customer_id -> display name (for session issuance)."""
        return {o["customer_id"]: o["customer"] for o in self._db.values()}

    def _get_owned_order(self, order_id: str, customer_id: str | None) -> dict[str, Any]:
        """Fetch an order and enforce ownership.

        An order that exists but belongs to another customer is reported as
        ORDER_NOT_FOUND — the API must not leak which order ids exist.
        `customer_id=None` means "no ownership scoping" (direct tool-level
        tests); the HTTP layer always passes the authenticated id.
        """
        order = self._db.get(order_id)
        if order is None:
            raise OrderAPIError("ORDER_NOT_FOUND", f"订单号 {order_id} 不存在，请核对后重试。")
        if customer_id and order.get("customer_id") != customer_id:
            raise OrderAPIError("ORDER_NOT_FOUND", f"订单号 {order_id} 不存在，请核对后重试。")
        return order

    # -- read ---------------------------------------------------------------

    def get_order(self, order_id: str, customer_id: str | None = None) -> dict[str, Any]:
        return self._get_owned_order(order_id, customer_id)

    def get_return(self, order_id: str, customer_id: str | None = None) -> dict[str, Any] | None:
        """Read-only outcome check, used to resolve UNKNOWN actions.

        After a transient failure the write may or may not have landed. The
        correct way to find out is a read like this one — never a blind retry
        of the POST, which could create a duplicate return.
        """
        try:
            order = self._get_owned_order(order_id, customer_id)
        except OrderAPIError:
            return None
        if not order or not order.get("return_status"):
            return None
        # return_status format: "退货已受理-{ticket}" where ticket is "RT-XXXX".
        prefix = "退货已受理-"
        status = order["return_status"]
        ticket = status[len(prefix):] if status.startswith(prefix) else status.split("-", 1)[-1]
        record = self._returns.get(ticket)
        return {
            "return_ticket": ticket,
            "order_id": order_id,
            "refund_amount": record["refund_amount"] if record else None,
            "status": "退货已受理，等待寄回",
        }

    # -- write (state-changing) ----------------------------------------------

    def validate_return(self, order_id: str, reason: str, customer_id: str | None = None) -> dict[str, Any]:
        """Pre-checks a return request WITHOUT writing anything.

        Returns the proposal payload the user must confirm.
        """
        order = self._get_owned_order(order_id, customer_id)  # may raise ORDER_NOT_FOUND

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
        proposal = {
            "order_id": order_id,
            "reason": reason,
            "items": [i["name"] for i in order["items"]],
            "refund_amount": refund,
            "policy": RETURN_POLICY_TEXT,
        }
        # Fingerprint of exactly what the user is being asked to confirm.
        # If the order changes between proposal and confirmation (items,
        # prices, an existing return), the fingerprint no longer matches and
        # execution is refused instead of silently writing a different amount.
        proposal["fingerprint"] = self._fingerprint(order, proposal)
        return proposal

    @staticmethod
    def _fingerprint(order: dict[str, Any], proposal: dict[str, Any]) -> str:
        blob = json.dumps(
            {
                "order_id": order["order_id"],
                "items": [(i["name"], i["price"], i["qty"]) for i in order["items"]],
                "refund_amount": proposal["refund_amount"],
                "policy": proposal["policy"],
                "return_status": order.get("return_status"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def create_return(
        self,
        order_id: str,
        reason: str,
        idempotency_key: str | None = None,
        expected_fingerprint: str | None = None,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        """Executes the return request. Called ONLY after user confirmation.

        - `idempotency_key`: replaying the same confirmation returns the same
          ticket instead of creating a second return (request-replay key).
        - `expected_fingerprint`: the proposal the user actually saw. A mismatch
          means the order changed under them -> refuse (STALE_PROPOSAL).
        - `customer_id`: the authenticated owner; a mismatch refuses the write.
        - Business uniqueness: validation (no active return) and creation share
          ONE atomic critical section. Two different sessions/actions racing to
          return the same order cannot both succeed — the second gets
          ORDER_ALREADY_RETURNED. The idempotency key alone cannot guarantee
          this: it only de-duplicates the *same* action.
        """
        # The whole check-then-act sequence runs under the lock: idempotent
        # replay, ownership, no-active-return, fingerprint and creation.
        # (validate_return only reads state and never takes the lock itself,
        # so there is no re-entrancy problem.)
        with self._lock:
            # Idempotent replay first: never touch state on a retry.
            if idempotency_key and idempotency_key in self._idempotency:
                return self._idempotency[idempotency_key]

            proposal = self.validate_return(order_id, reason, customer_id)  # may raise

            if expected_fingerprint and expected_fingerprint != proposal["fingerprint"]:
                raise OrderAPIError(
                    "STALE_PROPOSAL",
                    "订单信息在您确认前已发生变化，该退货方案已失效，请重新申请后再确认。",
                )

            if order_id == "AT-10099":
                # Injected transient failure: outcome is UNKNOWN, not "failed".
                # Callers must not retry blindly; they should re-read the order.
                raise OrderAPIError(
                    "BACKEND_TIMEOUT",
                    "退货系统暂时不可用，请稍后重试或转人工处理。",
                    transient=True,
                )

            ticket = "RT-" + uuid.uuid4().hex[:8].upper()
            self._returns[ticket] = {**proposal, "created_at": time.strftime("%Y-%m-%d %H:%M")}
            order = self._db[order_id]
            order["return_status"] = f"退货已受理-{ticket}"
            result = {
                "return_ticket": ticket,
                "order_id": order_id,
                "refund_amount": proposal["refund_amount"],
                "status": "退货已受理，等待寄回",
                "note": "请将商品寄回至包装上的退货地址，质检通过后 3-5 个工作日退款。",
            }
            if idempotency_key:
                self._idempotency[idempotency_key] = result
        return result


# Single shared instance (module-level, same contract as a real client).
order_api: OrderAPI = MockOrderAPI()
