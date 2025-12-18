import contextlib

from services import tenants
from services import whatsapp_api


@contextlib.contextmanager
def _temp_env(env):
    original = tenants.get_current_tenant_env()
    tenants.set_current_tenant_env(env)
    try:
        yield
    finally:
        tenants.set_current_tenant_env(original)


def test_enviar_mensaje_without_credentials_returns_error(monkeypatch):
    monkeypatch.setattr(whatsapp_api, "stop_typing_feedback", lambda *_, **__: None)

    empty_env = {"META_TOKEN": None, "PHONE_NUMBER_ID": None}
    with _temp_env(empty_env):
        ok, reason = whatsapp_api.enviar_mensaje(
            "12345", "hola", return_error=True
        )

    assert ok is False
    assert "Faltan credenciales" in reason
