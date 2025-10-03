import json
import logging
import os
import sys
from types import SimpleNamespace

import pytest
import requests

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from services import whatsapp_api


class DummyResponse:
    def __init__(self, ok, status_code=200, text="", json_data=None):
        self.ok = ok
        self.status_code = status_code
        self.text = text
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


def test_enviar_mensaje_logs_info_on_success(monkeypatch, caplog):
    dummy_response = DummyResponse(
        ok=True,
        status_code=200,
        text="OK",
        json_data={"messages": [{"id": "wa-id"}]},
    )

    monkeypatch.setattr(whatsapp_api.requests, "post", lambda *args, **kwargs: dummy_response)
    monkeypatch.setattr(whatsapp_api, "guardar_mensaje", lambda *args, **kwargs: None)

    with caplog.at_level(logging.INFO):
        result = whatsapp_api.enviar_mensaje("123456789", "hola")

    assert result is True
    records = [record for record in caplog.records if record.message == "Mensaje enviado a WhatsApp API"]
    assert records, "No se registró el log de éxito"
    record = records[-1]
    assert record.levelno == logging.INFO
    assert record.numero == "123456789"
    assert record.tipo_respuesta == "texto"
    assert record.status_code == 200
    assert record.response_text == "OK"


def test_enviar_mensaje_logs_error_on_failure(monkeypatch, caplog):
    dummy_response = DummyResponse(ok=False, status_code=500, text="ERROR")

    monkeypatch.setattr(whatsapp_api.requests, "post", lambda *args, **kwargs: dummy_response)
    monkeypatch.setattr(whatsapp_api, "guardar_mensaje", lambda *args, **kwargs: None)

    with caplog.at_level(logging.ERROR):
        result = whatsapp_api.enviar_mensaje("987654321", "hola")

    assert result is False
    records = [record for record in caplog.records if record.message == "Error en la respuesta de WhatsApp API"]
    assert records, "No se registró el log de error"
    record = records[-1]
    assert record.levelno == logging.ERROR
    assert record.numero == "987654321"
    assert record.status_code == 500
    assert record.response_text == "ERROR"


def test_enviar_mensaje_logs_error_on_media_validation_exception(monkeypatch, caplog):
    def fake_head(*args, **kwargs):
        raise requests.RequestException("timeout")

    monkeypatch.setattr(whatsapp_api.requests, "head", fake_head)
    monkeypatch.setattr(whatsapp_api.requests, "post", lambda *args, **kwargs: pytest.fail("No se debería llamar a post"))

    with caplog.at_level(logging.ERROR):
        result = whatsapp_api.enviar_mensaje(
            "555555555",
            "mensaje",
            tipo_respuesta="image",
            opciones="http://example.com/image.jpg",
        )

    assert result is False
    records = [record for record in caplog.records if record.message == "Error al validar la URL de medios"]
    assert records, "No se registró el log de error por validación"
    record = records[-1]
    assert record.levelno == logging.ERROR
    assert record.numero == "555555555"
    assert record.media_link == "http://example.com/image.jpg"
    assert "timeout" in record.error


def test_enviar_mensaje_logs_error_on_media_validation_status(monkeypatch, caplog):
    monkeypatch.setattr(
        whatsapp_api.requests,
        "head",
        lambda *args, **kwargs: SimpleNamespace(status_code=404),
    )
    monkeypatch.setattr(whatsapp_api.requests, "post", lambda *args, **kwargs: pytest.fail("No se debería llamar a post"))

    with caplog.at_level(logging.ERROR):
        result = whatsapp_api.enviar_mensaje(
            "222222222",
            "mensaje",
            tipo_respuesta="image",
            opciones="http://example.com/image.jpg",
        )

    assert result is False
    records = [record for record in caplog.records if record.message == "Respuesta no exitosa al validar la URL de medios"]
    assert records, "No se registró el log de error por status"
    record = records[-1]
    assert record.levelno == logging.ERROR
    assert record.status_code == 404
    assert record.numero == "222222222"
    assert record.media_link == "http://example.com/image.jpg"


def test_enviar_mensaje_flow_payload_and_logging(monkeypatch, caplog):
    posted_payload = {}

    def fake_post(url, headers=None, json=None):
        posted_payload["url"] = url
        posted_payload["headers"] = headers
        posted_payload["json"] = json
        return DummyResponse(
            ok=True,
            status_code=200,
            text="OK",
            json_data={"messages": [{"id": "wa-id"}]},
        )

    monkeypatch.setattr(whatsapp_api.requests, "post", fake_post)
    monkeypatch.setattr(whatsapp_api.requests, "head", lambda *args, **kwargs: pytest.fail("No se debe validar media_link en flow"))
    monkeypatch.setattr(whatsapp_api, "guardar_mensaje", lambda *args, **kwargs: None)

    opciones = json.dumps({
        "flow_cta": "Ir al flujo",
        "flow_name": "mi_flujo",
        "mode": "draft",
        "flow_token": "token-123",
        "flow_action": "open",
        "flow_action_payload": {"step": "1"},
        "header": "Encabezado",
        "footer": "Pie",
    })

    with caplog.at_level(logging.INFO):
        resultado = whatsapp_api.enviar_mensaje(
            "111222333",
            "Mensaje base",
            tipo_respuesta="flow",
            opciones=opciones,
        )

    assert resultado is True

    assert posted_payload["url"].endswith("/messages")
    assert posted_payload["headers"]["Content-Type"] == "application/json"

    esperado = {
        "messaging_product": "whatsapp",
        "to": "111222333",
        "type": "interactive",
        "interactive": {
            "type": "flow",
            "body": {"text": "Mensaje base"},
            "header": {"type": "text", "text": "Encabezado"},
            "footer": {"text": "Pie"},
            "action": {
                "name": "flow",
                "parameters": {
                    "flow_message_version": "3",
                    "flow_cta": "Ir al flujo",
                    "flow_name": "mi_flujo",
                    "mode": "draft",
                    "flow_token": "token-123",
                    "flow_action": "open",
                    "flow_action_payload": {"step": "1"},
                },
            },
        },
    }

    assert posted_payload["json"] == esperado

    records = [record for record in caplog.records if record.message == "Mensaje enviado a WhatsApp API"]
    assert records, "No se registró el log de éxito"
    record = records[-1]
    assert record.levelno == logging.INFO
    assert record.numero == "111222333"
    assert record.tipo_respuesta == "flow"
    assert record.status_code == 200
    assert record.response_text == "OK"
