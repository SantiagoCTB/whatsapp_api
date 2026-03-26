import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("INIT_DB_ON_START", "0")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import chat_routes


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_send_message_flow_sets_attention_state(client, monkeypatch):
    update_calls = []

    monkeypatch.setattr(chat_routes, "enviar_mensaje", lambda *_, **__: (True, None))
    monkeypatch.setattr(
        chat_routes,
        "obtener_ultimo_mensaje_cliente_info",
        lambda *_: {"tipo": "cliente", "timestamp": None},
    )
    monkeypatch.setattr(chat_routes, "_schedule_followup_messages", lambda *_, **__: None)
    monkeypatch.setattr(chat_routes, "get_chat_state", lambda *_: ("ia_chat", None, "en_flujo"))
    monkeypatch.setattr(chat_routes, "update_chat_state", lambda *args, **kwargs: update_calls.append((args, kwargs)))

    with client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["rol"] = "admin"

    response = client.post(
        "/send_message",
        json={
            "numero": "573001112233",
            "mensaje": "Completa este formulario",
            "tipo_respuesta": "flow",
            "opciones": {"flow_cta": "Abrir demo", "flow_id": "123"},
        },
    )

    assert response.status_code == 200
    assert update_calls
    assert update_calls[-1][0] == ("573001112233", "ia_chat", "atencion")


def test_send_message_text_keeps_agent_state(client, monkeypatch):
    update_calls = []

    monkeypatch.setattr(chat_routes, "enviar_mensaje", lambda *_, **__: (True, None))
    monkeypatch.setattr(
        chat_routes,
        "obtener_ultimo_mensaje_cliente_info",
        lambda *_: {"tipo": "cliente", "timestamp": None},
    )
    monkeypatch.setattr(chat_routes, "_schedule_followup_messages", lambda *_, **__: None)
    monkeypatch.setattr(chat_routes, "get_chat_state", lambda *_: ("ia_chat", None, "en_flujo"))
    monkeypatch.setattr(chat_routes, "update_chat_state", lambda *args, **kwargs: update_calls.append((args, kwargs)))

    with client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["rol"] = "admin"

    response = client.post(
        "/send_message",
        json={
            "numero": "573001112233",
            "mensaje": "Mensaje normal",
            "tipo_respuesta": "texto",
        },
    )

    assert response.status_code == 200
    assert update_calls
    assert update_calls[-1][0] == ("573001112233", "ia_chat", "asesor")
