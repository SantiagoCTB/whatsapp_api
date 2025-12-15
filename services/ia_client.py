"""Cliente para interactuar con el modelo de IA configurado."""

from __future__ import annotations

import logging
import importlib.util
import logging
from typing import Iterable, Mapping

from config import Config
from services import tenants, db

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


def _get_api_key(settings: dict | None = None) -> str:
    settings = settings or _get_runtime_ia_settings()
    token = settings.get("token") or ""
    if not token:
        raise RuntimeError("IA_API_TOKEN no está configurado")
    return token


def _get_model(settings: dict | None = None) -> str:
    settings = settings or _get_runtime_ia_settings()
    return settings.get("model") or "o4-mini"


def _get_runtime_ia_settings() -> dict:
    config = _load_db_ia_config()
    if not config or not config.get("enabled", True):
        return {"token": None, "model": None}

    return {
        "token": (config.get("token") or "").strip() or None,
        "model": (config.get("model") or "").strip() or "o4-mini",
    }


def _load_db_ia_config() -> dict | None:
    conn = None
    try:
        conn = db.get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SHOW TABLES LIKE 'ia_config'")
        if not cursor.fetchone():
            return None

        cursor.execute("SHOW COLUMNS FROM ia_config LIKE 'enabled';")
        has_enabled = cursor.fetchone() is not None
        select_cols = "model_name, model_token" + (", enabled" if has_enabled else "")
        cursor.execute(f"SELECT {select_cols} FROM ia_config ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if not row:
            return None

        return {
            "model": row.get("model_name"),
            "token": row.get("model_token"),
            "enabled": bool(row.get("enabled", True)) if has_enabled else True,
        }
    except Exception as exc:  # pragma: no cover - depende del entorno/DB
        logger.exception("No se pudo leer la configuración de IA", exc_info=exc)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


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

    settings = _get_runtime_ia_settings()
    try:
        api_key = _get_api_key(settings)
    except RuntimeError as exc:  # pragma: no cover - depende del entorno
        logger.warning(str(exc))
        return ""

    model = _get_model(settings)
    client = OpenAI(api_key=api_key)
    messages = build_messages(history, user_message, system_message=system_message)
    if not messages:
        logger.info("No hay mensajes para enviar al modelo de IA")
        return ""

    try:
        completion = client.chat.completions.create(model=model, messages=messages)
    except Exception as exc:  # pragma: no cover - depende de la API externa
        logger.exception("No se pudo obtener respuesta del modelo", exc_info=exc)
        return ""

    choice = completion.choices[0].message if completion and completion.choices else None
    return (choice.content or "").strip() if choice else ""
