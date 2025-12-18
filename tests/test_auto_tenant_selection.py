import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services import tenants


@pytest.fixture
def sample_tenant():
    return tenants.TenantInfo(
        tenant_key="acme",
        name="Acme",
        db_name="acme_db",
        db_host="localhost",
        db_port=3306,
        db_user="root",
        db_password="secret",
        metadata={},
    )


def test_auto_select_single_tenant_returns_only_entry(monkeypatch, sample_tenant):
    monkeypatch.setattr(tenants, "list_tenants", lambda force_reload=False: [sample_tenant])

    assert tenants.auto_select_single_tenant() is sample_tenant


def test_auto_select_single_tenant_returns_none_when_empty(monkeypatch):
    monkeypatch.setattr(tenants, "list_tenants", lambda force_reload=False: [])

    assert tenants.auto_select_single_tenant() is None


def test_auto_select_single_tenant_returns_none_when_multiple(monkeypatch, sample_tenant):
    other = tenants.TenantInfo(
        tenant_key="beta",
        name="Beta",
        db_name="beta_db",
        db_host="localhost",
        db_port=3306,
        db_user="root",
        db_password="secret",
        metadata={},
    )
    monkeypatch.setattr(
        tenants, "list_tenants", lambda force_reload=False: [sample_tenant, other]
    )

    assert tenants.auto_select_single_tenant() is None
