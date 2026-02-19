from pathlib import Path
import sys

from flask import Flask


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import plantillas_routes


def _app_with_login():
    app = Flask(__name__)
    app.secret_key = "test"
    app.register_blueprint(plantillas_routes.plantillas_bp)

    @app.before_request
    def _fake_login():
        from flask import session

        session["user"] = "tester"

    return app


def test_credentials_status_ready_without_waba_id(monkeypatch):
    app = _app_with_login()

    monkeypatch.setattr(
        plantillas_routes.tenants,
        "get_current_tenant_env",
        lambda: {"META_TOKEN": "token", "PHONE_NUMBER_ID": "phone", "WABA_ID": ""},
    )
    monkeypatch.setattr(plantillas_routes, "_resolve_waba_id", lambda *_: "resolved-waba")

    with app.test_client() as client:
        response = client.get("/api/plantillas/credentials")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["ready"] is True
    assert payload["configured"]["WABA_ID"] is False
    assert payload["warnings"]


def test_credentials_status_missing_required_values(monkeypatch):
    app = _app_with_login()

    monkeypatch.setattr(
        plantillas_routes.tenants,
        "get_current_tenant_env",
        lambda: {"META_TOKEN": "", "PHONE_NUMBER_ID": "", "WABA_ID": ""},
    )

    with app.test_client() as client:
        response = client.get("/api/plantillas/credentials")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ready"] is False
    assert "META_TOKEN" in payload["missing"]
    assert "PHONE_NUMBER_ID" in payload["missing"]


def test_create_template_resolves_waba_id(monkeypatch):
    app = _app_with_login()

    monkeypatch.setattr(
        plantillas_routes.tenants,
        "get_current_tenant_env",
        lambda: {"META_TOKEN": "token", "PHONE_NUMBER_ID": "phone", "WABA_ID": ""},
    )
    monkeypatch.setattr(plantillas_routes, "_resolve_waba_id", lambda *_: "resolved-waba")
    monkeypatch.setattr(
        plantillas_routes,
        "build_template_create_payload",
        lambda _: {
            "name": "demo_template",
            "language": "es_CO",
            "category": "UTILITY",
            "parameter_format": "POSITIONAL",
            "components": [{"type": "BODY", "text": "Hola"}],
        },
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"id": "123"}

    def _fake_post(url, params=None, json=None, timeout=20):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(plantillas_routes.requests, "post", _fake_post)

    with app.test_client() as client:
        response = client.post("/api/plantillas", json={})

    assert response.status_code == 200
    assert captured["url"].endswith("/resolved-waba/message_templates")



def test_preview_send_flow_payload(monkeypatch):
    app = _app_with_login()

    monkeypatch.setattr(
        plantillas_routes,
        "build_flow_send_payload",
        lambda payload: {"type": "interactive", "interactive": {"type": "flow"}, "input": payload},
    )

    with app.test_client() as client:
        response = client.post("/api/plantillas/preview-send-flow", json={"to": "57", "flow_cta": "Abrir", "flow_id": "1"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["payload"]["interactive"]["type"] == "flow"


def test_send_flow_uses_messages_endpoint(monkeypatch):
    app = _app_with_login()

    monkeypatch.setattr(
        plantillas_routes.tenants,
        "get_current_tenant_env",
        lambda: {"META_TOKEN": "token", "PHONE_NUMBER_ID": "phone", "WABA_ID": ""},
    )
    monkeypatch.setattr(
        plantillas_routes,
        "build_flow_send_payload",
        lambda _: {
            "messaging_product": "whatsapp",
            "to": "573001112233",
            "type": "interactive",
            "interactive": {"type": "flow"},
        },
    )

    captured = {}

    class _FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"messages": [{"id": "wamid.1"}]}

    def _fake_post(url, params=None, json=None, timeout=20):
        captured["url"] = url
        captured["params"] = params
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(plantillas_routes.requests, "post", _fake_post)

    with app.test_client() as client:
        response = client.post("/api/plantillas/send-flow", json={"to": "57"})

    assert response.status_code == 200
    assert captured["url"].endswith("/phone/messages")
    assert captured["json"]["interactive"]["type"] == "flow"
