"""P2-5: LLM channel circuit breaker — timeouts degrade, never hang or 500."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _session(client: TestClient) -> str:
    r = client.post("/api/session/new", json={"customer_id": "CUST-001"})
    assert r.status_code == 200
    return r.json()["token"]


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _ExplodingGraph:
    """Stands in for the LangGraph agent; invoke always fails like a hung LLM."""

    def get_state(self, cfg):
        class _V:
            values = {"messages": []}

        return _V()

    def invoke(self, payload, cfg):
        raise TimeoutError("simulated provider hang")


def test_llm_failure_degrades_to_deterministic_reply(client, monkeypatch):
    import app.main as main_mod
    from app.store import sessions

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(main_mod, "get_agent", lambda: _ExplodingGraph())
    sessions.clear()

    tok = _session(client)
    r = client.post("/api/chat", json={"message": "退货政策是什么"}, headers=_hdr(tok))
    assert r.status_code == 200  # never a 500, never a hang
    body = r.json()
    assert "连接中断" in body["reply"]
    assert body["pending_action"] is None
    assert body["sources"] == []
    # the failure is auditable
    kinds = [e["event"] for e in body["events"]]
    assert "agent_degraded" in kinds
    degraded = next(e for e in body["events"] if e["event"] == "agent_degraded")
    assert degraded["code"] == "TimeoutError"


def test_llm_timeout_config_applied(monkeypatch):
    """build_model() must pass an explicit request_timeout + retry cap."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "7.5")
    from app.agent import build_model

    model = build_model()
    assert model.max_retries == 1
    # openai client exposes timeout via request_timeout on the runnable config
    assert model.request_timeout == 7.5


def test_agent_degraded_event_is_registered():
    from app.events import REGISTRY, AgentDegraded

    assert REGISTRY["agent_degraded"] is AgentDegraded
    d = AgentDegraded("APITimeoutError").to_dict()
    assert d == {"event": "agent_degraded", "code": "APITimeoutError"}
