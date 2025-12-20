import json
import os
import io
import sys
from pathlib import Path

import pytest

os.environ.setdefault("INIT_DB_ON_START", "0")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import create_app
from routes import chat_routes


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def _patch_chat_dependencies(monkeypatch, media_root):
    monkeypatch.setattr(chat_routes, "MEDIA_ROOT", str(media_root))
    media_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(chat_routes.tenants, "get_media_root", lambda *_, **__: str(media_root))
    monkeypatch.setattr(chat_routes, "get_chat_state", lambda numero: None)
    monkeypatch.setattr(chat_routes, "update_chat_state", lambda *_, **__: None)
    monkeypatch.setattr(chat_routes, "subir_media", lambda path: "media123")


def test_media_root_falls_back_to_static_when_outside_static(monkeypatch, tmp_path):
    outside_static = tmp_path / "external_uploads"
    monkeypatch.setattr(
        chat_routes.tenants,
        "get_runtime_setting",
        lambda *_, **__: str(outside_static),
    )

    resolved_root = Path(chat_routes._media_root())

    assert resolved_root.resolve() == Path(chat_routes.Config.MEDIA_ROOT).resolve()
    assert resolved_root.exists()


def test_serve_media_returns_correct_content_type(tmp_path, client, monkeypatch):
    _patch_chat_dependencies(monkeypatch, tmp_path)
    filename = "sample.ogg"
    media_path = tmp_path / filename
    media_path.write_bytes(b"ogg-bytes")

    response = client.get(f"/media/{filename}")

    assert response.status_code == 200
    assert response.mimetype == "audio/ogg"


def test_send_audio_generates_public_url_and_keeps_caption(tmp_path, client, monkeypatch):
    captured = {}

    _patch_chat_dependencies(monkeypatch, tmp_path)
    converted_path = tmp_path / "converted.mp3"
    converted_path.write_bytes(b"converted")
    converted_m4a_path.write_bytes(b"converted")
    monkeypatch.setattr(
        chat_routes,
        "_convert_audio_to_mp3",
        lambda src: (str(converted_path), None),
    )
    def fake_send(numero, caption, tipo, tipo_respuesta, opciones=None, **kwargs):
        captured.setdefault("calls", []).append(
            {
                "numero": numero,
                "caption": caption,
                "tipo": tipo,
                "tipo_respuesta": tipo_respuesta,
                "opciones": opciones,
            }
        )
        return True, None

    monkeypatch.setattr(chat_routes, "enviar_mensaje", fake_send)

    with client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["rol"] = "admin"

    data = {
        "numero": "5215551234",
        "caption": "nota de voz",
        "audio": (io.BytesIO(b"audio-bytes"), "grabacion", "audio/webm"),
    }

    response = client.post(
        "/send_audio",
        data=data,
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "sent_audio"
    assert payload["url"].startswith("http")
    assert payload["url"].endswith(".mp3")
    assert "/media/" in payload["url"]
    assert payload["urls"]["audio_mp3_url"].endswith(".mp3")

    assert len(captured["calls"]) == 2

    media_call = captured["calls"][0]
    assert media_call["caption"] == ""
    assert media_call["opciones"]["id"] == "media123"
    assert media_call["opciones"]["link"] == payload["url"]
    assert media_call["opciones"]["voice"] is True
    assert media_call["tipo_respuesta"] == "audio"

    text_call = captured["calls"][1]
    assert text_call["tipo_respuesta"] == "texto"
    assert text_call["caption"] == "nota de voz"


def test_send_audio_rejects_unknown_format(tmp_path, client, monkeypatch):
    _patch_chat_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_routes, "enviar_mensaje", lambda *_, **__: None)
    monkeypatch.setattr(chat_routes, "subir_media", lambda *_: "media123")

    with client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["rol"] = "admin"

    response = client.post(
        "/send_audio",
        data={
            "numero": "5215550000",
            "audio": (io.BytesIO(b"123"), "sin_extension", "application/octet-stream"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert "audio válido" in payload["error"].lower()


def test_send_audio_rejects_empty_recording(tmp_path, client, monkeypatch):
    _patch_chat_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_routes, "enviar_mensaje", lambda *_, **__: None)
    monkeypatch.setattr(chat_routes, "subir_media", lambda *_: "media123")

    with client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["rol"] = "admin"

    response = client.post(
        "/send_audio",
        data={
            "numero": "5215550001",
            "audio": (io.BytesIO(b""), "vacio.ogg", "audio/ogg"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert "vacío" in payload["error"].lower()


def test_send_audio_rejects_when_conversion_fails(tmp_path, client, monkeypatch):
    _patch_chat_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_routes, "_convert_audio_to_mp3", lambda *_: (None, "fail"))
    captured = {}

    def fake_send(numero, caption, tipo, tipo_respuesta, opciones=None, **kwargs):
        captured.setdefault("calls", []).append(
            {
                "numero": numero,
                "caption": caption,
                "tipo": tipo,
                "tipo_respuesta": tipo_respuesta,
                "opciones": opciones,
            }
        )
        return True, None

    monkeypatch.setattr(chat_routes, "enviar_mensaje", fake_send)
    monkeypatch.setattr(chat_routes, "subir_media", lambda *_: "media123")

    with client.session_transaction() as sess:
        sess["user"] = "tester"
        sess["rol"] = "admin"

    response = client.post(
        "/send_audio",
        data={
            "numero": "5215559999",
            "caption": "nota",
            "audio": (io.BytesIO(b"audio-bytes"), "grabacion", "audio/webm"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 422
    payload = response.get_json()
    assert payload["error"] == "fail"
    assert "calls" not in captured
