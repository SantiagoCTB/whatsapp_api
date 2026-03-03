import os
import sys
from datetime import date
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("INIT_DB_ON_START", "0")

from app import create_app
from routes import auth_routes, tenant_admin_routes
from services import tenants


class _Cursor:
    def __init__(self, row):
        self._row = row

    def execute(self, _query, _params):
        return None

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self._row = row

    def cursor(self):
        return _Cursor(self._row)

    def close(self):
        return None


def _tenant(subscription=None):
    return tenants.TenantInfo(
        tenant_key="acme",
        name="Acme",
        db_name="acme_db",
        db_host="localhost",
        db_port=3306,
        db_user="root",
        db_password="secret",
        metadata={"subscription": subscription or {}},
    )


def test_is_tenant_subscription_active_validates_paid_until():
    active = _tenant({"paid_until": "2999-12-31"})
    expired = _tenant({"paid_until": "2000-01-01"})

    assert tenants.is_tenant_subscription_active(active, reference_date=date(2026, 1, 1))
    assert not tenants.is_tenant_subscription_active(expired, reference_date=date(2026, 1, 1))


def test_ensure_monthly_counter_current_resets_counter(monkeypatch):
    stale = _tenant({"billing_cycle": "2025-01", "monthly_counter": 99, "paid_until": "2999-01-01"})

    def _fake_update(_tenant_key, updates):
        return _tenant(updates)

    monkeypatch.setattr(tenants, "update_tenant_subscription", _fake_update)

    result = tenants.ensure_monthly_counter_current(stale, reference_date=date(2026, 2, 3))

    assert result["billing_cycle"] == "2026-02"
    assert result["monthly_counter"] == 0


def test_login_blocks_non_superadmin_when_membership_expired(monkeypatch):
    app = create_app()

    monkeypatch.setattr(auth_routes, "get_connection", lambda allow_tenant_context=True: _Connection((7, "operador", "x")))
    monkeypatch.setattr(auth_routes, "_verify_password", lambda stored, plain: True)
    monkeypatch.setattr(auth_routes, "get_roles_by_user", lambda _uid, allow_tenant_context=True: ["agente"])
    monkeypatch.setattr(tenants, "get_current_tenant", lambda: _tenant({"paid_until": "2000-01-01"}))
    monkeypatch.setattr(tenants, "is_tenant_subscription_active", lambda tenant: False)

    client = app.test_client()
    response = client.post("/login", data={"username": "operador", "password": "12345678"})

    assert response.status_code == 403
    assert "membresía está vencida" in response.get_data(as_text=True)


def test_superadmin_can_update_subscription_from_admin_panel(monkeypatch):
    app = create_app()

    tenant = _tenant({"paid_until": None, "monthly_counter": 0, "billing_cycle": "2026-01"})
    captured = {}

    monkeypatch.setattr(tenant_admin_routes.tenants, "get_tenant", lambda key, force_reload=False: tenant if key == "acme" else None)
    monkeypatch.setattr(
        tenant_admin_routes.tenants,
        "update_tenant_subscription",
        lambda tenant_key, payload: captured.update({"tenant_key": tenant_key, "payload": payload}),
    )

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "superadmin"
        sess["roles"] = ["superadmin"]

    response = client.post(
        "/admin/tenants/acme/subscription",
        data={"paid_until": "2030-12-31", "monthly_limit": "1500"},
    )

    assert response.status_code == 302
    assert captured["tenant_key"] == "acme"
    assert captured["payload"]["paid_until"] == "2030-12-31"
    assert captured["payload"]["monthly_limit"] == 1500


def test_expired_membership_blocks_requests_with_legacy_admin_role(monkeypatch):
    app = create_app()

    monkeypatch.setattr(tenants, "get_current_tenant", lambda: _tenant({"paid_until": "2000-01-01"}))
    monkeypatch.setattr(tenants, "is_tenant_subscription_active", lambda tenant: False)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["roles"] = []
        sess["rol"] = "admin"

    response = client.get("/")

    assert response.status_code == 403
    assert "Membresía vencida" in response.get_data(as_text=True)
