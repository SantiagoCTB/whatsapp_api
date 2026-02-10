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

    def fake_get(url, params=None, timeout=None):
        calls["url"] = url
        calls["params"] = params
        calls["timeout"] = timeout
        return _Response(status_code=200, payload={"access_token": "abc"})

    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_ID", "123")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_GRAPH_API_VERSION", "v24.0")
    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._exchange_embedded_signup_code_for_token(
        "my-code",
        "https://app.example/configuracion/signup",
    )

    assert response["ok"] is True
    assert calls["url"] == "https://graph.facebook.com/v24.0/oauth/access_token"
    assert calls["params"]["redirect_uri"] == "https://app.example/configuracion/signup"
    assert calls["params"]["code"] == "my-code"


def test_exchange_embedded_omits_redirect_uri_when_empty(monkeypatch):
    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls["params"] = params
        return _Response(status_code=200, payload={"access_token": "abc"})

    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_ID", "123")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_GRAPH_API_VERSION", "v24.0")
    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._exchange_embedded_signup_code_for_token("my-code", None)

    assert response["ok"] is True
    assert "redirect_uri" not in calls["params"]


def test_exchange_embedded_retries_on_redirect_mismatch(monkeypatch):
    seen_redirects = []

    def fake_get(url, params=None, timeout=None):
        seen_redirects.append(params.get("redirect_uri"))
        if len(seen_redirects) == 1:
            return _Response(
                status_code=400,
                payload={
                    "error": {
                        "message": "Error validating verification code. Please make sure your redirect_uri is identical to the one you used in the OAuth dialog request",
                        "error_subcode": 36008,
                    }
                },
            )
        return _Response(status_code=200, payload={"access_token": "abc"})

    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_ID", "123")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_APP_SECRET", "secret")
    monkeypatch.setattr(configuracion.Config, "FACEBOOK_GRAPH_API_VERSION", "v24.0")
    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._exchange_embedded_signup_code_with_fallbacks(
        "my-code",
        "https://first.example/callback",
        "https://second.example/callback",
    )

    assert response["ok"] is True
    assert seen_redirects == [
        "https://first.example/callback",
        "https://second.example/callback",
    ]
