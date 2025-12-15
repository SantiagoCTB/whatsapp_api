import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from services import whatsapp_api


class _DummyResponse:
    def raise_for_status(self):
        return None

    @staticmethod
    def json():
        return {"id": "media123"}


def test_subir_media_infers_ogg_when_guess_type_fails(monkeypatch, tmp_path):
    audio_path = tmp_path / "audio_sin_mime.ogg"
    audio_path.write_bytes(b"OggS" + b"\x00" * 12)

    monkeypatch.setattr(whatsapp_api.mimetypes, "guess_type", lambda *_: (None, None))
    monkeypatch.setattr(
        whatsapp_api,
        "_get_runtime_env",
        lambda: {"token": "test-token", "phone_id": "phone-1", "media_root": "/tmp"},
    )

    captured = {}

    def fake_post(url, headers=None, data=None, files=None):
        captured.update({"url": url, "headers": headers, "data": data, "files": files})
        return _DummyResponse()

    monkeypatch.setattr(whatsapp_api.requests, "post", fake_post)

    media_id = whatsapp_api.subir_media(str(audio_path))

    assert media_id == "media123"
    assert captured["data"]["type"] == "audio/ogg"
    uploaded = captured["files"]["file"]
    assert uploaded[0] == audio_path.name
    assert uploaded[2] == "audio/ogg"
