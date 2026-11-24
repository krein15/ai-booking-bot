from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.llm.base import LLMUnavailableError
from app.llm.fake import OfflineDemoLLM, ScriptedLLM
from app.main import create_app
from tests.test_sheets import FakeClient as FakeSheetsClient

SESSION = "test-session-0001"


def client_for(make_settings, now_fn, llm=None, sheets=None, **overrides):
    app = create_app(make_settings(**overrides), llm=llm or OfflineDemoLLM(), now_fn=now_fn, sheets_client_factory=sheets)
    return TestClient(app)


def test_config_and_pages(make_settings, now_fn):
    with client_for(make_settings, now_fn, google_sheet_id="abc") as client:
        cfg = client.get("/api/config").json()
        assert cfg["assistant_name"] == "Ева"
        assert cfg["quick_replies"]
        assert cfg["sheet_url"] == "https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing"
        assert "data-target" in client.get("/").text
        widget = client.get("/widget.js")
        assert widget.headers["content-type"].startswith("application/javascript")
        assert client.get("/health").json()["llm"] == "offline-demo"


def test_full_booking_flow(make_settings, now_fn):
    with client_for(make_settings, now_fn) as client:
        first = client.post("/api/chat", json={"session_id": SESSION, "message": "Когда свободно?"})
        assert first.status_code == 200, first.text
        assert "Свободно" in first.json()["reply"]
        assert first.json()["booking"] is None

        second = client.post(
            "/api/chat", json={"session_id": SESSION, "message": "Запиши меня, телефон 8 900 123-45-67"}
        )
        body = second.json()
        assert body["booking"]["number"] == 1
        assert body["remaining_messages"] == 38

        recent = client.get("/api/bookings/recent").json()["items"]
        assert recent[0]["phone"] == "+7 (900) ***-**-67"
        assert recent[0]["client"] == "Тест Т."
        assert recent[0]["in_sheet"] is False


def test_booking_reaches_google_sheet(make_settings, now_fn):
    sheets = FakeSheetsClient()
    with client_for(make_settings, now_fn, sheets=lambda: sheets) as client:
        client.post("/api/chat", json={"session_id": SESSION, "message": "запиши, 8 900 123-45-67"})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not sheets.spreadsheet.sheets.get("Записи", None):
            time.sleep(0.05)
        while time.monotonic() < deadline and not sheets.spreadsheet.sheets["Записи"].rows:
            time.sleep(0.05)
        rows = sheets.spreadsheet.sheets["Записи"].rows
        assert len(rows) == 1 and rows[0][6] == "+7 (900) ***-**-67"
        assert client.get("/api/bookings/recent").json()["items"][0]["in_sheet"] is True


def test_validation_errors(make_settings, now_fn):
    with client_for(make_settings, now_fn, max_message_chars=20) as client:
        assert client.post("/api/chat", json={"session_id": "bad id!", "message": "hi"}).status_code == 422
        too_long = client.post("/api/chat", json={"session_id": SESSION, "message": "x" * 21})
        assert too_long.status_code == 400 and "длинное" in too_long.json()["detail"]


def test_rate_limits(make_settings, now_fn):
    with client_for(make_settings, now_fn, ip_messages_per_minute=2) as client:
        for _ in range(2):
            assert client.post("/api/chat", json={"session_id": SESSION, "message": "привет"}).status_code == 200
        blocked = client.post("/api/chat", json={"session_id": SESSION, "message": "привет"})
        assert blocked.status_code == 429 and "Подождите" in blocked.json()["detail"]


def test_session_message_limit(make_settings, now_fn):
    with client_for(make_settings, now_fn, max_user_messages_per_session=1) as client:
        assert client.post("/api/chat", json={"session_id": SESSION, "message": "1"}).status_code == 200
        blocked = client.post("/api/chat", json={"session_id": SESSION, "message": "2"})
        assert blocked.status_code == 429 and "Начните новый" in blocked.json()["detail"]


def test_new_sessions_limit(make_settings, now_fn):
    with client_for(make_settings, now_fn, ip_new_sessions_per_day=1) as client:
        assert client.post("/api/chat", json={"session_id": "session-aaaa", "message": "1"}).status_code == 200
        assert client.post("/api/chat", json={"session_id": "session-aaaa", "message": "2"}).status_code == 200
        assert client.post("/api/chat", json={"session_id": "session-bbbb", "message": "1"}).status_code == 429


def test_model_outage_returns_503_and_keeps_quota(make_settings, now_fn):
    def fail(*_):
        raise LLMUnavailableError("down")

    with client_for(make_settings, now_fn, llm=ScriptedLLM([fail]), max_user_messages_per_session=1) as client:
        response = client.post("/api/chat", json={"session_id": SESSION, "message": "привет"})
        assert response.status_code == 503 and "недоступна" in response.json()["detail"]
        session = client.app.state.sessions.get_or_create(SESSION, "x")
        assert session.user_messages == 0 and session.history == []
