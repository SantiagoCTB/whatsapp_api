import os
import sys
from pathlib import Path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault("INIT_DB_ON_START", "0")

from app import create_app
from routes import tenant_admin_routes
from services.tenants import TenantInfo


def test_admin_tenants_dashboard_handles_nullable_nested_metadata(monkeypatch):
    app = create_app()

    selected_tenant = TenantInfo(
        tenant_key="acme",
        name="Acme",
        db_name="acme_db",
        db_host="localhost",
        db_port=3306,
        db_user="user",
        db_password="pass",
        metadata={
            "instagram_account": None,
            "page_selection": None,
            "whatsapp_business": None,
        },
    )

    monkeypatch.setattr(tenant_admin_routes.tenants, "list_tenants", lambda force_reload=True: [selected_tenant])
    monkeypatch.setattr(tenant_admin_routes.tenants, "get_tenant", lambda key: selected_tenant if key == "acme" else None)
    monkeypatch.setattr(tenant_admin_routes.tenants, "get_tenant_roles", lambda _tenant: [])
    monkeypatch.setattr(tenant_admin_routes.tenants, "list_tenant_users", lambda _tenant: [])
    monkeypatch.setattr(
        tenant_admin_routes.tenants,
        "get_tenant_env",
        lambda _tenant: {
            "WABA_ID": "waba-1",
            "BUSINESS_ID": "biz-1",
            "PHONE_NUMBER_ID": "phone-1",
            "META_TOKEN": "token",
        },
    )

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess["roles"] = ["superadmin"]

    response = client.get("/admin/tenants/?tenant=acme")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Cuenta Instagram:" in html
