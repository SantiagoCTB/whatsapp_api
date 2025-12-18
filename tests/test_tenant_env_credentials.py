from pathlib import Path
import sys

import pytest


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services import tenants


def _build_tenant(metadata=None):
    return tenants.TenantInfo(
        tenant_key="acme",
        name="ACME",
        db_name="acme_db",
        db_host="localhost",
        db_port=3306,
        db_user="root",
        db_password="pwd",
        metadata=metadata or {},
    )


def test_tenant_env_uses_metadata_over_globals(monkeypatch):
    monkeypatch.setattr(tenants.Config, "META_TOKEN", "global-token")
    monkeypatch.setattr(tenants.Config, "PHONE_NUMBER_ID", "global-phone")

    tenant = _build_tenant(
        metadata={
            "env": {"META_TOKEN": "tenant-token", "PHONE_NUMBER_ID": "tenant-phone"}
        }
    )

    env = tenants.get_tenant_env(tenant)

    assert env["META_TOKEN"] == "tenant-token"
    assert env["PHONE_NUMBER_ID"] == "tenant-phone"


def test_tenant_env_does_not_inherit_globals_for_credentials(monkeypatch):
    monkeypatch.setattr(tenants.Config, "META_TOKEN", "global-token")
    monkeypatch.setattr(tenants.Config, "PHONE_NUMBER_ID", "global-phone")

    tenant = _build_tenant(metadata={})

    env = tenants.get_tenant_env(tenant)

    assert env["META_TOKEN"] is None
    assert env["PHONE_NUMBER_ID"] is None


def test_legacy_env_keeps_global_credentials(monkeypatch):
    monkeypatch.setattr(tenants.Config, "META_TOKEN", "global-token")
    monkeypatch.setattr(tenants.Config, "PHONE_NUMBER_ID", "global-phone")

    env = tenants.get_tenant_env(None)

    assert env["META_TOKEN"] == "global-token"
    assert env["PHONE_NUMBER_ID"] == "global-phone"
