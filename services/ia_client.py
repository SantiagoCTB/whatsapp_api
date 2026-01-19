"""Cliente para interactuar con el modelo de IA configurado."""

from __future__ import annotations

import logging
import importlib.util
from typing import Iterable, Mapping

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


def _get_model() -> str:
    return tenants.get_runtime_setting("IA_MODEL", default=Config.IA_MODEL)


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
