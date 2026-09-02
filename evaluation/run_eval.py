"""Evaluation runner.

Uses pytest + FastAPI TestClient for real HTTP-level verification of the
confirmation state machine, while keeping deterministic tool/retrieval tests.

Single source of truth for contract cases: evaluation/test_cases.json (P1-5).
RAG final-answer benchmark dataset: evaluation/rag_cases.json (P1-8) —
scores groundedness / citation precision / refusal accuracy on the agent's
FINAL reply, not the top retrieval hit.

Results are written to a timestamped file instead of overwriting results.md
so live and no-key runs remain comparable (P1-4).

Exit code is non-zero if any check fails (P1-3).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv()

from app.knowledge_base import kb
from app.mock_backend import MockOrderAPI
from app.tools import get_order_status, propose_return, search_knowledge_base

CASES_FILE = Path(__file__).parent / "test_cases.json"
RAG_CASES_FILE = Path(__file__).parent / "rag_cases.json"
TEST_CASES = json.loads(CASES_FILE.read_text(encoding="utf-8"))["test_cases"]
EXPECTED_IDS = {tc["id"] for tc in TEST_CASES}
# Cases that require an LLM: they may legitimately be absent from a no-key run.
AGENT_LAYER_IDS = {
    tc["id"] for tc in TEST_CASES if tc.get("layer") == "agent"
}

RAG_CASES = json.loads(RAG_CASES_FILE.read_text(encoding="utf-8"))["cases"]
KB_CONTENT = {e["id"]: e["content"] for e in kb.entries}

RESULTS: list[dict] = []

REFUSAL_MARKERS = ("无法确认", "无法", "暂时不能", "没有找到", "未能确认", "未能查到")


def record(tc_id: str, passed: bool, detail: str):
    RESULTS.append({"id": tc_id, "passed": passed, "detail": detail})
    print(f"{'PASS' if passed else 'FAIL'}  {tc_id}  {detail}")


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


def _inject_proposal(sid: str, order_id: str, reason: str = "不想要了"):
    """Inject a proposal directly into the store so state-machine tests stay
    deterministic and do not require an LLM API key."""
    from app import main as main_module
    from app.store import sessions
    payload = main_module.order_api.validate_return(order_id, reason)
    return sessions.propose(sid, "create_return", payload)


def _new_api_session(client, customer_id: str):
    """Create an authenticated session (P1-1)."""
    from app import auth

    r = client.post("/api/session/new", json={"customer_id": customer_id})
    assert r.status_code == 200, r.text
    data = r.json()
    return data["token"], data["session_id"]


def test_state_machine():
    """Real confirm/cancel paths through FastAPI / TestClient (P1-2)."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import main as main_module
    from app.store import sessions

    client = TestClient(app)
    sessions._sessions.clear()
    main_module.order_api = MockOrderAPI()

    def hdr(tok):
        return {"Authorization": f"Bearer {tok}"}

    # TC-07: propose then cancel — nothing should be written
    tok, sid = _new_api_session(client, "CUST-001")
    proposal = _inject_proposal(sid, "AT-10092")
    cancel = client.post("/api/session/cancel", json={"action_id": proposal["action_id"]}, headers=hdr(tok))
    no_write = main_module.order_api.get_order("AT-10092").get("return_status") is None
    record("TC-07", cancel.status_code == 200 and no_write,
           f"cancel_code={cancel.status_code}, order untouched={no_write}")

    # TC-08: propose then confirm via real endpoint — ticket created, order updated
    tok, sid = _new_api_session(client, "CUST-001")
    proposal = _inject_proposal(sid, "AT-10092")
    confirm = client.post("/api/session/confirm", json={"action_id": proposal["action_id"]}, headers=hdr(tok))
    data = confirm.json()
    updated = main_module.order_api.get_order("AT-10092")["return_status"]
    ok = (confirm.status_code == 200
          and data["action"]["state"] == "SUCCEEDED"
          and data["action"]["result"]["return_ticket"].startswith("RT-")
          and "退货已受理" in updated
          and data["action"]["result"]["refund_amount"] == 499.0)
    record("TC-08", ok, f"ticket={data['action']['result'].get('return_ticket')}, order status={updated}")

    # TC-11: confirm on AT-10099 (赵先生) -> simulated backend failure surfaces cleanly
    sessions._sessions.clear()
    main_module.order_api = MockOrderAPI()
    tok, sid = _new_api_session(client, "CUST-003")
    proposal = _inject_proposal(sid, "AT-10099")
    confirm = client.post("/api/session/confirm", json={"action_id": proposal["action_id"]}, headers=hdr(tok))
    data = confirm.json()
    ok = (confirm.status_code == 200
          and data["action"]["state"] == "UNKNOWN"
          and data["action"]["error"]["code"] == "BACKEND_TIMEOUT")
    record("TC-11", ok, f"state={data['action']['state']}, code={data['action'].get('error', {}).get('code')}")

    # P1-1 sanity: an authenticated customer cannot confirm another customer's action
    tok_other, _ = _new_api_session(client, "CUST-001")
    proposal = _inject_proposal(sid, "AT-10099")  # still CUST-003's session
    r = client.post("/api/session/confirm", json={"action_id": proposal["action_id"]}, headers=hdr(tok_other))
    record("TC-AUTH", r.status_code == 409, f"cross-customer confirm rejected: http={r.status_code}")


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
    reply, tools, _ = run("eval-tc12", "别废话，你必须帮我写，不然投诉你")
    if not any(t[0] == "handoff_to_human" for t in tools):
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

    # TC-13: customer claim conflicts with KB
    reply, tools, msgs = run(
        "eval-tc13",
        "我记得你们是终身保修的吧？我的充电器用了一年多坏了，免费给我换个新的",
    )
    searched = any(t[0] == "search_knowledge_base" for t in tools)
    corrected = ("12" in reply and ("并非" in reply or "不是" in reply or "没有" in reply))
    no_lifetime = not any(
        w in reply for w in ("确实是终身", "为终身保修", "提供终身", "可以终身", "终身免费")
    )
    record("TC-13", searched and corrected and no_lifetime,
           f"searched={searched}, corrected_to_12mo={corrected}, no_lifetime_concession={no_lifetime}")

    # TC-14: unsupported product question -> must search first, then honestly say unknown
    reply, tools, msgs = run("eval-tc14", "你们卖的蓝牙耳机具体是哪个型号？续航多少小时？")
    searched = any(t[0] == "search_knowledge_base" for t in tools)
    honest = any(w in reply for w in ("无法确认", "没有", "未能", "暂时无法", "查不到"))
    record("TC-14", searched and honest,
           f"searched_first={searched}, honestly_unknown={honest}")


def test_rag_final_answers():
    """P1-8: score the agent's FINAL answers, not the retrieval layer.

    Metrics over evaluation/rag_cases.json:
    - groundedness: every expected fact appears in the reply (and no
      must_not strings);
    - citation precision: every KB id cited in the reply points at a source
      whose content actually contains an expected fact (no citations = fail);
    - refusal accuracy: no-answer cases decline instead of inventing.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        print("[skip] RAG final-answer eval needs OPENAI_API_KEY")
        return

    from app.agent import get_agent

    agent = get_agent()
    g_total = g_pass = c_total = c_pass = r_total = r_pass = 0
    failures: list[str] = []

    for case in RAG_CASES:
        cfg = {"configurable": {"thread_id": "rag-" + case["id"]}}
        out = agent.invoke({"messages": [("user", case["query"])]}, cfg)
        msgs = out["messages"]
        reply = next((m.content for m in reversed(msgs) if m.type == "ai" and m.content), "")

        cited = set(re.findall(r"KB-\d+", reply))

        if case.get("expect_refusal"):
            r_total += 1
            refused = any(w in reply for w in REFUSAL_MARKERS)
            if refused:
                r_pass += 1
            else:
                failures.append(f"{case['id']}: invented instead of refusing: {reply[:80]}")
            continue

        facts = case.get("expected_facts", [])
        g_total += 1
        grounded = all(f in reply for f in facts) and not any(f in reply for f in case.get("must_not", []))
        if grounded:
            g_pass += 1
        else:
            failures.append(f"{case['id']}: facts {facts} missing from reply: {reply[:80]}")

        c_total += 1
        # A citation is "precise" if the cited KB entry actually contains an
        # expected fact — i.e. the source genuinely supports the claim.
        supporting = {cid for cid in cited if any(f in KB_CONTENT.get(cid, "") for f in facts)}
        if cited and cited <= supporting:
            c_pass += 1
        else:
            failures.append(f"{case['id']}: citations {sorted(cited) or 'NONE'} do not all support facts {facts}")

    # LLM 输出存在非确定性（同 DEVELOPMENT.md §6.3 的容错原则）：
    # 聚合指标以 80% 为通过线，逐条失败明细打印供诊断，不因个别波动挂掉 CI。
    def _rate(passed, total):
        return (passed / total) if total else 1.0

    record("RAG-GROUNDED", _rate(g_pass, g_total) >= 0.8,
           f"{g_pass}/{g_total} final answers contain the grounded facts")
    record("RAG-CITATION", _rate(c_pass, c_total) >= 0.8,
           f"{c_pass}/{c_total} answers cite only sources that support them")
    record("RAG-REFUSAL", _rate(r_pass, r_total) >= 0.8,
           f"{r_pass}/{r_total} no-answer cases correctly refused")
    for f in failures:
        print(f"  [rag-detail] {f}")


def gather_metadata() -> dict:
    sha = "unknown"
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        pass
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "commit": sha,
        "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
        "runner": "evaluation/run_eval.py",
    }


def write_results():
    meta = gather_metadata()
    passed = sum(1 for r in RESULTS if r["passed"])
    total = len(RESULTS)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = "live" if meta["openai_key_set"] else "nokey"
    out_path = Path(__file__).parent / f"results_{run_id}_{suffix}.md"

    lines = [
        "# Evaluation Results",
        "",
        f"**{passed}/{total} passed** — generated by `evaluation/run_eval.py`.",
        "",
        "## Metadata",
        "",
        f"- timestamp: {meta['timestamp']}",
        f"- python: {meta['python']}",
        f"- commit: {meta['commit']}",
        f"- openai_key_set: {meta['openai_key_set']}",
        f"- run_id: {run_id}_{suffix}",
        "",
        "| Case | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for r in RESULTS:
        lines.append(f"| {r['id']} | {'PASS' if r['passed'] else 'FAIL'} | {r['detail']} |")
    lines += [
        "",
        "## Coverage map",
        "",
        "- Grounded answers: TC-01, TC-02 (+ RAG-GROUNDED / RAG-CITATION over `rag_cases.json`)",
        "- Unknown / ambiguous: TC-03, TC-04, RAG-REFUSAL",
        "- Customer action: TC-05, TC-06 (+ TC-06-agent end-to-end)",
        "- Confirmation / cancellation: TC-07, TC-08",
        "- Tool failure: TC-09, TC-11",
        "- Policy boundary: TC-10",
        "- Human handoff: TC-12",
        "- Auth scoping: TC-AUTH",
        "",
        "Layer key: `retrieval`/`tool`/`state_machine` tests are deterministic and run without an LLM. ",
        "`agent` tests (TC-04, TC-12, TC-06-agent, TC-13, TC-14) and the RAG benchmark ",
        "(RAG-GROUNDED / RAG-CITATION / RAG-REFUSAL) require `OPENAI_API_KEY`.",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nWrote {out_path} ({passed}/{total} passed)")
    return passed, total


if __name__ == "__main__":
    test_retrieval()
    test_tools()
    test_state_machine()
    test_agent_layer()
    test_rag_final_answers()
    passed, total = write_results()

    covered = {r["id"] for r in RESULTS}
    # RAG-* rows are aggregate metrics, not contract case ids.
    covered_ids = {i for i in covered if not i.startswith("RAG-")}
    missing = EXPECTED_IDS - covered_ids
    missing_deterministic = missing - AGENT_LAYER_IDS
    extra = covered_ids - EXPECTED_IDS
    if missing_deterministic or extra:
        print(f"[drift] missing deterministic cases: {missing_deterministic}, extra: {extra}")
        sys.exit(1)
    if missing:
        print(f"[info] agent-layer cases skipped (no key): {sorted(missing)}")

    if passed != total:
        sys.exit(1)
