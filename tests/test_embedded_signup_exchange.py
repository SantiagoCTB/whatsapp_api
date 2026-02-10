from pathlib import Path
from types import SimpleNamespace
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import configuracion
from app import create_app


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_exchange_embedded_uses_configured_graph_version(monkeypatch):
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["url"] = url
        calls["data"] = data
        calls["timeout"] = timeout
        return _Response(status_code=200, payload={"access_token": "abc"})

    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_ID", "123")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_GRAPH_API_VERSION", "v24.0")
    monkeypatch.setattr(configuracion.requests, "post", fake_post)

    response = configuracion._exchange_embedded_signup_code_for_token(
        "my-code",
        "https://app.example/configuracion/signup",
    )

    assert response["ok"] is True
    assert calls["url"] == "https://graph.facebook.com/v24.0/oauth/access_token"
    assert calls["data"]["redirect_uri"] == "https://app.example/configuracion/signup"
    assert calls["data"]["code"] == "my-code"


def test_exchange_embedded_omits_redirect_uri_when_empty(monkeypatch):
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["data"] = data
        return _Response(status_code=200, payload={"access_token": "abc"})

    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_ID", "123")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_GRAPH_API_VERSION", "v24.0")
    monkeypatch.setattr(configuracion.requests, "post", fake_post)

    response = configuracion._exchange_embedded_signup_code_for_token("my-code", None)

    assert response["ok"] is True
    assert "redirect_uri" not in calls["data"]


def test_exchange_embedded_falls_back_to_get_when_post_fails(monkeypatch):
    seen_methods = []

    def fake_post(url, data=None, timeout=None):
        seen_methods.append(("post", data.get("redirect_uri")))
        return _Response(
            status_code=400,
            payload={
                "error": {
                    "message": "Error validating verification code. Please make sure your redirect_uri is identical to the one you used in the OAuth dialog request",
                    "error_subcode": 36008,
                }
            },
        )

    def fake_get(url, params=None, timeout=None):
        seen_methods.append(("get", params.get("redirect_uri")))
        return _Response(status_code=200, payload={"access_token": "abc"})

    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_ID", "123")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_GRAPH_API_VERSION", "v24.0")
    monkeypatch.setattr(configuracion.requests, "post", fake_post)
    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._exchange_embedded_signup_code_for_token(
        "my-code",
        "https://first.example/callback",
    )

    assert response["ok"] is True
    assert seen_methods == [
        ("post", "https://first.example/callback"),
        ("get", "https://first.example/callback"),
    ]


def test_embedded_signup_error_message_for_domain_not_allowed():
    message = configuracion._build_embedded_signup_error_message(
        "No se pudo intercambiar.",
        {
            "error": {
                "code": 191,
                "message": "Não é possível carregar a URL",
            }
        },
    )

    assert "dominio" in message.lower() or "dominio" in message.lower().replace("ó", "o")
    assert "oauth" in message.lower()


def test_embedded_signup_error_message_for_redirect_mismatch():
    message = configuracion._build_embedded_signup_error_message(
        "No se pudo intercambiar.",
        {
            "error": {
                "code": 100,
                "error_subcode": 36008,
                "message": "Error validating verification code. Please make sure your redirect_uri is identical",
            }
        },
    )

    assert "redirect_uri" in message.lower()
    assert "sdk" in message.lower()


def test_save_signup_uses_redirect_uri_fallbacks_when_uri_differs(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    tenant = SimpleNamespace(tenant_key="acme", metadata={})
    calls = {}

    def fake_exchange(code, redirect_uri, fallback_uri=None):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        calls["fallback_uri"] = fallback_uri
        return {"ok": True, "access_token": "token-from-code"}

    def fake_update_env(tenant_key, env_updates):
        calls["updated_tenant_key"] = tenant_key
        calls["env_updates"] = env_updates

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(configuracion, "_resolve_embedded_signup_redirect_uri", lambda _fallback: "https://resolved.example/configuracion/signup")
    monkeypatch.setattr(configuracion, "_exchange_embedded_signup_code_with_fallbacks", fake_exchange)
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})
    monkeypatch.setattr(configuracion.tenants, "update_tenant_env", fake_update_env)
    monkeypatch.setattr(configuracion.tenants, "update_tenant_metadata", lambda *_args, **_kwargs: None)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.post(
            "/configuracion/signup",
            json={
                "code": "embedded-code",
                "redirect_uri": "https://provided.example/configuracion/signup",
            },
        )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert calls["code"] == "embedded-code"
    assert calls["redirect_uri"] == "https://provided.example/configuracion/signup"
    assert calls["fallback_uri"] == "https://resolved.example/configuracion/signup"
    assert calls["updated_tenant_key"] == "acme"
    assert calls["env_updates"]["META_TOKEN"] == "token-from-code"
