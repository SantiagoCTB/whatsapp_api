import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import chat_routes
from services import whatsapp_api as whatsapp_api


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_start_typing_feedback_skips_state_when_disabled(monkeypatch):
    numero = "5215550000"
    monkeypatch.setattr(whatsapp_api, "_TYPING_ENABLED", False)
    with whatsapp_api._typing_lock:
        whatsapp_api._typing_ui_state.clear()
        whatsapp_api._typing_sessions.clear()

    whatsapp_api.start_typing_feedback(numero)

    with whatsapp_api._typing_lock:
        assert numero not in whatsapp_api._typing_ui_state
        assert numero not in whatsapp_api._typing_sessions


def test_is_typing_feedback_active_returns_false_when_disabled(monkeypatch):
    numero = "5215550001"
    monkeypatch.setattr(whatsapp_api, "_TYPING_ENABLED", False)

    with whatsapp_api._typing_lock:
        whatsapp_api._typing_ui_state.add(numero)
    try:
        assert whatsapp_api.is_typing_feedback_active(numero) is False
    finally:
        with whatsapp_api._typing_lock:
            whatsapp_api._typing_ui_state.discard(numero)


def test_get_chat_typing_flag_disabled(client, monkeypatch):
    numero = "5215550002"

    class DummyCursor:
        def execute(self, query, params=None):
            self.last_query = (query, params)

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class DummyConnection:
        def __init__(self):
            self._cursor = DummyCursor()
            self.closed = False

        def cursor(self):
            return self._cursor

        def close(self):
            self.closed = True

    monkeypatch.setattr(chat_routes, "get_connection", lambda: DummyConnection())
    monkeypatch.setattr(chat_routes, "_table_exists", lambda *args, **kwargs: False)
    monkeypatch.setattr(whatsapp_api, "_TYPING_ENABLED", False)

    with whatsapp_api._typing_lock:
        whatsapp_api._typing_ui_state.clear()
        whatsapp_api._typing_sessions.clear()

    whatsapp_api.start_typing_feedback(numero)

    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["rol"] = "admin"

    response = client.get(f"/get_chat/{numero}")

    assert response.status_code == 200
    data = response.get_json()
    assert data["mensajes"] == []
    assert data["is_typing"] is False

    with whatsapp_api._typing_lock:
        whatsapp_api._typing_ui_state.clear()
        whatsapp_api._typing_sessions.clear()
