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
