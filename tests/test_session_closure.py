import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import chat_routes
from routes import webhook as webhook_module


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_finalizar_chat_envia_notificacion(client, monkeypatch):
    acciones = []

    def fake_clear(numero):
        acciones.append(("clear", numero))

    def fake_notify(numero, origin="manual"):
        acciones.append(("notify", numero, origin))
        return True

    def fake_update(numero, step, estado=None):
        acciones.append(("update", numero, step, estado))

    monkeypatch.setattr(
        chat_routes,
        "_load_chat_state_definitions",
        lambda include_hidden=True: ([], {"inactivo"}),
    )
    monkeypatch.setattr(chat_routes, "get_chat_state", lambda _: (None, None, None))
    monkeypatch.setattr(chat_routes, "update_chat_state", fake_update)
    monkeypatch.setattr(chat_routes, "clear_chat_runtime_state", fake_clear)
    monkeypatch.setattr(chat_routes, "notify_session_closed", fake_notify)

    with client.session_transaction() as sess:
        sess["user"] = "admin"

    response = client.post("/finalizar_chat", json={"numero": "5215550000"})

    assert response.status_code == 200
    assert response.json == {"status": "ok"}
    assert any(action[0] == "update" for action in acciones)
    assert ("clear", "5215550000") in acciones
    assert ("notify", "5215550000", "manual") in acciones


def test_handle_text_message_notifica_timeout(monkeypatch):
    numero = "5215551111"
    now = datetime.utcnow()
    last_activity = now - timedelta(seconds=webhook_module.SESSION_TIMEOUT + 5)

    monkeypatch.setattr(
        webhook_module,
        "get_chat_state",
        lambda _: ("menu", last_activity),
    )

    deleted = []
    cleared = []
    notified = []
    saved_messages = []
    steps = []
    processed = []

    monkeypatch.setattr(webhook_module, "delete_chat_state", lambda n: deleted.append(n))
    monkeypatch.setattr(webhook_module, "clear_chat_runtime_state", lambda n: cleared.append(n))
    monkeypatch.setattr(
        webhook_module,
        "notify_session_closed",
        lambda n, origin="timeout": notified.append((n, origin)) or True,
    )
    monkeypatch.setattr(
        webhook_module,
        "guardar_mensaje",
        lambda numero, mensaje, tipo, **kwargs: saved_messages.append(
            (numero, mensaje, tipo, kwargs)
        ),
    )
    monkeypatch.setattr(
        webhook_module,
        "set_user_step",
        lambda n, step, estado="espera_usuario": steps.append((n, step, estado)),
    )
    monkeypatch.setattr(
        webhook_module,
        "process_step_chain",
        lambda n, texto, **kwargs: processed.append((n, texto, kwargs)),
    )
    monkeypatch.setattr(webhook_module, "handle_global_command", lambda *args, **kwargs: False)

    webhook_module.handle_text_message(numero, "Hola", save=True)

    assert deleted == [numero]
    assert cleared == [numero]
    assert notified == [(numero, "timeout")]
    assert saved_messages and saved_messages[0][0] == numero
    assert steps and steps[0][0] == numero
    assert processed[0][1] == "iniciar"
    assert processed[-1][1] == "hola"


def test_handle_text_message_no_timeout_when_reciente(monkeypatch):
    numero = "5215552222"
    last_activity = datetime.utcnow() - timedelta(seconds=5)

    monkeypatch.setattr(
        webhook_module,
        "get_chat_state",
        lambda _: ("menu", last_activity),
    )

    notified = []

    monkeypatch.setattr(webhook_module, "delete_chat_state", lambda n: None)
    monkeypatch.setattr(webhook_module, "clear_chat_runtime_state", lambda n: None)
    monkeypatch.setattr(
        webhook_module,
        "notify_session_closed",
        lambda n, origin="timeout": notified.append((n, origin)) or True,
    )
    monkeypatch.setattr(webhook_module, "guardar_mensaje", lambda *args, **kwargs: None)
    monkeypatch.setattr(webhook_module, "set_user_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(webhook_module, "process_step_chain", lambda *args, **kwargs: None)
    monkeypatch.setattr(webhook_module, "handle_global_command", lambda *args, **kwargs: True)

    webhook_module.handle_text_message(numero, "Hola", save=True)

    assert not notified


def test_handle_text_message_no_timeout_when_step_missing(monkeypatch):
    """No se debe notificar cierre si no hay un paso activo previo."""

    numero = "5215553333"
    last_activity = datetime.utcnow() - timedelta(seconds=webhook_module.SESSION_TIMEOUT + 5)

    monkeypatch.setattr(
        webhook_module,
        "get_chat_state",
        lambda _: (None, last_activity, None),
    )

    notified = []

    monkeypatch.setattr(webhook_module, "delete_chat_state", lambda n: None)
    monkeypatch.setattr(webhook_module, "clear_chat_runtime_state", lambda n: None)
    monkeypatch.setattr(
        webhook_module,
        "notify_session_closed",
        lambda n, origin="timeout": notified.append((n, origin)) or True,
    )
    monkeypatch.setattr(webhook_module, "guardar_mensaje", lambda *args, **kwargs: None)
    monkeypatch.setattr(webhook_module, "set_user_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(webhook_module, "process_step_chain", lambda *args, **kwargs: None)
    monkeypatch.setattr(webhook_module, "handle_global_command", lambda *args, **kwargs: True)

    webhook_module.handle_text_message(numero, "Hola", save=True)

    assert not notified


def test_maybe_close_expired_session_notifies(monkeypatch):
    numero = "5215554444"
    now = datetime.utcnow()
    last_activity = now - timedelta(seconds=15)
    acciones = []

    monkeypatch.setattr(
        chat_routes,
        "delete_chat_state",
        lambda n: acciones.append(("delete", n)),
    )
    monkeypatch.setattr(
        chat_routes,
        "clear_chat_runtime_state",
        lambda n: acciones.append(("clear", n)),
    )
    monkeypatch.setattr(
        chat_routes,
        "notify_session_closed",
        lambda n, origin="timeout": acciones.append(("notify", n, origin)) or True,
    )

    estado = chat_routes._maybe_close_expired_session(
        numero=numero,
        step="menu",
        last_activity=last_activity,
        stored_estado=None,
        timeout_seconds=10,
        now=now,
    )

    assert estado == "inactivo"
    assert ("delete", numero) in acciones
    assert ("clear", numero) in acciones
    assert ("notify", numero, "timeout") in acciones


def test_maybe_close_expired_session_skips_inactive(monkeypatch):
    numero = "5215555555"
    now = datetime.utcnow()
    last_activity = now - timedelta(seconds=15)
    acciones = []

    monkeypatch.setattr(
        chat_routes,
        "delete_chat_state",
        lambda n: acciones.append(("delete", n)),
    )
    monkeypatch.setattr(
        chat_routes,
        "clear_chat_runtime_state",
        lambda n: acciones.append(("clear", n)),
    )
    monkeypatch.setattr(
        chat_routes,
        "notify_session_closed",
        lambda n, origin="timeout": acciones.append(("notify", n, origin)) or True,
    )

    estado = chat_routes._maybe_close_expired_session(
        numero=numero,
        step="menu",
        last_activity=last_activity,
        stored_estado="inactivo",
        timeout_seconds=10,
        now=now,
    )

    assert estado is None
    assert not acciones
