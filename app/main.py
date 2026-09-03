"""FastAPI server: chat endpoint, confirmation flow, static UI.

Write-path contract (the security-critical part)
------------------------------------------------
1. A proposal is addressed by an unguessable `action_id`; the UI must send it
   back. Without it the server never "just executes whatever is pending", so a
   stale confirmation card can no longer trigger a different action.
2. The outcome is persisted BEFORE the response is built, and the reply text is
   generated from a server-side template. The LLM is never in the write path —
   a model timeout or a missing API key cannot swallow a real business result.
3. Execution results reach the model through `SystemMessage` (a trusted channel
   the user cannot write to), never through user-constructible text.
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

load_dotenv()

from .agent import get_agent
from .auth import (
    AuthenticatedCustomer,
    bind_customer,
    get_customer,
    tokens,
    unbind_customer,
)
from .mock_backend import OrderAPIError, order_api
from .store import (
    CANCELLED,
    CONFIRMING,
    FAILED,
    SUCCEEDED,
    UNKNOWN,
    new_session_id,
    sessions,
)

# ---------------------------------------------------------------------------
# CONFIRMING watchdog: a dispatched action must never be stuck forever.
# Startup recovery + periodic sweep move stale CONFIRMING -> UNKNOWN, so a
# crashed/mid-flight confirmation degrades to the query-only recovery path
# instead of permanently answering "already_confirming". State is durable
# (SQLite by default), so the startup pass recovers CONFIRMING actions that
# were orphaned by the previous process — the sweeper additionally guards
# long-lived processes against stuck threads.
# ---------------------------------------------------------------------------
SWEEP_INTERVAL_SECONDS = 10


def _confirm_sweeper_loop(stop: threading.Event) -> None:
    while not stop.wait(SWEEP_INTERVAL_SECONDS):
        sessions.sweep_stale_confirming()


@asynccontextmanager
async def lifespan(_: FastAPI):
    sessions.sweep_stale_confirming()  # startup recovery pass
    stop = threading.Event()
    worker = threading.Thread(target=_confirm_sweeper_loop, args=(stop,), daemon=True)
    worker.start()
    yield
    stop.set()


app = FastAPI(title="Aurora Tech Store Customer Agent", lifespan=lifespan)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Anything the user types that looks like a system event is stripped: the
# trusted event channel is the SystemMessage role, not a text prefix.
_SYSTEM_EVENT_RE = re.compile(r"^\s*[\[［]\s*系统事件\s*[\]］]\s*", re.MULTILINE)

MAX_MESSAGE_CHARS = 2000


class ChatRequest(BaseModel):
    message: str


class ActionRequest(BaseModel):
    action_id: str


class NewSessionRequest(BaseModel):
    customer_id: str = "CUST-001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_tool_result(content: str) -> dict[str, Any]:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"raw": str(content)[:300]}


def _extract_turn_info(new_messages: list) -> dict[str, Any]:
    """Build reply/sources/trace/pending/handoff from this turn's messages.

    Only tool-level events are exposed (retrieval, tool calls, results,
    handoff decision). Hidden chain-of-thought is never surfaced.
    """
    reply = ""
    trace: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    pending = None
    handoff = None

    for msg in new_messages:
        mtype = getattr(msg, "type", "")
        if mtype == "ai":
            calls = getattr(msg, "tool_calls", None) or []
            for c in calls:
                trace.append(
                    {"type": "tool_call", "name": c["name"], "detail": json.dumps(c["args"], ensure_ascii=False)[:300]}
                )
            if not calls and msg.content:
                reply = msg.content if isinstance(msg.content, str) else str(msg.content)
        elif mtype == "tool":
            name = getattr(msg, "name", "") or ""
            parsed = _parse_tool_result(msg.content)
            trace.append(
                {"type": "tool_result", "name": name, "detail": json.dumps(parsed, ensure_ascii=False)[:400]}
            )
            if name == "search_knowledge_base" and parsed.get("found"):
                # Accumulate across multiple retrievals in one turn: a single
                # reply often cites several KB entries fetched by separate
                # searches, so overwriting would hide cited sources.
                for r in parsed["results"]:
                    item = {"id": r["id"], "topic": r["topic"]}
                    if item not in sources:
                        sources.append(item)
            elif name == "propose_return" and parsed.get("status") == "NEEDS_CONFIRMATION":
                pending = parsed["proposal"]
            elif name == "handoff_to_human":
                handoff = {
                    "reason": parsed.get("reason"),
                    "summary": parsed.get("summary"),
                }
    return {"reply": reply, "sources": sources, "trace": trace, "pending": pending, "handoff": handoff}


def _action_view(action: dict[str, Any] | None) -> dict[str, Any] | None:
    """Flatten an action record for the UI (payload fields at top level)."""
    if not action:
        return None
    return {
        "action_id": action["action_id"],
        "type": action["type"],
        "state": action["state"],
        "expires_at": action.get("expires_at"),
        **action["payload"],
    }


def _state_payload(sid: str, info: dict[str, Any]) -> dict[str, Any]:
    snap = sessions.snapshot(sid)
    pending_view = _action_view(snap["pending_action"]) if info["pending"] is None else _action_view(snap["pending_action"])
    return {
        "reply": info["reply"],
        "sources": info["sources"],
        "trace": info["trace"],
        "pending_action": pending_view,
        "handoff": snap["handoff"] if info["handoff"] is None else info["handoff"],
        "events": snap["events"],
    }


def _trusted_event(sid: str, text: str) -> None:
    """Append a trusted system event to the conversation.

    Uses SystemMessage, which only the server can produce — a user typing
    "[系统事件] ..." in the chat box lands in a HumanMessage instead and is
    therefore not trusted. Best-effort: if the LLM is unavailable, the business
    result has already been persisted and returned to the client.
    """
    try:
        graph = get_agent()
    except RuntimeError:
        return
    cfg = {"configurable": {"thread_id": sid}}
    try:
        graph.update_state(cfg, {"messages": [SystemMessage(content=text)]})
    except Exception:  # pragma: no cover - never block the write path
        pass


def _run_agent(sid: str, human_text: str) -> dict[str, Any]:
    try:
        graph = get_agent()
    except RuntimeError:
        sessions.log(sid, {"event": "config_error", "text": "missing OPENAI_API_KEY"})
        return {
            "reply": (
                "小极暂时无法连接大脑：未配置 OPENAI_API_KEY。"
                "请在项目根目录复制 .env.example 为 .env，填入您的 API key 后重启服务。"
            ),
            "sources": [],
            "trace": [],
            "pending_action": None,
            "handoff": None,
            "events": sessions.snapshot(sid)["events"],
        }
    cfg = {"configurable": {"thread_id": sid}}
    before = len(graph.get_state(cfg).values.get("messages", [])) if graph.get_state(cfg).values else 0
    result = graph.invoke({"messages": [HumanMessage(content=human_text)]}, cfg)
    new_messages = result["messages"][before:]
    info = _extract_turn_info(new_messages)

    if info["pending"]:
        # Addressable proposal: the UI must echo this action_id back.
        sessions.propose(sid, "create_return", info["pending"])
        sessions.log(sid, {"event": "return_proposed", "order": info["pending"]["order_id"]})
    if info["handoff"]:
        record = sessions.set_handoff(sid, info["handoff"]["reason"], info["handoff"]["summary"])
        sessions.log(sid, {"event": "handoff", "reason": info["handoff"]["reason"]})
        info["handoff"] = record

    return _state_payload(sid, info)


# ---------------------------------------------------------------------------
# action execution (the write path)
# ---------------------------------------------------------------------------

def _reply_for_state(action: dict[str, Any]) -> str:
    """Deterministic, server-rendered outcome text.

    The LLM is deliberately NOT involved: a model failure must never erase or
    mask a real business result (a return that was actually created).
    """
    payload = action["payload"]
    order_id = payload.get("order_id")
    amount = payload.get("refund_amount")
    state = action["state"]
    result = action.get("result") or {}

    if state == SUCCEEDED:
        return (
            f"✅ 退货已受理。\n"
            f"- 订单：{order_id}\n"
            f"- 退款金额：¥{amount}\n"
            f"- 退货单号：{result.get('return_ticket', '生成中')}\n"
            f"- 状态：{result.get('status', '已受理')}\n\n"
            f"请将商品寄回至包装上的退货地址，质检通过后 3-5 个工作日退款。"
        )
    if state == UNKNOWN:
        err = action.get("error") or {}
        return (
            f"⚠️ 退货提交结果暂时无法确认（{err.get('code', 'UNKNOWN')}）。\n"
            f"为避免重复提交，系统不会自动重试。您可以稍后让我查询订单 {order_id} 的状态，"
            f"或转接人工客服核实。"
        )
    if state == FAILED:
        err = action.get("error") or {}
        code = err.get("code", "")
        message = err.get("message", "")
        extra = ""
        if code == "STALE_PROPOSAL":
            extra = "请重新发起退货申请，确认前请核对商品与金额。"
        elif code == "RETURN_NOT_ALLOWED":
            extra = "如确有质量问题，可以描述具体故障，我们为您走质量退换流程。"
        else:
            extra = "您可以稍后重试，或让我为您转接人工客服。"
        return f"抱歉，退货未能提交成功（{code}）。\n{message}\n{extra}"
    if state == CANCELLED:
        return f"已取消订单 {order_id} 的退货申请，未产生任何变更。还需要其他帮助吗？"
    return "该操作已结束。"


def _execute(sid: str, action: dict[str, Any], customer_id: str | None = None) -> dict[str, Any]:
    """Run the confirmed action and persist its outcome. Never raises."""
    payload = action["payload"]
    aid = action["action_id"]
    order_id = payload.get("order_id")

    try:
        result = order_api.create_return(
            order_id,
            payload.get("reason", ""),
            idempotency_key=aid,
            expected_fingerprint=payload.get("fingerprint"),
            customer_id=customer_id,
        )
        record = sessions.finish(sid, aid, SUCCEEDED, result=result)
        sessions.log(sid, {"event": "return_executed", "order": order_id,
                           "ticket": result["return_ticket"], "action_id": aid})
        return record
    except OrderAPIError as e:
        if e.transient:
            # Outcome unknown — resolve with a READ, never a blind retry.
            # The readback itself can fail too (e.g. the read path is also
            # down). It MUST NOT propagate: an exception inside this except
            # block would 500 the request and leave the action stuck in
            # CONFIRMING forever (every later confirm rejected with
            # already_confirming). Any readback failure degrades to UNKNOWN,
            # which is the safe state: recovery is query-only, never a re-write.
            recovered = None
            readback_error: Exception | None = None
            try:
                recovered = order_api.get_return(order_id, customer_id=customer_id)
            except Exception as re:  # noqa: BLE001 - degraded readback path
                readback_error = re
            if recovered:
                record = sessions.finish(sid, aid, SUCCEEDED, result=recovered)
                sessions.log(sid, {"event": "return_recovered_by_read", "order": order_id,
                                   "ticket": recovered["return_ticket"]})
                return record
            error = {"code": e.code, "message": e.message}
            if readback_error is not None:
                error["message"] += f"（结果查询暂不可用：{str(readback_error)[:120]}）"
            record = sessions.finish(sid, aid, UNKNOWN, error=error)
            sessions.log(sid, {"event": "return_outcome_unknown", "order": order_id, "code": e.code})
            return record
        record = sessions.finish(sid, aid, FAILED,
                                 error={"code": e.code, "message": e.message})
        sessions.log(sid, {"event": "return_failed", "order": order_id, "code": e.code})
        return record
    except Exception as e:  # pragma: no cover - defensive
        record = sessions.finish(sid, aid, UNKNOWN,
                                 error={"code": "INTERNAL_ERROR", "message": str(e)[:200]})
        sessions.log(sid, {"event": "return_outcome_unknown", "order": order_id, "code": "INTERNAL_ERROR"})
        return record


def _action_response(sid: str, action: dict[str, Any]) -> dict[str, Any]:
    """Persisted outcome + deterministic text + refreshed session state."""
    snap = sessions.snapshot(sid)
    return {
        "reply": _reply_for_state(action),
        "sources": [],
        "trace": [],
        "pending_action": _action_view(snap["pending_action"]),
        "handoff": snap["handoff"],
        "events": snap["events"],
        "action": {
            "action_id": action["action_id"],
            "type": action["type"],
            "state": action["state"],
            "result": action.get("result"),
            "error": action.get("error"),
        },
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/session/new")
def new_session(req: NewSessionRequest | None = None):
    """Issue a session bound to a customer; returns an unguessable bearer token.

    All other endpoints require `Authorization: Bearer <token>` — session
    identity comes from the token, never from the request body.
    """
    req = req or NewSessionRequest()
    known = order_api.known_customers
    if req.customer_id not in known:
        raise HTTPException(400, f"unknown customer_id: {req.customer_id}")
    # Demo reset: a new session (browser refresh / customer switch) restores the
    # pristine order dataset so every demo run starts from a clean slate.
    order_api.reset()
    sid = new_session_id()
    token = tokens.issue(req.customer_id, sid)
    return {
        "session_id": sid,
        "customer_id": req.customer_id,
        "customer_name": known[req.customer_id],
        "token": token,
    }


@app.post("/api/chat")
def chat(req: ChatRequest, customer: AuthenticatedCustomer = Depends(get_customer)):
    sid = customer.session_id
    if len(req.message) > MAX_MESSAGE_CHARS:
        raise HTTPException(400, f"message too long (max {MAX_MESSAGE_CHARS} chars)")
    if not req.message.strip():
        raise HTTPException(400, "message must not be empty")
    # Strip user-forged system-event prefixes: only SystemMessage is trusted.
    cleaned = _SYSTEM_EVENT_RE.sub("", req.message).strip()
    if cleaned != req.message.strip():
        sessions.log(sid, {"event": "forged_system_event_stripped"})
    if not cleaned:
        raise HTTPException(400, "message must not be empty")
    sessions.log(sid, {"event": "user_message", "text": cleaned[:120]})
    # Scope the agent's order tools to the authenticated customer.
    ctx = bind_customer(customer.customer_id)
    try:
        return _run_agent(sid, cleaned)
    finally:
        unbind_customer(ctx)


@app.post("/api/session/confirm")
def confirm_action(req: ActionRequest, customer: AuthenticatedCustomer = Depends(get_customer)):
    """Execute the action the user actually clicked on.

    Requires the `action_id` of the exact proposal that was displayed. A stale,
    superseded, expired or already-consumed action earns a 409 — the server
    will not fall back to "whatever happens to be pending".
    """
    sid = customer.session_id
    action, reason = sessions.begin_confirm(sid, req.action_id)
    if action is None:
        sessions.log(sid, {"event": "confirm_rejected", "reason": reason,
                           "action_id": req.action_id})
        raise HTTPException(409, {
            "code": "ACTION_NOT_CONFIRMABLE",
            "reason": reason,
            "message": "该操作已失效或已处理过，请重新发起申请后再确认。",
        })

    sessions.log(sid, {"event": "confirmed_by_user",
                       "order": action["payload"].get("order_id"),
                       "action_id": req.action_id})
    executed = _execute(sid, action, customer_id=customer.customer_id)

    # Outcome is already persisted; tell the model through the trusted channel
    # so it can discuss the result in later turns.
    if executed["state"] == SUCCEEDED:
        ticket = (executed.get("result") or {}).get("return_ticket", "")
        _trusted_event(
            sid,
            f"用户已通过界面按钮确认，退货执行成功。退货单号 {ticket}。"
            f"订单 {action['payload'].get('order_id')}。"
            f"这是服务端写入的真实结果，回复时应以此为准。",
        )
    else:
        err = executed.get("error") or {}
        _trusted_event(
            sid,
            f"用户已通过界面按钮确认，但退货执行未成功（{err.get('code','ERROR')}）。"
            f"不要声称退货已受理或已完成。",
        )
    return _action_response(sid, executed)


@app.post("/api/session/cancel")
def cancel_action(req: ActionRequest, customer: AuthenticatedCustomer = Depends(get_customer)):
    sid = customer.session_id
    action, reason = sessions.cancel(sid, req.action_id)
    if action is None:
        sessions.log(sid, {"event": "cancel_rejected", "reason": reason,
                           "action_id": req.action_id})
        raise HTTPException(409, {
            "code": "ACTION_NOT_CANCELLABLE",
            "reason": reason,
            "message": "该操作已失效或已处理过，无需取消。",
        })
    sessions.log(sid, {"event": "cancelled_by_user",
                       "order": action["payload"].get("order_id"),
                       "action_id": req.action_id})
    _trusted_event(
        sid,
        "用户已通过界面按钮取消退货申请，未执行任何写入。不要声称退货已受理。",
    )
    return _action_response(sid, action)


@app.get("/api/session/action/{action_id}")
def action_status(action_id: str, customer: AuthenticatedCustomer = Depends(get_customer)):
    """Read-only outcome lookup — how a client recovers an UNKNOWN action."""
    action = sessions.get_action(customer.session_id, action_id)
    if action is None:
        raise HTTPException(404, "action not found")
    return {
        "action_id": action["action_id"],
        "type": action["type"],
        "state": action["state"],
        "result": action.get("result"),
        "error": action.get("error"),
    }


@app.get("/api/session/state")
def session_state(customer: AuthenticatedCustomer = Depends(get_customer)):
    snap = sessions.snapshot(customer.session_id)
    snap["pending_action"] = _action_view(snap["pending_action"])
    snap["customer_id"] = customer.customer_id
    return snap
