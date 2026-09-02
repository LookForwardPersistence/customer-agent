"""FastAPI server: chat endpoint, confirmation flow, static UI."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

load_dotenv()

from .agent import get_agent
from .mock_backend import OrderAPIError, order_api
from .store import sessions

app = FastAPI(title="Aurora Tech Store Customer Agent")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ConfirmRequest(BaseModel):
    session_id: str


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
                sources = [
                    {"id": r["id"], "topic": r["topic"]} for r in parsed["results"]
                ]
            elif name == "propose_return" and parsed.get("status") == "NEEDS_CONFIRMATION":
                pending = parsed["proposal"]
            elif name == "handoff_to_human":
                handoff = {
                    "reason": parsed.get("reason"),
                    "summary": parsed.get("summary"),
                }
    return {"reply": reply, "sources": sources, "trace": trace, "pending": pending, "handoff": handoff}


def _state_payload(sid: str, info: dict[str, Any]) -> dict[str, Any]:
    snap = sessions.snapshot(sid)
    return {
        "reply": info["reply"],
        "sources": info["sources"],
        "trace": info["trace"],
        "pending_action": snap["pending_action"] if info["pending"] is None else info["pending"],
        "handoff": snap["handoff"] if info["handoff"] is None else info["handoff"],
        "events": snap["events"],
    }


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
        sessions.set_pending(sid, {"action": "create_return", **info["pending"]})
        sessions.log(sid, {"event": "return_proposed", "order": info["pending"]["order_id"]})
    if info["handoff"]:
        record = sessions.set_handoff(sid, info["handoff"]["reason"], info["handoff"]["summary"])
        sessions.log(sid, {"event": "handoff", "reason": info["handoff"]["reason"]})
        info["handoff"] = record

    return _state_payload(sid, info)


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/chat")
def chat(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(400, "message must not be empty")
    sessions.log(req.session_id, {"event": "user_message", "text": req.message[:120]})
    return _run_agent(req.session_id, req.message)


@app.post("/api/session/confirm")
def confirm_action(req: ConfirmRequest):
    """Execute the pending return AFTER the user clicked the confirm button."""
    sid = req.session_id
    pending = sessions.pop_pending(sid)
    if not pending:
        raise HTTPException(400, "No pending action to confirm")

    sessions.log(sid, {"event": "confirmed_by_user", "order": pending["order_id"]})
    try:
        result = order_api.create_return(pending["order_id"], pending["reason"])
        sessions.log(sid, {"event": "return_executed", "ticket": result["return_ticket"]})
        event = (
            f"[系统事件] 用户已点击确认按钮，退货已执行。\n执行结果：{json.dumps(result, ensure_ascii=False)}\n"
            "请向客户确认退货已受理，并告知下一步（寄回商品、退款时效）。"
        )
    except OrderAPIError as e:
        sessions.log(sid, {"event": "return_failed", "code": e.code})
        event = (
            f"[系统事件] 用户已点击确认按钮，但退货执行失败。\n错误（{e.code}）：{e.message}\n"
            "请向客户致歉并说明情况；根据失败原因给出下一步建议（稍后重试或转人工）。"
        )
    return _run_agent(sid, event)


@app.post("/api/session/cancel")
def cancel_action(req: ConfirmRequest):
    sid = req.session_id
    pending = sessions.pop_pending(sid)
    if not pending:
        raise HTTPException(400, "No pending action to cancel")
    sessions.log(sid, {"event": "cancelled_by_user", "order": pending["order_id"]})
    event = (
        "[系统事件] 用户已点击取消按钮，退货申请未执行，方案已作废。"
        "请礼貌确认，并询问是否还需要其他帮助。"
    )
    return _run_agent(sid, event)


@app.get("/api/session/{sid}/state")
def session_state(sid: str):
    return sessions.snapshot(sid)


@app.post("/api/session/new")
def new_session():
    return {"session_id": uuid.uuid4().hex[:12]}
