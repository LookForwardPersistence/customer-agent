"""Agent tools (stateless by design).

Contract:
- Tools are pure functions over the mock backend / KB — no session state,
  fully unit-testable.
- The ONLY write path to the backend (`create_return`) is NOT exposed to
  the LLM. It runs server-side after the user clicks the confirmation
  button. This prevents prompt-injection from triggering state changes.
- `propose_return` validates and returns a proposal; the API layer turns
  it into a pending action requiring explicit UI confirmation.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from .knowledge_base import kb
from .mock_backend import OrderAPIError, order_api


def _fmt_order(order: dict[str, Any]) -> dict[str, Any]:
    items = "、".join(f"{i['name']} x{i['qty']}" for i in order["items"])
    tracking = f"{order['carrier']} {order['tracking_no']}" if order.get("carrier") else "暂无"
    return {
        "order_id": order["order_id"],
        "status": order["status"],
        "items": items,
        "total": order["total"],
        "placed_at": order["placed_at"],
        "tracking": tracking,
        "eta": order.get("eta"),
        "return_status": order.get("return_status"),
    }


@tool
def search_knowledge_base(query: str) -> str:
    """在店铺知识库中检索政策、流程、产品相关信息（退货政策、运费、配送、保修、支付、发票、会员、价保等）。

    Args:
        query: 用户的政策类问题或关键词。
    """
    results = kb.search(query)
    if not results:
        return json.dumps(
            {"found": False, "message": "知识库中没有找到相关内容，请勿编造答案。"},
            ensure_ascii=False,
        )
    confidence = "high" if results[0]["score"] >= 0.25 else "low"
    return json.dumps(
        {
            "found": True,
            "confidence": confidence,
            "hint": (
                None
                if confidence == "high"
                else "检索结果相关度较低，可能与用户问题不匹配。仅当某条结果确实能回答问题时才使用它；否则如实告知无法确认。"
            ),
            "results": [
                {"id": r["id"], "topic": r["topic"], "content": r["content"], "score": r["score"]}
                for r in results
            ],
        },
        ensure_ascii=False,
    )


@tool
def get_order_status(order_id: str) -> str:
    """查询订单的实时状态、物流单号、商品明细和金额。订单号格式为 AT- 开头。

    Args:
        order_id: 订单号，例如 AT-10086。
    """
    try:
        return json.dumps(
            {"ok": True, "order": _fmt_order(order_api.get_order(order_id))},
            ensure_ascii=False,
        )
    except OrderAPIError as e:
        return json.dumps(
            {"ok": False, "error_code": e.code, "message": e.message}, ensure_ascii=False
        )


@tool
def propose_return(order_id: str, reason: str) -> str:
    """为用户发起退货申请的预校验（不会真正提交）。返回待确认的退货方案，需等待用户明确确认。

    Args:
        order_id: 要退货的订单号。
        reason: 退货原因（如不想要了 / 质量问题 / 描述不符）。
    """
    try:
        proposal = order_api.validate_return(order_id, reason)
        return json.dumps(
            {
                "status": "NEEDS_CONFIRMATION",
                "proposal": proposal,
                "message": "退货方案已生成，等待用户通过界面确认后才会执行。",
            },
            ensure_ascii=False,
        )
    except OrderAPIError as e:
        return json.dumps(
            {"status": "ERROR", "error_code": e.code, "message": e.message}, ensure_ascii=False
        )


@tool
def handoff_to_human(reason: str, summary: str) -> str:
    """将对话转接给人工客服。在问题超出机器人能力、用户明确要求人工、或多次处理失败时调用。

    Args:
        reason: 转接原因（如：超出服务范围 / 用户要求 / 系统故障）。
        summary: 给人工客服的上下文摘要，应包含客户诉求、关键信息（订单号等）以及已尝试的处理。
    """
    return json.dumps(
        {
            "status": "HANDOFF_QUEUED",
            "reason": reason,
            "summary": summary,
            "message": "转接请求已记录，人工客服将在会话上下文中看到上述摘要。",
        },
        ensure_ascii=False,
    )


TOOLS = [search_knowledge_base, get_order_status, propose_return, handoff_to_human]
