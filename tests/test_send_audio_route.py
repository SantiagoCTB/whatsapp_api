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
    monkeypatch.setattr(chat_routes, "get_chat_state", lambda numero: None)
    monkeypatch.setattr(chat_routes, "update_chat_state", lambda *_, **__: None)


def test_send_audio_generates_public_url_and_keeps_caption(tmp_path, client, monkeypatch):
    captured = {}

    _patch_chat_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(
        chat_routes,
        "enviar_mensaje",
        lambda numero, caption, tipo, tipo_respuesta, opciones: captured.update(
            {
                "numero": numero,
                "caption": caption,
                "tipo": tipo,
                "tipo_respuesta": tipo_respuesta,
                "opciones": opciones,
            }
        ),
    )

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
    assert payload["url"].endswith(".webm")
    assert captured["caption"] == "nota de voz"
    assert captured["opciones"] == payload["url"]


def test_send_audio_rejects_unknown_format(tmp_path, client, monkeypatch):
    _patch_chat_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(chat_routes, "enviar_mensaje", lambda *_, **__: None)

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
