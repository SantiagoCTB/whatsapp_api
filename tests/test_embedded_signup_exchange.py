from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import configuracion


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
