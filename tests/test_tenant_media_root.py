import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import Config
from services import tenants


@pytest.fixture(autouse=True)
def reset_tenant_context():
    tenants.clear_current_tenant()
    tenants.set_current_tenant_env(None)
    yield
    tenants.clear_current_tenant()
    tenants.set_current_tenant_env(None)


def test_get_media_root_avoids_duplicate_tenant_path(tmp_path, monkeypatch):
    tenant_key = "whapco"
    base_dir = tmp_path / "app"
    static_root = base_dir / "static"
    uploads_root = static_root / "uploads"
    tenant_root = uploads_root / tenant_key

    monkeypatch.setattr(Config, "BASEDIR", str(base_dir))
    monkeypatch.setattr(Config, "MEDIA_ROOT", str(uploads_root))
    monkeypatch.setattr(Config, "DEFAULT_TENANT", tenant_key)

    tenants.set_current_tenant_env({"MEDIA_ROOT": str(tenant_root)})

    result = tenants.get_media_root()

    assert os.path.normpath(result) == os.path.normpath(str(tenant_root))
