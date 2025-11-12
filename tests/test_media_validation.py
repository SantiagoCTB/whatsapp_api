import requests

from services import whatsapp_api


class DummyResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.text = ""
        self._payload = payload or {"messages": [{"id": "wamid.test"}]}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload

    def close(self):
        pass


def _patch_common_dependencies(monkeypatch):
    monkeypatch.setattr(whatsapp_api, "stop_typing_feedback", lambda *_, **__: None)
    monkeypatch.setattr(whatsapp_api, "guardar_mensaje", lambda *_, **__: None)


def test_enviar_mensaje_continues_when_head_fails(monkeypatch):
    _patch_common_dependencies(monkeypatch)

    def fake_head(*_, **__):  # pragma: no cover - exception path
        raise requests.RequestException("boom")

    send_calls = {}

    def fake_post(url, headers=None, json=None):
        send_calls["payload"] = json
        return DummyResponse()

    monkeypatch.setattr(whatsapp_api.requests, "head", fake_head)
    monkeypatch.setattr(whatsapp_api.requests, "post", fake_post)

    ok = whatsapp_api.enviar_mensaje(
        "12345",
        "hola",
        tipo_respuesta="image",
        opciones="https://example.com/image.jpg",
    )

    assert ok is True
    assert send_calls["payload"]["type"] == "image"


def test_enviar_mensaje_uses_get_when_head_not_allowed(monkeypatch):
    _patch_common_dependencies(monkeypatch)

    head_response = DummyResponse(status_code=405)

    def fake_head(*_, **__):
        return head_response

    get_called = {}

    def fake_get(*args, **kwargs):
        get_called["args"] = args
        get_called["kwargs"] = kwargs
        return DummyResponse(status_code=200)

    def fake_post(url, headers=None, json=None):
        return DummyResponse()

    monkeypatch.setattr(whatsapp_api.requests, "head", fake_head)
    monkeypatch.setattr(whatsapp_api.requests, "get", fake_get)
    monkeypatch.setattr(whatsapp_api.requests, "post", fake_post)

    ok = whatsapp_api.enviar_mensaje(
        "12345",
        "hola",
        tipo_respuesta="video",
        opciones="https://example.com/video.mp4",
    )

    assert ok is True
    assert get_called["args"][0] == "https://example.com/video.mp4"
    assert get_called["kwargs"]["stream"] is True
