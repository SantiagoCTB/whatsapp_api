from pathlib import Path
from types import SimpleNamespace
import sys
import time

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


def test_messenger_signup_uses_redirect_uri_fallbacks_when_uri_differs(monkeypatch):
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
            "/configuracion/messenger/signup",
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


def test_configuracion_signup_ignores_non_instagram_oauth_codes(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    called = {"instagram_handler": False}

    def fake_handle(_code, _redirect_uri):
        called["instagram_handler"] = True
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "meta-embedded-code", "state": "acme"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert called["instagram_handler"] is False


def test_configuracion_signup_processes_instagram_oauth_code_when_marked(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    calls = {}

    def fake_handle(code, redirect_uri):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)
    monkeypatch.setattr(configuracion, "_resolve_instagram_redirect_uri", lambda _fallback: "https://app.example/configuracion/signup")

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "ig-code", "oauth_provider": "instagram"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert calls["code"] == "ig-code"
    assert calls["redirect_uri"] == "https://app.example/configuracion/signup"


def test_configuracion_signup_processes_instagram_oauth_code_when_pending_in_session(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    calls = {}

    def fake_handle(code, redirect_uri):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)
    monkeypatch.setattr(configuracion, "_resolve_instagram_redirect_uri", lambda _fallback: "https://app.example/configuracion/signup")

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]
            session["instagram_oauth_pending_at"] = time.time()

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "ig-code-without-marker"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert calls["code"] == "ig-code-without-marker"
    assert calls["redirect_uri"] == "https://app.example/configuracion/signup"



def test_fetch_instagram_user_uses_graph_instagram_me_without_version(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        captured["headers"] = headers or {}
        return _Response(status_code=200, payload={"id": "1784", "username": "acme"})

    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._fetch_instagram_user("ig-user-token")

    assert response["ok"] is True
    assert captured["url"] == "https://graph.instagram.com/me"
    assert captured["params"]["access_token"] == "ig-user-token"
    assert captured["params"]["fields"] == "user_id,id,username,account_type"




def test_fetch_instagram_user_falls_back_to_facebook_me_accounts(monkeypatch):
    calls = {"count": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return _Response(
                status_code=400,
                payload={"error": {"code": 100, "message": "Unsupported request - method type: get", "type": "IGApiException"}},
            )

        assert url == f"https://graph.facebook.com/{configuracion.Config.FACEBOOK_GRAPH_API_VERSION}/me/accounts"
        return _Response(
            status_code=200,
            payload={
                "data": [
                    {
                        "id": "123456789",
                        "name": "Acme Page",
                        "instagram_business_account": {"id": "178414", "username": "acme.ig"},
                    }
                ]
            },
        )

    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._fetch_instagram_user("fb-user-token")

    assert response["ok"] is True
    assert response["account"]["id"] == "178414"
    assert response["account"]["username"] == "acme.ig"
    assert response["account"]["user_id"] == "123456789"


def test_fetch_instagram_user_surfaces_error_payload(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _Response(
            status_code=400,
            payload={"error": {"message": "Unsupported get request."}},
        )

    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._fetch_instagram_user("bad-token")

    assert response["ok"] is False
    assert response["error"] == "No se pudo obtener la cuenta de Instagram."
    assert response["details"]["message"] == "Unsupported get request."


def test_configuracion_signup_ignores_non_instagram_oauth_codes(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    called = {"instagram_handler": False}

    def fake_handle(_code, _redirect_uri):
        called["instagram_handler"] = True
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "meta-embedded-code", "state": "acme"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert called["instagram_handler"] is False


def test_configuracion_signup_processes_instagram_oauth_code_when_marked(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    calls = {}

    def fake_handle(code, redirect_uri):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)
    monkeypatch.setattr(configuracion, "_resolve_instagram_redirect_uri", lambda _fallback: "https://app.example/configuracion/signup")

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "ig-code", "oauth_provider": "instagram"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert calls["code"] == "ig-code"
    assert calls["redirect_uri"] == "https://app.example/configuracion/signup"


def test_configuracion_signup_processes_instagram_oauth_code_when_pending_in_session(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    calls = {}

    def fake_handle(code, redirect_uri):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)
    monkeypatch.setattr(configuracion, "_resolve_instagram_redirect_uri", lambda _fallback: "https://app.example/configuracion/signup")

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]
            session["instagram_oauth_pending_at"] = time.time()

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "ig-code-without-marker"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert calls["code"] == "ig-code-without-marker"
    assert calls["redirect_uri"] == "https://app.example/configuracion/signup"



def test_fetch_instagram_user_uses_graph_instagram_me_without_version(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params or {}
        captured["headers"] = headers or {}
        return _Response(status_code=200, payload={"id": "1784", "username": "acme"})

    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._fetch_instagram_user("ig-user-token")

    assert response["ok"] is True
    assert captured["url"] == "https://graph.instagram.com/me"
    assert captured["params"]["access_token"] == "ig-user-token"
    assert captured["params"]["fields"] == "user_id,id,username,account_type"


def test_fetch_instagram_user_surfaces_error_payload(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _Response(
            status_code=400,
            payload={"error": {"message": "Unsupported get request."}},
        )

    monkeypatch.setattr(configuracion.requests, "get", fake_get)

    response = configuracion._fetch_instagram_user("bad-token")

    assert response["ok"] is False
    assert response["error"] == "No se pudo obtener la cuenta de Instagram."
    assert response["details"]["message"] == "Unsupported get request."


def test_configuracion_signup_ignores_non_instagram_oauth_codes(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    called = {"instagram_handler": False}

    def fake_handle(_code, _redirect_uri):
        called["instagram_handler"] = True
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "meta-embedded-code", "state": "acme"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert called["instagram_handler"] is False


def test_configuracion_signup_processes_instagram_oauth_code_when_marked(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    calls = {}

    def fake_handle(code, redirect_uri):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)
    monkeypatch.setattr(configuracion, "_resolve_instagram_redirect_uri", lambda _fallback: "https://app.example/configuracion/signup")

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "ig-code", "oauth_provider": "instagram"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert calls["code"] == "ig-code"
    assert calls["redirect_uri"] == "https://app.example/configuracion/signup"


def test_configuracion_signup_processes_instagram_oauth_code_when_pending_in_session(monkeypatch):
    app = create_app()
    app.config["TESTING"] = True

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: SimpleNamespace(tenant_key="acme", metadata={}))
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    calls = {}

    def fake_handle(code, redirect_uri):
        calls["code"] = code
        calls["redirect_uri"] = redirect_uri
        return {"ok": True, "access_token": "token"}

    monkeypatch.setattr(configuracion, "_handle_instagram_oauth_code", fake_handle)
    monkeypatch.setattr(configuracion, "_resolve_instagram_redirect_uri", lambda _fallback: "https://app.example/configuracion/signup")

    with app.test_client() as client:
        with client.session_transaction() as session:
            session["user"] = "admin"
            session["roles"] = ["admin"]
            session["instagram_oauth_pending_at"] = time.time()

        response = client.get(
            "/configuracion/signup",
            query_string={"code": "ig-code-without-marker"},
        )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/configuracion/signup")
    assert calls["code"] == "ig-code-without-marker"
    assert calls["redirect_uri"] == "https://app.example/configuracion/signup"

def test_build_redirect_uri_attempts_prioritizes_whatsapp_and_root_domain(monkeypatch):
    monkeypatch.setattr(configuracion.Config, "WHATSAPP_OAUTH_REDIRECT_URI", "https://app.whapco.site/configuracion/signup")

    attempts = configuracion._build_redirect_uri_attempts(
        "https://app.whapco.site/configuracion/signup",
        None,
    )

    assert attempts[0] == ""
    assert "https://app.whapco.site/configuracion/signup" in attempts
    assert "https://app.whapco.site" in attempts
    assert "https://app.whapco.site/" in attempts


def test_build_redirect_uri_attempts_uses_configured_whatsapp_when_primary_differs(monkeypatch):
    monkeypatch.setattr(configuracion.Config, "WHATSAPP_OAUTH_REDIRECT_URI", "https://app.whapco.site/configuracion/signup")

    attempts = configuracion._build_redirect_uri_attempts(
        "https://provided.example/configuracion/signup",
        "https://fallback.example/configuracion/signup",
    )

    assert attempts[0] == ""
    assert attempts[1] == "https://provided.example/configuracion/signup"
    assert attempts[2] == "https://app.whapco.site/configuracion/signup"
    assert "https://app.whapco.site" in attempts


def test_exchange_instagram_code_accepts_data_wrapped_response(monkeypatch):
    def fake_post(url, data=None, timeout=None):
        return _Response(
            status_code=200,
            payload={"data": [{"access_token": "wrapped-token", "user_id": "1020"}]},
        )

    monkeypatch.setattr(configuracion.Config, "INSTAGRAM_APP_ID", "ig-app")
    monkeypatch.setattr(configuracion.Config, "INSTAGRAM_APP_SECRET", "ig-secret")
    monkeypatch.setattr(configuracion.Config, "INSTAGRAM_OAUTH_TOKEN_URL", "https://api.instagram.com/oauth/access_token")
    monkeypatch.setattr(configuracion.requests, "post", fake_post)
    monkeypatch.setattr(
        configuracion.requests,
        "get",
        lambda *_args, **_kwargs: _Response(status_code=400, payload={"error": {"message": "skip long-lived"}}),
    )

    response = configuracion._exchange_instagram_code_for_token("abc", "https://app.example/configuracion/signup")

    assert response["ok"] is True
    assert response["access_token"] == "wrapped-token"


def test_handle_instagram_oauth_uses_user_id_as_account_id_when_id_is_missing(monkeypatch):
    tenant = SimpleNamespace(tenant_key="acme", metadata={})
    captured = {}

    monkeypatch.setattr(configuracion, "_resolve_signup_tenant", lambda: tenant)
    monkeypatch.setattr(
        configuracion,
        "_exchange_instagram_code_for_token",
        lambda _code, _uri: {"ok": True, "access_token": "ig-token", "is_long_lived": True},
    )
    monkeypatch.setattr(
        configuracion,
        "_fetch_instagram_user",
        lambda _token: {"ok": True, "account": {"user_id": "178414", "username": "acme.ig"}},
    )
    monkeypatch.setattr(configuracion.tenants, "get_tenant_env", lambda _tenant: {})

    def fake_update_env(_tenant_key, env_updates):
        captured["env_updates"] = env_updates

    monkeypatch.setattr(configuracion.tenants, "update_tenant_env", fake_update_env)
    monkeypatch.setattr(configuracion.tenants, "update_tenant_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(configuracion.tenants, "trigger_page_backfill_for_platform", lambda *_args, **_kwargs: None)

    response = configuracion._handle_instagram_oauth_code("abc", "https://app.example/configuracion/signup")

    assert response["ok"] is True
    assert captured["env_updates"]["INSTAGRAM_ACCOUNT_ID"] == "178414"
    assert captured["env_updates"]["INSTAGRAM_PAGE_ID"] == "178414"
