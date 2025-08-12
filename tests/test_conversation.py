import pytest
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def flask_client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test")

    monkeypatch.setattr("services.db.init_db", lambda: None)

    rules = {
        ("menu_principal", "iniciar"): ("Bienvenido", "step1", "texto", None, None),
        ("step1", "next"): ("Paso intermedio", "step2", "texto", None, None),
        ("step2", "salir"): ("Fin", "", "texto", None, None),
    }
    processed = set()

    class DummyCursor:
        def __init__(self):
            self.last = None

        def execute(self, query, params=None):
            if "mensajes_procesados" in query:
                if query.strip().upper().startswith("SELECT"):
                    self.last = (1,) if params[0] in processed else None
                elif query.strip().upper().startswith("INSERT"):
                    processed.add(params[0])
                    self.last = None
            elif "FROM reglas" in query:
                self.last = rules.get(params)
            else:
                self.last = None

        def fetchone(self):
            return self.last

        def close(self):
            pass

    class DummyConn:
        def cursor(self):
            return DummyCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("services.db.get_connection", lambda: DummyConn())

    state = {}

    def fake_update(numero, step):
        state[numero] = step

    def fake_get(numero):
        step = state.get(numero)
        return (step, None) if step is not None else None

    def fake_delete(numero):
        state.pop(numero, None)

    monkeypatch.setattr("services.db.update_chat_state", fake_update)
    monkeypatch.setattr("services.db.get_chat_state", fake_get)
    monkeypatch.setattr("services.db.delete_chat_state", fake_delete)
    monkeypatch.setattr("services.db.guardar_mensaje", lambda *a, **k: None)

    sent = []

    def fake_send(numero, mensaje, tipo_respuesta=None, opciones=None, tipo=None):
        sent.append((numero, mensaje))

    monkeypatch.setattr("services.whatsapp_api.enviar_mensaje", fake_send)

    from app import app

    app.config.update({"TESTING": True})

    with app.test_client() as client:
        from routes.webhook import user_steps
        user_steps.clear()
        yield client, sent, state, user_steps
        user_steps.clear()


def build_payload(number, text, msg_id):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": number,
                                    "id": msg_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }


def test_conversation_flow(flask_client):
    client, sent, state, user_steps = flask_client

    def send(text, msg_id):
        payload = build_payload("111", text, msg_id)
        client.post("/webhook", json=payload)

    send("hola", "1")
    assert user_steps.get("111") == "step1"
    assert state.get("111") == "step1"

    send("next", "2")
    assert user_steps.get("111") == "step2"

    send("reiniciar", "3")
    assert user_steps.get("111") == "step1"

    send("next", "4")
    assert user_steps.get("111") == "step2"

    send("salir", "5")
    assert user_steps.get("111") == ""
