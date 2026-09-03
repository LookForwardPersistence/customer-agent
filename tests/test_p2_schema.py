"""P2-4: request schemas reject malformed input before any handler runs."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _session(client: TestClient, customer_id: str = "CUST-001") -> tuple[str, str]:
    r = client.post("/api/session/new", json={"customer_id": customer_id})
    assert r.status_code == 200
    return r.json()["token"], r.json()["session_id"]


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- ChatRequest -----------------------------------------------------------


def test_chat_rejects_empty_message(client):
    tok, _ = _session(client)
    r = client.post("/api/chat", json={"message": ""}, headers=_hdr(tok))
    assert r.status_code == 422  # schema: min_length=1


def test_chat_rejects_whitespace_only_with_400(client):
    # Whitespace passes the schema (len >= 1) but must still 400 after strip.
    tok, _ = _session(client)
    r = client.post("/api/chat", json={"message": "   \n\t "}, headers=_hdr(tok))
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_chat_rejects_overlong_message(client):
    tok, _ = _session(client)
    r = client.post("/api/chat", json={"message": "x" * 2001}, headers=_hdr(tok))
    assert r.status_code == 422  # schema: max_length=2000


def test_chat_rejects_missing_message_field(client):
    tok, _ = _session(client)
    r = client.post("/api/chat", json={}, headers=_hdr(tok))
    assert r.status_code == 422


def test_chat_accepts_message_at_limit_boundary(client):
    # 2000 chars is legal — no key configured, so we only assert it is NOT 422.
    tok, _ = _session(client)
    r = client.post("/api/chat", json={"message": "退货 " + "y" * 1996}, headers=_hdr(tok))
    assert r.status_code != 422


# --- ActionRequest ---------------------------------------------------------


def test_confirm_rejects_action_id_with_bad_charset(client):
    tok, _ = _session(client)
    r = client.post("/api/session/confirm", json={"action_id": "bad id!!"},
                    headers=_hdr(tok))
    assert r.status_code == 422  # pattern: ^[A-Za-z0-9_-]+$


def test_confirm_rejects_overlong_action_id(client):
    tok, _ = _session(client)
    r = client.post("/api/session/confirm", json={"action_id": "a" * 65},
                    headers=_hdr(tok))
    assert r.status_code == 422  # max_length=64


def test_confirm_rejects_short_action_id(client):
    tok, _ = _session(client)
    r = client.post("/api/session/confirm", json={"action_id": "abc"}, headers=_hdr(tok))
    assert r.status_code == 422  # min_length=8


def test_generated_action_id_passes_schema(client):
    # Round-trip: a real action_id from the store must satisfy the pattern
    # (no LLM needed — drive the store directly).
    import re

    from app.store import sessions

    sessions.clear()
    record = sessions.propose("sess-schema", "create_return", {"order_id": "AT-10086"})
    aid = record["action_id"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", aid), aid
    # And the model accepts it end-to-end via cancel (404: unknown session id).
    tok, _ = _session(client)
    r = client.post("/api/session/cancel", json={"action_id": aid}, headers=_hdr(tok))
    assert r.status_code in (200, 404, 409)  # any of these proves schema passed


# --- NewSessionRequest -----------------------------------------------------


def test_new_session_rejects_malformed_customer_id(client):
    r = client.post("/api/session/new", json={"customer_id": "DROP TABLE"})
    assert r.status_code == 422  # pattern: ^CUST-\d{3,}$


def test_new_session_rejects_unknown_but_wellformed_id(client):
    # Pattern-valid but not in the directory -> issuance fails cleanly.
    r = client.post("/api/session/new", json={"customer_id": "CUST-999"})
    assert r.status_code in (400, 404)


def test_new_session_default_customer(client):
    r = client.post("/api/session/new", json={})
    assert r.status_code == 200
    assert r.json()["customer_id"] == "CUST-001"
