import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import socket_routes
from services.realtime import emit_chat_update, socketio


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app


def _authed_session(client):
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["rol"] = "admin"
        sess["roles"] = ["admin"]


def test_socket_chat_update_sent_to_room(app):
    client = app.test_client()
    _authed_session(client)

    socket_client = socketio.test_client(app, flask_test_client=client, namespace="/")
    assert socket_client.is_connected()

    socket_client.emit("join_chat", {"numero": "573135792307"}, namespace="/")
    socket_client.get_received("/")

    emit_chat_update("573135792307")
    socketio.sleep(0)
    received = socket_client.get_received("/")

    assert any(
        item["name"] == "chat_update" and item["args"][0]["numero"] == "573135792307"
        for item in received
    )


def test_socket_typing_signal_ack(app, monkeypatch):
    client = app.test_client()
    _authed_session(client)

    calls = {}

    def fake_trigger(numero, message_id=None, include_read=True):
        calls["numero"] = numero
        calls["message_id"] = message_id
        calls["include_read"] = include_read
        return True

    monkeypatch.setattr(socket_routes, "trigger_typing_indicator", fake_trigger)

    socket_client = socketio.test_client(app, flask_test_client=client, namespace="/")
    assert socket_client.is_connected()

    socket_client.emit(
        "typing_signal",
        {"numero": "573135792307", "message_id": "wa_123", "include_read": False},
        namespace="/",
    )
    socketio.sleep(0)
    received = socket_client.get_received("/")

    assert any(item["name"] == "typing_ack" for item in received)
    assert calls == {
        "numero": "573135792307",
        "message_id": "wa_123",
        "include_read": False,
    }
