"""Cliente para interactuar con el modelo de IA configurado."""

from __future__ import annotations

import json
import logging
import importlib.util
import os
from typing import Iterable, Mapping

import requests

from config import Config
from services import tenants

if importlib.util.find_spec("openai"):
    from openai import OpenAI  # type: ignore
else:  # pragma: no cover - fallback para entornos sin SDK
    class _DummyCompletions:
        @staticmethod
        def create(*_, **__):
            return type("_DummyResponse", (), {"choices": []})()

    class _DummyChat:
        completions = _DummyCompletions()

    class OpenAI:  # type: ignore
        def __init__(self, *_, **__):
            self.chat = _DummyChat()

logger = logging.getLogger(__name__)


def _get_api_key() -> str:
    token = tenants.get_runtime_setting("IA_API_TOKEN", default=Config.IA_API_TOKEN)
    if not token:
        raise RuntimeError("IA_API_TOKEN no está configurado")
    return token


def get_api_key() -> str:
    return _get_api_key()


def _get_model() -> str:
    return tenants.get_runtime_setting("IA_MODEL", default=Config.IA_MODEL)


def _extract_response_text(payload: dict) -> str:
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def upload_file(file_path: str, *, purpose: str = "user_data") -> str | None:
    """Sube un archivo a OpenAI y devuelve el file_id."""

    try:
        api_key = _get_api_key()
    except RuntimeError as exc:  # pragma: no cover - depende del entorno
        logger.warning(str(exc))
        return None

    url = "https://api.openai.com/v1/files"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {"purpose": purpose}

    try:
        with open(file_path, "rb") as handle:
            files = {"file": (os.path.basename(file_path), handle)}
            resp = requests.post(url, headers=headers, data=data, files=files, timeout=120)
    except Exception as exc:  # pragma: no cover - depende del entorno
        logger.exception("No se pudo subir el archivo a OpenAI", exc_info=exc)
        return None

    if not resp.ok:
        logger.warning(
            "OpenAI rechazó el archivo",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        return None

    payload = resp.json()
    return payload.get("id")


def create_response_with_file(
    file_id: str,
    prompt: str,
    *,
    model: str | None = None,
) -> str:
    """Envía un prompt con un archivo adjunto y devuelve el texto generado."""

    try:
        api_key = _get_api_key()
    except RuntimeError as exc:  # pragma: no cover - depende del entorno
        logger.warning(str(exc))
        return ""

    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model or _get_model() or "o4-mini",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "file_id": file_id},
                    {"type": "input_text", "text": prompt},
                ],
            }
        ],
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=180)
    except Exception as exc:  # pragma: no cover - depende del entorno
        logger.exception("No se pudo obtener respuesta del modelo", exc_info=exc)
        return ""

    if not resp.ok:
        logger.warning(
            "OpenAI no pudo procesar el archivo",
            extra={"status": resp.status_code, "body": resp.text[:300]},
        )
        return ""

    payload = resp.json()
    return _extract_response_text(payload)


def build_messages(
    history: Iterable[Mapping[str, str]] | None,
    user_message: str | None,
    *,
    system_message: str | None = None,
):
    """Construye el payload de mensajes para la API del modelo."""

    system = (
        (system_message or tenants.get_runtime_setting("IA_SYSTEM_MESSAGE", default=Config.IA_SYSTEM_MESSAGE))
        or ""
    ).strip()
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})

    for entry in history or []:
        content = (entry.get("mensaje") or "").strip()
        if not content:
            continue
        tipo = (entry.get("tipo") or "").lower()
        role = "assistant" if not tipo.startswith("cliente") else "user"
        messages.append({"role": role, "content": content})

    if user_message:
        messages.append({"role": "user", "content": user_message})

    return messages


def build_messages_with_image(
    history: Iterable[Mapping[str, str]] | None,
    user_message: str | None,
    image_url: str,
    *,
    system_message: str | None = None,
):
    """Construye el payload de mensajes para la API del modelo con una imagen."""

    messages = build_messages(history, None, system_message=system_message)
    content: list[dict[str, str | dict[str, str]]] = []
    if user_message:
        content.append({"type": "text", "text": user_message})
    if image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    if content:
        messages.append({"role": "user", "content": content})
    return messages


def generate_response(
    history: Iterable[Mapping[str, str]] | None,
    user_message: str,
    *,
    system_message: str | None = None,
) -> str:
    """Genera una respuesta usando el modelo configurado."""

    try:
        api_key = _get_api_key()
    except RuntimeError as exc:  # pragma: no cover - depende del entorno
        logger.warning(str(exc))
        return ""

    model = _get_model() or "o4-mini"
    client = OpenAI(api_key=api_key)
    messages = build_messages(history, user_message, system_message=system_message)
    if not messages:
        logger.info("No hay mensajes para enviar al modelo de IA")
        return ""

    logger.debug(
        "Enviando solicitud al modelo IA",
        extra={
            "model": model,
            "message_count": len(messages),
            "user_length": len(user_message or ""),
        },
    )

    try:
        completion = client.chat.completions.create(model=model, messages=messages)
    except Exception as exc:  # pragma: no cover - depende de la API externa
        logger.exception("No se pudo obtener respuesta del modelo", exc_info=exc)
        return ""

    choice = completion.choices[0].message if completion and completion.choices else None
    if not choice:
        logger.warning("La respuesta del modelo no incluyó opciones")
        return ""
    content = (choice.content or "").strip()
    if not content:
        logger.warning("La respuesta del modelo llegó vacía")
    return content


def generate_response_with_image(
    history: Iterable[Mapping[str, str]] | None,
    user_message: str,
    image_url: str,
    *,
    system_message: str | None = None,
) -> str:
    """Genera una respuesta usando el modelo configurado con una imagen."""

    try:
        api_key = _get_api_key()
    except RuntimeError as exc:  # pragma: no cover - depende del entorno
        logger.warning(str(exc))
        return ""

    model = _get_model() or "o4-mini"
    client = OpenAI(api_key=api_key)
    messages = build_messages_with_image(
        history, user_message, image_url, system_message=system_message
    )
    if not messages:
        logger.info("No hay mensajes para enviar al modelo de IA con imagen")
        return ""

    logger.debug(
        "Enviando solicitud multimodal al modelo IA",
        extra={
            "model": model,
            "message_count": len(messages),
            "user_length": len(user_message or ""),
        },
    )

    try:
        completion = client.chat.completions.create(model=model, messages=messages)
    except Exception as exc:  # pragma: no cover - depende de la API externa
        logger.exception("No se pudo obtener respuesta del modelo con imagen", exc_info=exc)
        return ""

    choice = completion.choices[0].message if completion and completion.choices else None
    if not choice:
        logger.warning("La respuesta del modelo multimodal no incluyó opciones")
        return ""
    content = (choice.content or "").strip()
    if not content:
        logger.warning("La respuesta multimodal llegó vacía")
    return content
