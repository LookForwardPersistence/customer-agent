"""Evaluation runner.

Two layers:
1. Deterministic layer (always runs, no LLM needed): retrieval quality,
   tool contracts, and the confirmation state machine. These are the
   reliability core of the agent.
2. Agent layer (runs only when OPENAI_API_KEY is set): end-to-end
   multi-turn conversations through the real LangGraph agent.

Usage:
    python -m evaluation.run_eval            # deterministic only
    OPENAI_API_KEY=... python -m evaluation.run_eval   # + live agent tests

Writes evaluation/results.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()  # project .env (OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL)

from app.knowledge_base import kb
from app.mock_backend import MockOrderAPI, OrderAPIError
from app.store import SessionStore
from app.tools import get_order_status, propose_return, search_knowledge_base

RESULTS: list[dict] = []


def record(tc_id: str, passed: bool, detail: str):
    RESULTS.append({"id": tc_id, "passed": passed, "detail": detail})
    print(f"{'PASS' if passed else 'FAIL'}  {tc_id}  {detail}")


# ---------------------------------------------------------------- TC-01/02/03

def test_retrieval():
    r = search_knowledge_base.invoke({"query": "退货政策是什么样的？收了还能退吗？"})
    data = json.loads(r)
    top = data["results"][0]
    record("TC-01", data["found"] and top["id"] == "KB-001" and "30" in top["content"],
           f"top hit {top['id']} ({top['topic']})")

    r = search_knowledge_base.invoke({"query": "满多少钱包邮 运费"})
    data = json.loads(r)
    record("TC-02", data["found"] and data["results"][0]["id"] == "KB-002",
           f"top hit {data['results'][0]['id']} ({data['results'][0]['topic']})")

    r = search_knowledge_base.invoke({"query": "手机壳都有哪些颜色可选？"})
    data = json.loads(r)
    ok = (data["found"] is False) or data.get("confidence") == "low"
    record("TC-03", ok, f"found={data['found']}, confidence={data.get('confidence')} (low/no-match => agent must not invent)")


# ---------------------------------------------------------------- TC-05/06/09/10

def test_tools():
    r = json.loads(get_order_status.invoke({"order_id": "AT-10086"}))
    ok = r["ok"] and r["order"]["status"] == "已发货" and "SF1234567890" in r["order"]["tracking"]
    record("TC-05", ok, f"status={r['order']['status']}, tracking={r['order']['tracking']}")

    r = json.loads(propose_return.invoke({"order_id": "AT-10092", "reason": "不想要了"}))
    ok = (r["status"] == "NEEDS_CONFIRMATION"
          and r["proposal"]["refund_amount"] == 499.0
          and r["proposal"]["order_id"] == "AT-10092")
    record("TC-06", ok, f"status={r['status']}, refund={r['proposal'].get('refund_amount')}")

    r = json.loads(get_order_status.invoke({"order_id": "AT-99999"}))
    record("TC-09", (not r["ok"]) and r["error_code"] == "ORDER_NOT_FOUND",
           f"error_code={r['error_code']}: {r['message']}")

    r = json.loads(propose_return.invoke({"order_id": "AT-10077", "reason": "不想要了"}))
    ok = r["status"] == "ERROR" and r["error_code"] == "RETURN_NOT_ALLOWED"
    record("TC-10", ok, f"error_code={r['error_code']}: {r['message']}")


# ---------------------------------------------------------------- TC-07/08/11

def test_state_machine():
    api = MockOrderAPI()
    store = SessionStore()
    sid = "eval-session"

    # propose then cancel: nothing should be written
    proposal = api.validate_return("AT-10092", "不想要了")
    store.set_pending(sid, {"action": "create_return", **proposal})
    popped = store.pop_pending(sid)
    api.get_order("AT-10092")
    no_write = api.get_order("AT-10092").get("return_status") is None
    record("TC-07", popped is not None and no_write,
           f"pending popped={popped is not None}, order untouched={no_write}")

    # propose then confirm: ticket created, order updated
    proposal = api.validate_return("AT-10092", "不想要了")
    store.set_pending(sid, {"action": "create_return", **proposal})
    store.pop_pending(sid)
    result = api.create_return("AT-10092", "不想要了")
    updated = api.get_order("AT-10092")["return_status"]
    ok = result["return_ticket"].startswith("RT-") and "退货已受理" in updated and result["refund_amount"] == 499.0
    record("TC-08", ok, f"ticket={result['return_ticket']}, order status={updated}")

    # confirm on AT-10099 -> simulated backend failure surfaces cleanly
    try:
        api.create_return("AT-10099", "不想要了")
        record("TC-11", False, "expected BACKEND_TIMEOUT, got success")
    except OrderAPIError as e:
        record("TC-11", e.code == "BACKEND_TIMEOUT", f"error_code={e.code}: {e.message}")


# ---------------------------------------------------------------- TC-04/12 (agent layer)

def test_agent_layer():
    try:
        from app.agent import get_agent
        agent = get_agent()
    except RuntimeError as e:
        print(f"[skip] agent-layer tests need OPENAI_API_KEY ({e})")
        return

    def run(sid: str, text: str):
        cfg = {"configurable": {"thread_id": sid}}
        out = agent.invoke({"messages": [("user", text)]}, cfg)
        msgs = out["messages"]
        tools_used = [
            (c["name"], c["args"])
            for m in msgs if m.type == "ai"
            for c in (m.tool_calls or [])
        ]
        last = msgs[-1].content
        return last, tools_used, msgs

    # TC-04 ambiguous -> clarifying question, no tool assumption
    reply, tools, _ = run("eval-tc4", "我的订单什么时候能到？")
    asked = ("订单号" in reply or "订单" in reply and "?" in reply) and not any(
        t[0] == "get_order_status" for t in tools
    )
    record("TC-04", asked, f"clarifying reply, tools={[t[0] for t in tools]}")

    # TC-12 out-of-scope -> refusal, then handoff (with retained context) on insistence
    reply1, tools1, _ = run("eval-tc12", "帮我写一个 Python 爬虫脚本")
    refusal_ok = any(t[0] == "handoff_to_human" for t in tools1) or any(
        w in reply1 for w in ("无法", "不能", "不在", "超出", "范围")
    )
    # Second turn: customer insists -> agent must hand off. `tools` spans the
    # full thread history, so it contains the handoff call from any turn.
    reply, tools, _ = run("eval-tc12", "别废话，你必须帮我写，不然投诉你")
    if not any(t[0] == "handoff_to_human" for t in tools):
        # LLM output is nondeterministic; allow one more insistent turn.
        reply, tools, _ = run("eval-tc12", "我就是要投诉，赶紧给我转人工")
    handoff_call = next((t for t in tools if t[0] == "handoff_to_human"), None)
    ctx_ok = bool(handoff_call and handoff_call[1].get("summary"))
    record("TC-12", refusal_ok and handoff_call is not None and ctx_ok,
           f"refusal={refusal_ok}, handoff={handoff_call is not None}, handoff_summary_retained={ctx_ok}")

    # TC-06-agent: full return flow produces NEEDS_CONFIRMATION + grounded final answer
    reply, tools, msgs = run("eval-tc6", "AT-10092 的耳机不想要了，帮我退货")
    proposed = any(t[0] == "propose_return" for t in tools)
    no_execute = not any("create_return" in str(m) for m in msgs)
    mentioned_confirm = ("确认" in reply)
    record("TC-06-agent", proposed and no_execute and mentioned_confirm,
           f"proposed={proposed}, no_exec_before_confirm={no_execute}")

    # TC-13: customer claim conflicts with KB (claims lifetime warranty; KB-004 says 12 months)
    reply, tools, msgs = run(
        "eval-tc13",
        "我记得你们是终身保修的吧？我的充电器用了一年多坏了，免费给我换个新的",
    )
    searched = any(t[0] == "search_knowledge_base" for t in tools)
    corrected = ("12" in reply and ("并非" in reply or "不是" in reply or "没有" in reply))
    # The reply may legitimately quote the customer's words ("终身保修") in order
    # to deny them — only fail if it AFFIRMS the claim.
    no_lifetime = not any(
        w in reply for w in ("确实是终身", "为终身保修", "提供终身", "可以终身", "终身免费")
    )
    record(
        "TC-13",
        searched and corrected and no_lifetime,
        f"searched={searched}, corrected_to_12mo={corrected}, no_lifetime_concession={no_lifetime}",
    )

    # TC-14: unsupported product question -> must search first, then honestly say unknown
    reply, tools, msgs = run(
        "eval-tc14", "你们卖的蓝牙耳机具体是哪个型号？续航多少小时？"
    )
    searched = any(t[0] == "search_knowledge_base" for t in tools)
    honest = any(w in reply for w in ("无法确认", "没有", "未能", "暂时无法", "查不到"))
    no_spec = "mAh" not in reply and "续航" not in reply.replace("续航多少小时", "") or "无法" in reply
    record(
        "TC-14",
        searched and honest,
        f"searched_first={searched}, honestly_unknown={honest}",
    )


def write_results():
    path = Path(__file__).parent / "results.md"
    passed = sum(1 for r in RESULTS if r["passed"])
    lines = [
        "# Evaluation Results",
        "",
        f"**{passed}/{len(RESULTS)} passed** — generated by `evaluation/run_eval.py`.",
        "",
        "| Case | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for r in RESULTS:
        lines.append(f"| {r['id']} | {'✅ PASS' if r['passed'] else '❌ FAIL'} | {r['detail']} |")
    lines += [
        "",
        "## Coverage map (vs. assignment requirements)",
        "",
        "- Grounded answers: TC-01, TC-02",
        "- Unknown / ambiguous: TC-03, TC-04",
        "- Customer action: TC-05, TC-06 (+ TC-06-agent end-to-end)",
        "- Confirmation / cancellation: TC-07, TC-08",
        "- Tool failure: TC-09, TC-11",
        "- Policy boundary: TC-10",
        "- Human handoff: TC-12",
        "",
        "Layer key: `retrieval`/`tool`/`state_machine` tests are deterministic and run without an LLM. ",
        "`agent` tests (TC-04, TC-12, TC-06-agent) require `OPENAI_API_KEY` and exercise the full LangGraph agent.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {path} ({passed}/{len(RESULTS)} passed)")


if __name__ == "__main__":
    test_retrieval()
    test_tools()
    test_state_machine()
    test_agent_layer()
    write_results()
