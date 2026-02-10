from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from routes import configuracion


def test_embedded_redirect_uri_uses_explicit_setting(monkeypatch):
    monkeypatch.setattr(configuracion.Config, "EMBEDDED_SIGNUP_REDIRECT_URI", "https://explicit.example/callback")
    monkeypatch.setattr(configuracion.Config, "SIGNUP_FACEBOOK", "https://facebook.com/dialog/oauth?redirect_uri=https%3A%2F%2Fquery.example%2Fcb")
    monkeypatch.setattr(configuracion.Config, "PUBLIC_BASE_URL", "https://base.example")

    resolved = configuracion._resolve_embedded_signup_redirect_uri("https://fallback.example/configuracion/signup")

    assert resolved == "https://explicit.example/callback"


def test_embedded_redirect_uri_falls_back_to_signup_url_redirect(monkeypatch):
    monkeypatch.setattr(configuracion.Config, "EMBEDDED_SIGNUP_REDIRECT_URI", "")
    monkeypatch.setattr(
        configuracion.Config,
        "SIGNUP_FACEBOOK",
        "https://www.facebook.com/v24.0/dialog/oauth?client_id=123&redirect_uri=https%3A%2F%2Fapp.example%2Fconfiguracion%2Fsignup&state=abc",
    )
    monkeypatch.setattr(configuracion.Config, "PUBLIC_BASE_URL", "https://base.example")

    resolved = configuracion._resolve_embedded_signup_redirect_uri("https://fallback.example/configuracion/signup")

    assert resolved == "https://app.example/configuracion/signup"


def test_embedded_redirect_uri_uses_public_base_when_signup_url_has_no_redirect(monkeypatch):
    monkeypatch.setattr(configuracion.Config, "EMBEDDED_SIGNUP_REDIRECT_URI", "")
    monkeypatch.setattr(configuracion.Config, "SIGNUP_FACEBOOK", "https://www.facebook.com/v24.0/dialog/oauth?client_id=123")
    monkeypatch.setattr(configuracion.Config, "PUBLIC_BASE_URL", "https://base.example/")

    resolved = configuracion._resolve_embedded_signup_redirect_uri("https://fallback.example/configuracion/signup")

    assert resolved == "https://base.example/configuracion/signup"
