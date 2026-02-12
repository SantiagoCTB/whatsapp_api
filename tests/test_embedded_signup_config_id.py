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


def test_whatsapp_embedded_config_id_takes_priority(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})
    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(configuracion.tenants, "get_active_tenant_key", lambda: "acme")
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})
    monkeypatch.setattr(configuracion, "_normalize_page_selection", lambda _metadata: {})
    monkeypatch.setattr(configuracion, "_fetch_instagram_backfill_counts", lambda _tenant: (0, 0))
    monkeypatch.setattr(configuracion.Config, "WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID", "wa-config-123")
    monkeypatch.setattr(configuracion.Config, "SIGNUP_FACEBOOK", "legacy-config-999")

    client = _admin_client(app)
    response = client.get("/configuracion/signup")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-config-code="wa-config-123"' in html


def test_whatsapp_embedded_config_id_falls_back_to_signup_facebook(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})
    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(configuracion.tenants, "get_active_tenant_key", lambda: "acme")
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})
    monkeypatch.setattr(configuracion, "_normalize_page_selection", lambda _metadata: {})
    monkeypatch.setattr(configuracion, "_fetch_instagram_backfill_counts", lambda _tenant: (0, 0))
    monkeypatch.setattr(configuracion.Config, "WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID", "")
    monkeypatch.setattr(configuracion.Config, "SIGNUP_FACEBOOK", "legacy-config-999")

    client = _admin_client(app)
    response = client.get("/configuracion/signup")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'data-config-code="legacy-config-999"' in html
