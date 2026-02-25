from pathlib import Path
from types import SimpleNamespace
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import configuracion


def _admin_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user"] = "admin"
        session["roles"] = ["admin"]
    return client


def test_whatsapp_accounts_returns_client_and_owned(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"META_TOKEN": "token", "BUSINESS_ID": "biz-123"},
    )

    def fake_graph_get(path, access_token, params=None):
        assert access_token == "token"
        if path.endswith("client_whatsapp_business_accounts"):
            return {"ok": True, "data": {"data": [{"id": "waba-client-1"}]}}
        if path.endswith("owned_whatsapp_business_accounts"):
            return {"ok": True, "data": {"data": [{"id": "waba-owned-1"}]}}
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(configuracion, "_graph_get", fake_graph_get)

    client = _admin_client(app)
    response = client.get("/configuracion/whatsapp/accounts")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["business_id"] == "biz-123"
    assert payload["client_accounts"][0]["id"] == "waba-client-1"
    assert payload["owned_accounts"][0]["id"] == "waba-owned-1"


def test_whatsapp_businesses_calls_me_businesses(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"META_TOKEN": "token", "BUSINESS_ID": "biz-123"},
    )

    calls = {}

    def fake_graph_get(path, access_token, params=None):
        calls["path"] = path
        calls["access_token"] = access_token
        calls["params"] = params
        return {"ok": True, "data": {"data": [{"id": "biz-1", "name": "Negocio ACME"}]}}

    monkeypatch.setattr(configuracion, "_graph_get", fake_graph_get)

    client = _admin_client(app)
    response = client.get("/configuracion/whatsapp/businesses")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert calls["path"] == "me/businesses"
    assert calls["access_token"] == "token"
    assert payload["businesses"][0]["id"] == "biz-1"

def test_whatsapp_message_templates_uses_tenant_waba(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"META_TOKEN": "token", "WABA_ID": "waba-123"},
    )

    calls = {}

    def fake_graph_get(path, access_token, params=None):
        calls["path"] = path
        calls["params"] = params
        return {"ok": True, "data": {"data": [{"name": "welcome_template"}]}}

    monkeypatch.setattr(configuracion, "_graph_get", fake_graph_get)

    client = _admin_client(app)
    response = client.get("/configuracion/whatsapp/message-templates")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert calls["path"] == "waba-123/message_templates"
    assert payload["templates"][0]["name"] == "welcome_template"


def test_whatsapp_subscribe_app_calls_graph_post(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"META_TOKEN": "token", "WABA_ID": "waba-123"},
    )

    calls = {}

    def fake_graph_post(path, access_token, data=None):
        calls["path"] = path
        calls["access_token"] = access_token
        calls["data"] = data
        return {"ok": True, "data": {"success": True}}

    monkeypatch.setattr(configuracion, "_graph_post", fake_graph_post)

    client = _admin_client(app)
    response = client.post("/configuracion/whatsapp/subscribe-app", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert calls["path"] == "waba-123/subscribed_apps"
    assert calls["access_token"] == "token"


def test_whatsapp_phone_number_action_register(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"META_TOKEN": "token", "PHONE_NUMBER_ID": "123456"},
    )

    calls = {}

    def fake_graph_post(path, access_token, data=None):
        calls["path"] = path
        calls["access_token"] = access_token
        calls["data"] = data
        return {"ok": True, "data": {"success": True}}

    monkeypatch.setattr(configuracion, "_graph_post", fake_graph_post)

    client = _admin_client(app)
    response = client.post("/configuracion/whatsapp/phone-number-action", json={"action": "register"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["action"] == "register"
    assert calls["path"] == "123456/register"
    assert calls["access_token"] == "token"
    assert calls["data"] == {"messaging_product": "whatsapp"}


def test_whatsapp_phone_number_action_deregister_requires_action(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"LONG_LIVED_TOKEN": "token", "PHONE_NUMBER_ID": "123456"},
    )

    client = _admin_client(app)
    response = client.post("/configuracion/whatsapp/phone-number-action", json={"action": "invalid"})

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert "Acción inválida" in payload["error"]


def test_whatsapp_phone_number_action_register_includes_pin(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {"META_TOKEN": "token", "PHONE_NUMBER_ID": "123456"},
    )

    calls = {}

    def fake_graph_post(path, access_token, data=None):
        calls["path"] = path
        calls["access_token"] = access_token
        calls["data"] = data
        return {"ok": True, "data": {"success": True}}

    monkeypatch.setattr(configuracion, "_graph_post", fake_graph_post)

    client = _admin_client(app)
    response = client.post(
        "/configuracion/whatsapp/phone-number-action",
        json={"action": "register", "pin": "123456"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["action"] == "register"
    assert calls["path"] == "123456/register"
    assert calls["access_token"] == "token"
    assert calls["data"] == {"messaging_product": "whatsapp", "pin": "123456"}


def test_messenger_pages_accepts_page_token_in_user_token_field(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion.tenants,
        "get_tenant_env",
        lambda _tenant: {},
    )

    saved = {}

    def fake_update_env(tenant_key, env_updates):
        saved["tenant_key"] = tenant_key
        saved["env_updates"] = env_updates

    monkeypatch.setattr(configuracion.tenants, "update_tenant_env", fake_update_env)
    monkeypatch.setattr(
        configuracion,
        "_fetch_page_accounts",
        lambda _token: {"ok": False, "error": "No se pudieron obtener las páginas."},
    )
    monkeypatch.setattr(
        configuracion,
        "_fetch_page_from_token",
        lambda token: {
            "ok": True,
            "page": {"id": "page-123", "name": "Mi Página", "access_token": token},
        },
    )

    client = _admin_client(app)
    response = client.post(
        "/configuracion/messenger/pages",
        json={"platform": "messenger", "user_access_token": "EAAB-manual-page-token"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["pages"] == [{"id": "page-123", "name": "Mi Página"}]
    assert saved["tenant_key"] == "acme"
    assert saved["env_updates"]["MESSENGER_TOKEN"] == "EAAB-manual-page-token"
