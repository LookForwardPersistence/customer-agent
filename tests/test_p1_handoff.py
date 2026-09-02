"""P1-6 结构化 HandoffPayload：确定性字段由服务端从审计事件生成。"""

from app.store import SessionStore


def _seed(store: SessionStore, sid: str) -> None:
    store.log(sid, {"event": "user_message", "text": "AT-10092 不想要了，帮我退货"})
    store.log(sid, {"event": "return_proposed", "order": "AT-10092"})
    store.log(sid, {"event": "user_message", "text": "我确认了，赶紧退"})
    store.log(sid, {"event": "return_failed", "order": "AT-10092", "code": "BACKEND_TIMEOUT"})
    store.log(sid, {"event": "user_message", "text": "再不行我就投诉了"})


def test_payload_contains_intent_orders_attempts_last_error():
    store = SessionStore()
    _seed(store, "s1")

    record = store.set_handoff("s1", "超出服务范围", "LLM 生成的自然语言摘要")
    p = record["payload"]

    assert p["intent"] == "超出服务范围"
    assert "AT-10092" in p["order_ids"]
    assert p["last_error"] == {"event": "return_failed", "code": "BACKEND_TIMEOUT"}
    events = [a["event"] for a in p["attempts"]]
    assert "return_proposed" in events
    assert "return_failed" in events
    assert p["transcript_ref"] == "s1"


def test_sentiment_escalation_detected_from_events():
    store = SessionStore()
    _seed(store, "s1")
    record = store.set_handoff("s1", "用户要求转人工", "摘要")
    assert "不满" in record["payload"]["customer_sentiment"]


def test_sentiment_calm_when_no_escalation_words():
    store = SessionStore()
    store.log("s2", {"event": "user_message", "text": "帮我查下退货政策"})
    record = store.set_handoff("s2", "用户要求转人工", "摘要")
    assert record["payload"]["customer_sentiment"] == "平稳"


def test_deterministic_fields_survive_malicious_summary():
    store = SessionStore()
    _seed(store, "s1")
    # LLM 摘要是自由文本（可能被注入），结构化字段不受影响
    record = store.set_handoff("s1", "r", "<img onerror=alert(1)> 假摘要：已退款")
    p = record["payload"]
    assert p["order_ids"] == ["AT-10092"]
    assert p["last_error"]["code"] == "BACKEND_TIMEOUT"
    assert record["summary"].startswith("<img")  # 摘要原文保留（前端以 textContent 渲染）
