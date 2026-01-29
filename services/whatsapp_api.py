import os
import json
import logging
import mimetypes
from pathlib import Path
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import has_request_context, request, url_for

from config import Config
from services import tenants
from services.db import guardar_mensaje, obtener_ultimo_mensaje_cliente_info


logger = logging.getLogger(__name__)

API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v19.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{API_VERSION}"
INSTAGRAM_GRAPH_BASE_URL = "https://graph.instagram.com/v24.0"

_typing_lock = threading.Lock()
_typing_sessions = {}
_typing_ui_state = set()
_TYPING_INITIAL_DELAY = 2.0
_TYPING_INTERVAL = 6.0
_TYPING_ENABLED = bool(getattr(Config, "ENABLE_TYPING_INDICATOR", False))


def _public_base_url() -> str | None:
    if has_request_context():
        return request.url_root
    base_url = tenants.get_runtime_setting("PUBLIC_BASE_URL", default=Config.PUBLIC_BASE_URL)
    if not base_url:
        return None
    return str(base_url).strip() or None


def _build_static_url(path: str) -> str | None:
    base_url = _public_base_url()
    if not base_url:
        logger.warning("PUBLIC_BASE_URL no está configurado; no se puede construir URL pública.")
        return None
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _build_upload_url(filename: str) -> str | None:
    if has_request_context():
        return url_for(
            "static",
            filename=tenants.get_uploads_url_path(filename),
            _external=True,
        )
    base_url = _public_base_url()
    if not base_url:
        logger.warning("PUBLIC_BASE_URL no está configurado; no se puede construir URL pública.")
        return None
    uploads_path = tenants.get_uploads_url_path(filename)
    return f"{base_url.rstrip('/')}/static/{uploads_path}"


def _resolve_public_media_url(raw_value: str | None) -> str | None:
    if raw_value is None:
        return None
    try:
        normalized = str(raw_value).strip()
    except Exception:
        return None
    if not normalized:
        return None

    lower = normalized.lower()
    if lower.startswith("http://"):
        return f"https://{normalized[len('http://'):]}"
    if lower.startswith("https://"):
        return normalized

    if os.path.isfile(normalized):
        filename = os.path.basename(normalized)
        return _build_upload_url(filename)

    if normalized.startswith("/"):
        return _build_static_url(normalized)

    if normalized.startswith(("static/", "uploads/")):
        if normalized.startswith("uploads/"):
            normalized = f"static/{normalized}"
        return _build_static_url(normalized)

    if "/" in normalized:
        return _build_static_url(normalized)

    if "." in normalized:
        return _build_upload_url(normalized)

    return normalized


def _extract_local_media_bytes(raw_value: Any) -> int | None:
    candidates = []
    if isinstance(raw_value, dict):
        for key in ("path", "file_path", "filename", "file", "url", "link", "id"):
            value = raw_value.get(key)
            if value:
                candidates.append(value)
    elif isinstance(raw_value, list):
        candidates.extend(raw_value)
    else:
        candidates.append(raw_value)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            normalized = str(candidate).strip()
        except Exception:
            continue
        if not normalized:
            continue
        if os.path.isfile(normalized):
            try:
                return os.path.getsize(normalized)
            except OSError:
                continue
    return None


def _extract_local_media_path(raw_value: Any) -> str | None:
    candidates = []
    if isinstance(raw_value, dict):
        for key in ("path", "file_path", "filename", "file", "url", "link", "id"):
            value = raw_value.get(key)
            if value:
                candidates.append(value)
    elif isinstance(raw_value, list):
        candidates.extend(raw_value)
    else:
        candidates.append(raw_value)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            normalized = str(candidate).strip()
        except Exception:
            continue
        if not normalized:
            continue
        if os.path.isfile(normalized):
            return normalized
    return None


def _extract_instagram_attachment_reference(raw_value: Any) -> tuple[Any, str | None]:
    attachment_url = None
    attachment_id = None
    if isinstance(raw_value, list):
        for item in raw_value:
            if not item:
                continue
            if isinstance(item, dict):
                attachment_url = item.get("url") or item.get("link") or item.get("id")
                attachment_id = item.get("attachment_id") or item.get("attachmentId")
                if not attachment_url:
                    for key in ("path", "file_path", "filename", "file"):
                        if item.get(key):
                            attachment_url = item.get(key)
                            break
            else:
                attachment_url = item
            if attachment_url or attachment_id:
                break
        return attachment_url, attachment_id

    if isinstance(raw_value, dict):
        attachment_url = raw_value.get("url") or raw_value.get("link") or raw_value.get("id")
        attachment_id = (
            raw_value.get("attachment_id")
            or raw_value.get("attachmentId")
            or (raw_value.get("id") if not attachment_url else None)
        )
        if not attachment_url:
            for key in ("path", "file_path", "filename", "file"):
                if raw_value.get(key):
                    attachment_url = raw_value.get(key)
                    break
    else:
        attachment_url = raw_value
    return attachment_url, attachment_id


def _instagram_request_timeout(tipo_respuesta: str, opciones: Any) -> float:
    base_timeout = 10.0
    if tipo_respuesta not in {"image", "audio", "video", "document"}:
        return base_timeout

    media_timeout = float(getattr(Config, "MESSENGER_MEDIA_TIMEOUT_SECONDS", 180))
    if tipo_respuesta != "video":
        return media_timeout

    size_bytes = _extract_local_media_bytes(opciones)
    if not size_bytes:
        return media_timeout

    size_mb = size_bytes / (1024 * 1024)
    dynamic_timeout = media_timeout + (size_mb * 1.5)
    max_timeout = max(media_timeout, media_timeout * 3)
    return min(dynamic_timeout, max_timeout)


def _upload_instagram_attachment(
    file_path: str,
    attachment_type: str,
    *,
    is_reusable: bool = True,
) -> tuple[str | None, str | None]:
    try:
        runtime = _get_instagram_env()
    except RuntimeError as exc:
        return None, f"No se puede subir adjunto a Instagram: {exc}"

    if not os.path.isfile(file_path):
        return None, f"Adjunto de Instagram no existe: {file_path}"

    url = f"{GRAPH_BASE_URL}/me/message_attachments"
    headers = {"Authorization": f"Bearer {runtime['token']}"}
    attachment_payload = {
        "type": attachment_type,
        "payload": {"is_reusable": is_reusable},
    }

    try:
        with open(file_path, "rb") as file_handle:
            response = requests.post(
                url,
                headers=headers,
                data={"message_attachment": json.dumps(attachment_payload)},
                files={"filedata": file_handle},
                timeout=Config.MESSENGER_ATTACHMENT_TIMEOUT_SECONDS,
            )
    except requests.RequestException as exc:
        return None, f"Error subiendo adjunto a Instagram: {exc}"

    if response.status_code >= 400:
        details = _extract_error_details(response)
        return (
            None,
            "Fallo al subir adjunto a Instagram"
            f" (status={response.status_code}, details={details})",
        )

    try:
        payload = response.json()
    except ValueError:
        return None, "Respuesta inválida al subir adjunto a Instagram: no JSON"

    attachment_id = payload.get("attachment_id") if isinstance(payload, dict) else None
    if not attachment_id:
        return None, "Respuesta sin attachment_id al subir adjunto a Instagram"

    return attachment_id, None


def _upload_messenger_attachment(
    file_path: str,
    attachment_type: str,
    *,
    is_reusable: bool = True,
) -> str | None:
    try:
        runtime = _get_messenger_env()
    except RuntimeError as exc:
        logger.error("No se puede subir adjunto a Messenger: %s", exc)
        return None

    if not os.path.isfile(file_path):
        logger.error("Adjunto de Messenger no existe: %s", file_path)
        return None

    url = f"{GRAPH_BASE_URL}/me/message_attachments"
    headers = {"Authorization": f"Bearer {runtime['token']}"}
    attachment_payload = {
        "type": attachment_type,
        "payload": {"is_reusable": is_reusable},
    }

    try:
        with open(file_path, "rb") as file_handle:
            response = requests.post(
                url,
                headers=headers,
                data={"message_attachment": json.dumps(attachment_payload)},
                files={"filedata": file_handle},
                timeout=Config.MESSENGER_ATTACHMENT_TIMEOUT_SECONDS,
            )
    except requests.RequestException as exc:
        logger.error("Error subiendo adjunto a Messenger: %s", exc)
        return None

    if response.status_code >= 400:
        details = _extract_error_details(response)
        logger.error(
            "Fallo al subir adjunto a Messenger",
            extra={"status": response.status_code, "details": details},
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.error("Respuesta inválida al subir adjunto a Messenger: no JSON")
        return None

    attachment_id = payload.get("attachment_id") if isinstance(payload, dict) else None
    if not attachment_id:
        logger.error("Respuesta sin attachment_id al subir adjunto a Messenger")
        return None

    return attachment_id


def _get_runtime_env():
    env = tenants.get_current_tenant_env()
    token = (env.get("META_TOKEN") or "").strip()
    phone_id = (env.get("PHONE_NUMBER_ID") or "").strip()
    media_root = tenants.get_media_root()

    missing = []
    if not token:
        missing.append("META_TOKEN")
    if not phone_id:
        missing.append("PHONE_NUMBER_ID")

    if missing:
        raise RuntimeError(
            "Faltan credenciales de WhatsApp en el tenant actual: "
            + ", ".join(missing)
        )

    return {
        "token": token,
        "phone_id": phone_id,
        "media_root": media_root,
    }


def _get_messenger_env():
    env = tenants.get_current_tenant_env()
    page_token = (
        (env.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
        or (env.get("PAGE_ACCESS_TOKEN") or "").strip()
    )
    fallback_token = (env.get("MESSENGER_TOKEN") or "").strip()
    token = page_token or fallback_token
    page_id = (
        (env.get("MESSENGER_PAGE_ID") or "").strip()
        or (env.get("PAGE_ID") or "").strip()
    )

    missing = []
    if not token:
        missing.append("MESSENGER_PAGE_ACCESS_TOKEN/MESSENGER_TOKEN")
    if not page_id:
        missing.append("PAGE_ID")
    if missing:
        raise RuntimeError(
            "Faltan credenciales de Messenger en el tenant actual: " + ", ".join(missing)
        )

    return {"token": token, "page_id": page_id}


def _get_instagram_env():
    env = tenants.get_current_tenant_env()
    tenant = tenants.get_current_tenant()
    if tenant:
        tenant_env = tenants.get_tenant_env(tenant)
        env = {**tenant_env, **(env or {})}
    token = (env.get("INSTAGRAM_TOKEN") or "").strip()
    instagram_account_id = (
        (env.get("INSTAGRAM_ACCOUNT_ID") or "").strip()
        or (env.get("INSTAGRAM_PAGE_ID") or "").strip()
    )
    if not instagram_account_id and tenant and isinstance(tenant.metadata, dict):
        instagram_account = tenant.metadata.get("instagram_account") or {}
        if isinstance(instagram_account, dict):
            instagram_account_id = (instagram_account.get("id") or "").strip()
        if not instagram_account_id:
            page_selection = tenant.metadata.get("page_selection") or {}
            if isinstance(page_selection, dict):
                instagram_selection = page_selection.get("instagram") or {}
                if isinstance(instagram_selection, dict):
                    instagram_account_id = (
                        instagram_selection.get("page_id") or ""
                    ).strip()

    missing = []
    if not token:
        missing.append("INSTAGRAM_TOKEN")
    if not instagram_account_id:
        missing.append("INSTAGRAM_ACCOUNT_ID")
    if missing:
        raise RuntimeError(
            "Faltan credenciales de Instagram en el tenant actual: " + ", ".join(missing)
        )

    return {"token": token, "page_id": instagram_account_id}


def _get_messenger_messaging_type() -> str:
    value = tenants.get_runtime_setting(
        "MESSENGER_MESSAGING_TYPE", default="RESPONSE"
    )
    normalized = str(value or "").strip().upper()
    if normalized in {"RESPONSE", "UPDATE", "MESSAGE_TAG"}:
        return normalized
    if normalized:
        logger.warning(
            "MESSENGER_MESSAGING_TYPE inválido; se usará RESPONSE",
            extra={"messaging_type": normalized},
        )
    return "RESPONSE"


def _get_messenger_message_tag() -> str | None:
    tag = tenants.get_runtime_setting("MESSENGER_MESSAGE_TAG", default="")
    tag = str(tag or "").strip()
    return tag or None


def _get_whatsapp_video_limit_bytes() -> int:
    limit_mb = tenants.get_runtime_setting(
        "WHATSAPP_VIDEO_MAX_MB",
        default=Config.WHATSAPP_VIDEO_MAX_MB,
        cast=int,
    )
    if not isinstance(limit_mb, int):
        limit_mb = Config.WHATSAPP_VIDEO_MAX_MB
    if limit_mb < 1:
        limit_mb = 1
    return limit_mb * 1024 * 1024


def _resolve_message_channel(numero: str) -> str:
    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    last_tipo = (last_client_info or {}).get("tipo") or ""
    last_tipo_lower = str(last_tipo).lower()
    if "messenger" in last_tipo_lower:
        return "messenger"
    if "instagram" in last_tipo_lower:
        return "instagram"
    return "whatsapp"


def _messenger_window_open(numero: str) -> bool:
    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    if not last_client_info:
        return False
    last_ts = last_client_info.get("timestamp")
    if not isinstance(last_ts, datetime):
        return False
    elapsed_seconds = (datetime.utcnow() - last_ts).total_seconds()
    return elapsed_seconds <= 24 * 3600


def _instagram_window_open(numero: str) -> bool:
    return _messenger_window_open(numero)


def _extract_error_details(response: requests.Response) -> Dict[str, Any]:
    """Extrae información útil de un error devuelto por la API de WhatsApp."""

    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return {"raw_text": text[:1000]} if text else {}

    if not isinstance(payload, dict):
        return {"response_json": payload}

    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        details: Dict[str, Any] = {}
        for key in ("message", "type", "code", "error_subcode", "fbtrace_id"):
            value = error_obj.get(key)
            if value not in (None, ""):
                details[key] = value
        if error_obj.get("details"):
            details["details"] = error_obj["details"]
        return details or {"response_json": payload}

    return {"response_json": payload}


def _normalize_flow_options(raw_options: Dict[str, Any]) -> Dict[str, Any]:
    """Normaliza las opciones de un flow provenientes del panel."""

    if not isinstance(raw_options, dict):
        return {}

    options = {}
    for key, value in raw_options.items():
        if isinstance(value, str):
            value = value.strip()
        options[key] = value

    alias_map = {
        "cta": "flow_cta",
        "version": "flow_message_version",
        "token": "flow_token",
        "action": "flow_action",
        "header": "flow_header",
        "body": "flow_body",
        "footer": "flow_footer",
    }

    for alias, target in alias_map.items():
        alias_value = options.get(alias)
        if alias_value and target not in options:
            options[target] = alias_value

    payload = options.get("flow_action_payload")
    payload_obj: Optional[Dict[str, Any]]
    if isinstance(payload, str) and payload:
        try:
            payload_obj = json.loads(payload)
        except json.JSONDecodeError:
            payload_obj = None
    elif isinstance(payload, dict):
        payload_obj = dict(payload)
    else:
        payload_obj = None

    if payload_obj is None:
        payload_obj = {}

    initial_screen = options.get("initial_screen") or options.get("flow_initial_screen")
    if initial_screen and "screen" not in payload_obj:
        payload_obj["screen"] = initial_screen

    data_value = options.get("data") or options.get("flow_data")
    if data_value not in (None, "") and "data" not in payload_obj:
        if isinstance(data_value, str):
            stripped = data_value.strip()
            if stripped:
                try:
                    data_value = json.loads(stripped)
                except json.JSONDecodeError:
                    data_value = stripped
                else:
                    data_value = data_value
        payload_obj["data"] = data_value

    if payload_obj:
        options["flow_action_payload"] = payload_obj
    else:
        options.pop("flow_action_payload", None)

    return options


def list_phone_numbers(token: str, waba_id: str) -> Dict[str, Any]:
    if not token or not waba_id:
        return {"ok": False, "error": "Faltan credenciales para consultar números."}

    url = f"{GRAPH_BASE_URL}/{waba_id}/phone_numbers"
    params = {
        "fields": "id,display_phone_number,verified_name,quality_rating,code_verification_status",
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
    except requests.RequestException as exc:
        logger.warning("Error consultando números de WhatsApp: %s", exc)
        return {"ok": False, "error": "No se pudo conectar con la API de Meta."}

    if response.status_code >= 400:
        details = _extract_error_details(response)
        logger.warning(
            "Respuesta inválida al listar números de WhatsApp",
            extra={"status": response.status_code, "details": details},
        )
        return {"ok": False, "error": "No se pudieron obtener los números.", "details": details}

    try:
        payload = response.json()
    except ValueError:
        logger.warning("Respuesta inválida al listar números de WhatsApp: no JSON")
        return {"ok": False, "error": "Respuesta inválida de la API."}

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {"ok": False, "error": "No se encontraron números disponibles."}

    phone_numbers = []
    for item in data:
        if not isinstance(item, dict):
            continue
        phone_numbers.append(
            {
                "id": item.get("id"),
                "display_phone_number": item.get("display_phone_number"),
                "verified_name": item.get("verified_name"),
                "quality_rating": item.get("quality_rating"),
                "code_verification_status": item.get("code_verification_status"),
            }
        )

    return {"ok": True, "data": phone_numbers}

def enviar_mensaje(
    numero,
    mensaje,
    tipo='bot',
    tipo_respuesta='texto',
    opciones=None,
    reply_to_wa_id=None,
    step=None,
    regla_id=None,
    *,
    return_error=False,
):
    def _serialize_opciones(raw_opciones):
        if raw_opciones is None:
            return None
        if isinstance(raw_opciones, str):
            value = raw_opciones.strip()
            return value or None
        try:
            return json.dumps(raw_opciones, ensure_ascii=False)
        except (TypeError, ValueError):
            return None

    def _result(success, reason=None):
        if return_error:
            return success, reason
        return success

    def _fail(reason=None):
        stop_typing_feedback(numero)
        return _result(False, reason)

    channel = _resolve_message_channel(numero)
    if channel == "messenger":
        try:
            runtime = _get_messenger_env()
        except RuntimeError as exc:
            logger.error("No se puede enviar mensaje de Messenger: %s", exc)
            return _result(False, str(exc))

        if not _messenger_window_open(numero):
            return _result(
                False,
                "El usuario de Facebook tiene que haber enviado mensajes a esta página antes de escribirle.",
            )

        url = f"{GRAPH_BASE_URL}/{runtime['page_id']}/messages"
        headers = {
            "Authorization": f"Bearer {runtime['token']}",
            "Content-Type": "application/json",
        }

        messaging_type = _get_messenger_messaging_type()
        payload = {
            "recipient": {"id": numero},
            "messaging_type": messaging_type,
        }
        if messaging_type == "MESSAGE_TAG":
            message_tag = _get_messenger_message_tag()
            if not message_tag:
                return _fail(
                    "Debes configurar MESSENGER_MESSAGE_TAG para enviar mensajes etiquetados."
                )
            payload["tag"] = message_tag
        if reply_to_wa_id:
            payload["reply_to"] = {"mid": reply_to_wa_id}

        attachment_type = None
        attachment_url = None
        if tipo_respuesta == "texto":
            payload["message"] = {"text": mensaje}
        elif tipo_respuesta in {"image", "audio", "video", "document"}:
            attachment_type = "file" if tipo_respuesta == "document" else tipo_respuesta
            if isinstance(opciones, list) and tipo_respuesta == "image":
                attachments = []
                for item in opciones:
                    if isinstance(item, dict):
                        url = item.get("url") or item.get("link") or item.get("id")
                    else:
                        url = item
                    if not url:
                        continue
                    resolved_url = _resolve_public_media_url(url)
                    if not resolved_url:
                        continue
                    attachments.append({"type": "image", "payload": {"url": resolved_url}})
                if attachments:
                    payload["message"] = {"attachments": attachments}
            if "message" not in payload:
                attachment_id = None
                attachment_raw = None
                if isinstance(opciones, dict):
                    attachment_url = opciones.get("url") or opciones.get("link")
                    attachment_raw = attachment_url or opciones.get("path") or opciones.get("file")
                    attachment_id = (
                        opciones.get("attachment_id")
                        or opciones.get("attachmentId")
                        or (opciones.get("id") if not attachment_url else None)
                    )
                else:
                    attachment_url = opciones
                    attachment_raw = attachment_url
                if not attachment_url and not attachment_id:
                    return _fail("No se pudo enviar el adjunto a Messenger.")
                if attachment_id and not attachment_url:
                    payload["message"] = {
                        "attachment": {
                            "type": attachment_type,
                            "payload": {"attachment_id": attachment_id},
                        }
                    }
                else:
                    attachment_url = _resolve_public_media_url(attachment_url)
                    if attachment_url:
                        payload["message"] = {
                            "attachment": {
                                "type": attachment_type,
                                "payload": {"url": attachment_url, "is_reusable": True},
                            }
                        }
                    else:
                        attachment_path = None
                        if isinstance(attachment_raw, str) and os.path.isfile(attachment_raw):
                            attachment_path = attachment_raw
                        if attachment_path:
                            attachment_id = _upload_messenger_attachment(
                                attachment_path, attachment_type
                            )
                            if not attachment_id:
                                return _fail(
                                    "No se pudo subir el adjunto a Messenger."
                                )
                            payload["message"] = {
                                "attachment": {
                                    "type": attachment_type,
                                    "payload": {"attachment_id": attachment_id},
                                }
                            }
                        else:
                            return _fail(
                                "No se pudo construir una URL pública para el adjunto de Messenger."
                            )
        elif tipo_respuesta == "boton":
            try:
                botones = json.loads(opciones) if opciones else []
            except Exception:
                botones = []

            botones_messenger = []
            for boton in botones:
                if not isinstance(boton, dict):
                    continue
                btn_clean = {k: v for k, v in boton.items() if k not in {"step", "next_step"}}
                boton_type = (btn_clean.get("type") or "").strip().lower()
                reply_obj = btn_clean.get("reply") if isinstance(btn_clean.get("reply"), dict) else {}
                title = (
                    btn_clean.get("title")
                    or reply_obj.get("title")
                    or btn_clean.get("text")
                    or btn_clean.get("label")
                )
                payload_value = (
                    btn_clean.get("payload")
                    or btn_clean.get("PAYLOAD")
                    or btn_clean.get("id")
                    or reply_obj.get("id")
                )
                url_value = btn_clean.get("url") or btn_clean.get("link")
                phone_value = btn_clean.get("phone_number") or btn_clean.get("phone")

                if boton_type in {"web_url", "url", "link"} or url_value:
                    if url_value and title:
                        botones_messenger.append(
                            {"type": "web_url", "url": url_value, "title": title}
                        )
                    continue

                if boton_type in {"postback", "payload", "reply"} or payload_value:
                    if payload_value and title:
                        botones_messenger.append(
                            {"type": "postback", "payload": payload_value, "title": title}
                        )
                    continue

                if boton_type in {"phone_number", "call", "phone"}:
                    phone_number = phone_value or payload_value
                    if phone_number and title:
                        botones_messenger.append(
                            {"type": "phone_number", "payload": phone_number, "title": title}
                        )
                    continue

                if boton_type == "account_link":
                    if url_value:
                        botones_messenger.append({"type": "account_link", "url": url_value})
                    continue

                if boton_type == "account_unlink":
                    botones_messenger.append({"type": "account_unlink"})
                    continue

                if boton_type == "game_play":
                    if title:
                        button = {"type": "game_play", "title": title}
                        if payload_value:
                            button["payload"] = payload_value
                        if isinstance(btn_clean.get("game_metadata"), dict):
                            button["game_metadata"] = btn_clean["game_metadata"]
                        botones_messenger.append(button)

            if not botones_messenger:
                logger.warning(
                    "Botones vacíos para Messenger; se envía texto de fallback",
                    extra={"numero": numero, "tipo_respuesta": tipo_respuesta},
                )
                fallback_text = mensaje or "Por favor responde con texto."
                payload["message"] = {"text": fallback_text}
            else:
                prompt_text = mensaje or "Selecciona una opción."
                payload["message"] = {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "button",
                            "text": prompt_text,
                            "buttons": botones_messenger[:3],
                        },
                    }
                }
        elif tipo_respuesta in {"lista", "flow"}:
            logger.warning(
                "Tipo no soportado por Messenger; se envía texto de fallback",
                extra={"numero": numero, "tipo_respuesta": tipo_respuesta},
            )
            fallback_text = mensaje or "Por favor responde con texto."
            payload["message"] = {"text": fallback_text}
        else:
            return _fail("Tipo de respuesta no soportado para Messenger.")

        timeout_seconds = 10
        if tipo_respuesta in {"image", "audio", "video", "document"}:
            timeout_seconds = Config.MESSENGER_MEDIA_TIMEOUT_SECONDS
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
        except requests.RequestException as exc:
            logger.error("Error enviando solicitud a Messenger API: %s", exc)
            return _fail("No se pudo conectar con la API de Messenger.")

        if not resp.ok:
            error_details = _extract_error_details(resp)
            logger.error(
                "Fallo al enviar mensaje a Messenger API",
                extra={"status_code": resp.status_code, "details": error_details},
            )
            friendly_reason = (
                error_details.get("message")
                or error_details.get("raw_text")
                or "Messenger rechazó el mensaje."
            )
            return _fail(friendly_reason)

        stop_typing_feedback(numero)
        try:
            message_id = resp.json().get("message_id")
        except Exception:
            message_id = None


        tipo_db = tipo
        if "messenger" not in tipo_db:
            tipo_db = f"{tipo_db}_messenger"
        if tipo_respuesta in {"image", "audio", "video", "document"} and not tipo_db.endswith(
            f"_{tipo_respuesta}"
        ):
            tipo_db = f"{tipo_db}_{tipo_respuesta}"

        guardar_mensaje(
            numero,
            mensaje,
            tipo_db,
            wa_id=message_id,
            reply_to_wa_id=reply_to_wa_id,
            media_id=None,
            media_url=attachment_url,
            opciones=_serialize_opciones(opciones) if tipo_respuesta == "boton" else None,
            step=step,
            regla_id=regla_id,
        )
        return _result(True)

    if channel == "instagram":
        try:
            runtime = _get_instagram_env()
        except RuntimeError as exc:
            logger.error("No se puede enviar mensaje de Instagram: %s", exc)
            return _result(False, str(exc))

        if not _instagram_window_open(numero):
            return _result(
                False,
                "El usuario de Instagram tiene que haber enviado mensajes a esta cuenta antes de escribirle.",
            )

        url = f"{INSTAGRAM_GRAPH_BASE_URL}/{runtime['page_id']}/messages"
        headers = {
            "Authorization": f"Bearer {runtime['token']}",
            "Content-Type": "application/json",
        }

        payload = {
            "recipient": {"id": numero},
            "message": {},
        }
        if reply_to_wa_id:
            payload["reply_to"] = {"mid": reply_to_wa_id}

        attachment_type = None
        attachment_url = None
        if tipo_respuesta == "texto":
            payload["message"] = {"text": mensaje}
        elif tipo_respuesta in {"image", "audio", "video", "document"}:
            attachment_type = "file" if tipo_respuesta == "document" else tipo_respuesta
            if tipo_respuesta == "image":
                if isinstance(opciones, list):
                    for item in opciones:
                        if isinstance(item, dict):
                            attachment_url = item.get("url") or item.get("link") or item.get("id")
                        else:
                            attachment_url = item
                        if attachment_url:
                            break
                else:
                    if isinstance(opciones, dict):
                        attachment_url = opciones.get("url") or opciones.get("link") or opciones.get("id")
                    else:
                        attachment_url = opciones
                attachment_url = _resolve_public_media_url(attachment_url)
                attachment_id = None
                upload_error = None
                local_path = _extract_local_media_path(opciones)
                if local_path and not attachment_url:
                    attachment_id, upload_error = _upload_instagram_attachment(
                        local_path,
                        attachment_type,
                    )
                if attachment_id:
                    payload["message"] = {
                        "attachment": {
                            "type": "image",
                            "payload": {"attachment_id": attachment_id},
                        }
                    }
                else:
                    if not attachment_url:
                        if upload_error:
                            logger.error(
                                "No se pudo enviar el adjunto a Instagram: %s",
                                upload_error,
                            )
                        return _fail("No se pudo enviar el adjunto a Instagram.")
                    payload["message"] = {
                        "attachment": {
                            "type": "image",
                            "payload": {"url": attachment_url},
                        }
                    }
            else:
                attachment_url, attachment_id = _extract_instagram_attachment_reference(opciones)
                attachment_url = _resolve_public_media_url(attachment_url)
                local_path = _extract_local_media_path(opciones)
                upload_error = None
                if local_path and not attachment_id and not attachment_url:
                    attachment_id, upload_error = _upload_instagram_attachment(
                        local_path,
                        attachment_type,
                    )
                if attachment_id:
                    payload["message"] = {
                        "attachment": {
                            "type": attachment_type,
                            "payload": {"attachment_id": attachment_id},
                        }
                    }
                else:
                    if not attachment_url:
                        if upload_error:
                            logger.error(
                                "No se pudo enviar el adjunto a Instagram: %s",
                                upload_error,
                            )
                        return _fail("No se pudo enviar el adjunto a Instagram.")
                    payload["message"] = {
                        "attachment": {
                            "type": attachment_type,
                            "payload": {"url": attachment_url},
                        }
                    }
        elif tipo_respuesta == "boton":
            try:
                botones = json.loads(opciones) if opciones else []
            except Exception:
                botones = []

            botones_instagram = []
            for boton in botones:
                if not isinstance(boton, dict):
                    continue
                boton_type = (boton.get("type") or "").strip().lower()
                reply_obj = boton.get("reply") if isinstance(boton.get("reply"), dict) else {}
                title = (
                    boton.get("title")
                    or reply_obj.get("title")
                    or boton.get("text")
                    or boton.get("label")
                )
                payload_value = (
                    boton.get("payload")
                    or boton.get("PAYLOAD")
                    or boton.get("id")
                    or reply_obj.get("id")
                )
                url_value = boton.get("url") or boton.get("link")

                if boton_type in {"web_url", "url", "link"} or url_value:
                    if url_value and title:
                        botones_instagram.append(
                            {"type": "web_url", "url": url_value, "title": title}
                        )
                    continue

                if boton_type in {"postback", "payload", "reply"} or payload_value:
                    if payload_value and title:
                        botones_instagram.append(
                            {
                                "type": "postback",
                                "payload": payload_value,
                                "title": title,
                            }
                        )

            if not botones_instagram:
                logger.warning(
                    "Botones vacíos para Instagram; se envía texto de fallback",
                    extra={"numero": numero, "tipo_respuesta": tipo_respuesta},
                )
                fallback_text = mensaje or "Por favor responde con texto."
                payload["message"] = {"text": fallback_text}
            else:
                prompt_text = mensaje or "Selecciona una opción."
                payload["message"] = {
                    "attachment": {
                        "type": "template",
                        "payload": {
                            "template_type": "button",
                            "text": prompt_text,
                            "buttons": botones_instagram[:3],
                        },
                    }
                }
        elif tipo_respuesta in {"lista", "flow"}:
            logger.warning(
                "Tipo no soportado por Instagram; se envía texto de fallback",
                extra={"numero": numero, "tipo_respuesta": tipo_respuesta},
            )
            fallback_text = mensaje or "Por favor responde con texto."
            payload["message"] = {"text": fallback_text}
        else:
            return _fail("Tipo de respuesta no soportado para Instagram.")

        timeout = _instagram_request_timeout(tipo_respuesta, opciones)
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            logger.error("Error enviando solicitud a Instagram API: %s", exc)
            return _fail("No se pudo conectar con la API de Instagram.")

        if not resp.ok:
            if resp.status_code == 504 and tipo_respuesta in {"image", "audio", "video", "document"}:
                logger.warning(
                    "Timeout 504 al enviar media a Instagram; se asume envío exitoso.",
                    extra={"status_code": resp.status_code, "numero": numero},
                )
                stop_typing_feedback(numero)
                message_id = None
                tipo_db = tipo
                if "instagram" not in tipo_db:
                    tipo_db = f"{tipo_db}_instagram"
                if tipo_respuesta in {"image", "audio", "video", "document"} and not tipo_db.endswith(
                    f"_{tipo_respuesta}"
                ):
                    tipo_db = f"{tipo_db}_{tipo_respuesta}"
                guardar_mensaje(
                    numero,
                    mensaje,
                    tipo_db,
                    wa_id=message_id,
                    reply_to_wa_id=reply_to_wa_id,
                    media_id=None,
                    media_url=attachment_url,
                    opciones=_serialize_opciones(opciones) if tipo_respuesta == "boton" else None,
                    step=step,
                    regla_id=regla_id,
                )
                return _result(True)
            error_details = _extract_error_details(resp)
            logger.error(
                "Fallo al enviar mensaje a Instagram API",
                extra={"status_code": resp.status_code, "details": error_details},
            )
            friendly_reason = (
                error_details.get("message")
                or error_details.get("raw_text")
                or "Instagram rechazó el mensaje."
            )
            return _fail(friendly_reason)

        stop_typing_feedback(numero)
        try:
            message_id = resp.json().get("message_id")
        except Exception:
            message_id = None


        tipo_db = tipo
        if "instagram" not in tipo_db:
            tipo_db = f"{tipo_db}_instagram"
        if tipo_respuesta in {"image", "audio", "video", "document"} and not tipo_db.endswith(
            f"_{tipo_respuesta}"
        ):
            tipo_db = f"{tipo_db}_{tipo_respuesta}"

        guardar_mensaje(
            numero,
            mensaje,
            tipo_db,
            wa_id=message_id,
            reply_to_wa_id=reply_to_wa_id,
            media_id=None,
            media_url=attachment_url,
            opciones=_serialize_opciones(opciones) if tipo_respuesta == "boton" else None,
            step=step,
            regla_id=regla_id,
        )
        return _result(True)

    try:
        runtime = _get_runtime_env()
    except RuntimeError as exc:
        logger.error("No se puede enviar mensaje: %s", exc)
        return _result(False, str(exc))

    url = f"{GRAPH_BASE_URL}/{runtime['phone_id']}/messages"
    headers = {
        "Authorization": f"Bearer {runtime['token']}",
        "Content-Type": "application/json"
    }
    media_link = None

    if tipo_respuesta == 'texto':
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    elif tipo_respuesta == 'image':
        media_link = opciones
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "image",
            "image": {
                "link": opciones,
                "caption": mensaje
            }
        }

    elif tipo_respuesta == 'lista':
        try:
            opts = json.loads(opciones) if opciones else {}
        except Exception:
            opts = {}

        if isinstance(opts, list):
            sections = opts
            header = "Menú"
            footer = "Selecciona una opción"
            button = "Ver opciones"
        elif isinstance(opts, dict):
            sections = opts.get("sections", [])
            header = opts.get("header") or "Menú"
            footer = opts.get("footer") or "Selecciona una opción"
            button = opts.get("button") or "Ver opciones"
        else:
            sections = []
            header = "Menú"
            footer = "Selecciona una opción"
            button = "Ver opciones"
        if not sections:
            fallback = mensaje or "No hay opciones disponibles."
            logger.warning(
                "Lista vacía; enviando mensaje de texto de fallback", extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                }
            )
            return enviar_mensaje(
                numero,
                fallback,
                tipo,
                'texto',
                None,
                reply_to_wa_id,
                return_error=return_error,
            )

        sections_clean = []
        for sec in sections:
            rows_clean = []
            for row in sec.get("rows", []):
                row_clean = {k: v for k, v in row.items() if k not in {"step", "next_step"}}
                rows_clean.append(row_clean)
            sec_clean = {k: v for k, v in sec.items() if k != "rows"}
            sec_clean["rows"] = rows_clean
            sections_clean.append(sec_clean)

        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": header},
                "body": {"text": mensaje},
                "footer": {"text": footer},
                "action": {
                    "button": button,
                    "sections": sections_clean
                }
            }
        }

    elif tipo_respuesta == 'boton':
        try:
            botones = json.loads(opciones) if opciones else []
        except Exception:
            botones = []
        botones_clean = []
        for b in botones:
            btn_clean = {k: v for k, v in b.items() if k not in {"step", "next_step"}}
            botones_clean.append(btn_clean)
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": mensaje},
                "action": {"buttons": botones_clean}
            }
        }

    elif tipo_respuesta == 'audio':
        media_url_db = None
        if isinstance(opciones, dict):
            media_url_db = opciones.get("link")
            audio_obj = {}
            if opciones.get("id"):
                audio_obj["id"] = opciones["id"]
            if not audio_obj and opciones.get("link"):
                audio_obj["link"] = opciones["link"]
            if "voice" in opciones:
                audio_obj["voice"] = bool(opciones["voice"])
        elif opciones and os.path.isfile(opciones):
            filename   = os.path.basename(opciones)
            public_url = url_for(
                'static',
                filename=tenants.get_uploads_url_path(filename),
                _external=True,
            )
            audio_obj  = {"link": public_url}
            media_url_db = public_url
        else:
            audio_obj = {"link": opciones}
            media_url_db = opciones

        media_link = audio_obj.get("link")
        logger.debug(
            "Preparando payload de audio",
            extra={
                "numero": numero,
                "tipo_respuesta": tipo_respuesta,
                "media_link": media_link,
                "has_media_id": bool(audio_obj.get("id")),
            },
        )
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "audio",
            "audio": audio_obj
        }

    elif tipo_respuesta == 'video':
        if opciones and os.path.isfile(opciones):
            max_bytes = _get_whatsapp_video_limit_bytes()
            try:
                size_bytes = os.path.getsize(opciones)
            except OSError:
                size_bytes = None
            if size_bytes is not None and size_bytes > max_bytes:
                limit_mb = max_bytes // (1024 * 1024)
                return _fail(
                    "El video supera el tamaño máximo permitido por WhatsApp "
                    f"({limit_mb} MB). Comprime el archivo o envíalo como documento."
                )
            filename   = os.path.basename(opciones)
            public_url = url_for(
                'static',
                filename=tenants.get_uploads_url_path(filename),
                _external=True,
            )
            video_obj  = {"link": public_url}
        else:
            video_obj  = {"link": opciones}

        if mensaje:
            video_obj["caption"] = mensaje

        media_link = video_obj.get("link")
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "video",
            "video": video_obj
        }

    elif tipo_respuesta == 'document':
        media_link = opciones
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "document",
            "document": {
                "link": opciones,
                "caption": mensaje
            }
        }

    elif tipo_respuesta == 'flow':
        if isinstance(opciones, str):
            opciones_str = opciones.strip()
            if not opciones_str:
                opts = {}
            else:
                try:
                    opts = json.loads(opciones_str)
                except json.JSONDecodeError as exc:
                    logger.error(
                        "Opciones inválidas para flow",
                        extra={
                            "numero": numero,
                            "tipo_respuesta": tipo_respuesta,
                            "error": str(exc),
                        },
                    )
                    return _fail("Las opciones del flow no tienen un formato válido.")
        elif isinstance(opciones, dict):
            opts = dict(opciones)
        else:
            logger.error(
                "Opciones inválidas para flow",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                },
            )
            return _fail("Las opciones del flow no tienen un formato válido.")

        opts = _normalize_flow_options(opts)

        flow_message_version = opts.get("flow_message_version") or "3"
        flow_cta = opts.get("flow_cta")
        flow_id = opts.get("flow_id")
        flow_name = opts.get("flow_name")

        if not flow_cta:
            logger.error(
                "Falta flow_cta para flow",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                },
            )
            return _fail("Falta la acción (flow_cta) para el mensaje de flow.")

        if bool(flow_id) == bool(flow_name):
            logger.error(
                "Debe proporcionarse únicamente flow_id o flow_name",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                },
            )
            return _fail("Debes indicar solamente flow_id o flow_name, no ambos.")

        parameters = {
            "flow_message_version": flow_message_version,
            "flow_cta": flow_cta,
        }

        if flow_id:
            parameters["flow_id"] = flow_id
        if flow_name:
            parameters["flow_name"] = flow_name

        for key in ("mode", "flow_token", "flow_action", "flow_action_payload"):
            if key in opts and opts[key] is not None:
                parameters[key] = opts[key]

        body_text = (
            opts.get("flow_body")
            or opts.get("body")
            or opts.get("body_text")
            or mensaje
        )
        if not body_text:
            body_text = "Continuemos"

        interactive = {
            "type": "flow",
            "body": {"text": body_text},
            "action": {
                "name": "flow",
                "parameters": parameters,
            },
        }

        header_text = opts.get("flow_header") or opts.get("header")
        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}

        footer_text = opts.get("flow_footer") or opts.get("footer")
        if footer_text:
            interactive["footer"] = {"text": footer_text}

        media_link = None
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": interactive,
        }

    else:
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    if reply_to_wa_id:
        data["context"] = {"message_id": reply_to_wa_id}

    should_emit_typing = (
        _TYPING_ENABLED
        and numero
        and isinstance(tipo, str)
        and tipo.lower().startswith("bot")
    )

    typing_delay = 0.0
    if should_emit_typing:
        text_content = ""
        if isinstance(mensaje, str):
            text_content = mensaje.strip()
        elif mensaje is not None:
            text_content = str(mensaje).strip()

        if text_content:
            typing_delay = max(0.6, min(len(text_content) / 25.0, 2.5))
        else:
            typing_delay = 0.6

        try:
            trigger_typing_indicator(numero, include_read=False)
        except Exception:  # pragma: no cover - envío de typing depende de la API externa
            logger.exception(
                "No se pudo enviar el indicador de escritura",
                extra={"numero": numero, "tipo": tipo},
            )
            typing_delay = 0.0

    # Validar URLs externas antes de enviar a la API de WhatsApp
    if media_link and isinstance(media_link, str) and media_link.startswith(("http://", "https://")):
        validation_response = None
        try:
            validation_response = requests.head(media_link, allow_redirects=True, timeout=5)
            if validation_response.status_code == 405:
                validation_response.close()
                validation_response = requests.get(
                    media_link,
                    allow_redirects=True,
                    timeout=5,
                    stream=True,
                )

            if validation_response.status_code >= 400:
                logger.warning(
                    "Respuesta no exitosa al validar la URL de medios",
                    extra={
                        "numero": numero,
                        "tipo_respuesta": tipo_respuesta,
                        "media_link": media_link,
                        "status_code": validation_response.status_code,
                    },
                )
        except requests.RequestException as exc:
            logger.warning(
                "Error al validar la URL de medios",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                    "media_link": media_link,
                    "error": str(exc),
                },
            )
        finally:
            if validation_response is not None:
                try:
                    validation_response.close()
                except Exception:  # pragma: no cover - close() shouldn't fail
                    pass
    if typing_delay:
        time.sleep(typing_delay)

    resp = requests.post(url, headers=headers, json=data)
    log_payload = {
        "numero": numero,
        "tipo_respuesta": tipo_respuesta,
        "status_code": resp.status_code,
        "response_text": resp.text,
    }
    if not resp.ok:
        error_details = _extract_error_details(resp)
        log_payload["error_details"] = error_details
        reason = error_details.get("message") or error_details.get("raw_text") or resp.text
        logger.error(
            "Error en la respuesta de WhatsApp API: %s",
            (reason or "sin motivo proporcionado"),
            extra=log_payload,
        )

        friendly_reason = None
        if isinstance(error_details, dict):
            code = error_details.get("code")
            message_text = error_details.get("message")
            if code == 131030:
                friendly_reason = (
                    "El número de destino no está en la lista permitida "
                    "de tu número de prueba en Meta."
                )
            elif code == 100:
                friendly_reason = (
                    "Meta rechazó la solicitud por parámetros inválidos. "
                    "Revisa el número y el mensaje citado."
                )
            elif message_text and "anexo" in message_text.lower() and "maior" in message_text.lower():
                limit_mb = _get_whatsapp_video_limit_bytes() // (1024 * 1024)
                friendly_reason = (
                    "El archivo adjunto supera el tamaño permitido por WhatsApp "
                    f"({limit_mb} MB). Comprime el archivo o envíalo como documento."
                )
            elif message_text:
                friendly_reason = message_text
        if not friendly_reason:
            friendly_reason = "La API de WhatsApp rechazó el mensaje."

        return _fail(friendly_reason)
    logger.info("Mensaje enviado a WhatsApp API", extra=log_payload)
    stop_typing_feedback(numero)
    try:
        wa_id = resp.json().get("messages", [{}])[0].get("id")
    except Exception:
        wa_id = None
    tipo_db = tipo
    if tipo_respuesta in {"image", "audio", "video", "document"} and not tipo_db.endswith(
        f"_{tipo_respuesta}"
    ):
        tipo_db = f"{tipo_db}_{tipo_respuesta}"

    media_url_db = None
    if tipo_respuesta == 'video':
        media_url_db = video_obj.get("link")
    elif tipo_respuesta == 'audio':
        if isinstance(opciones, dict):
            media_url_db = opciones.get("link") or opciones.get("id")
        else:
            media_url_db = audio_obj.get("link")
    else:
        media_url_db = opciones if tipo_respuesta != 'boton' else None

    guardar_mensaje(
        numero,
        mensaje,
        tipo_db,
        wa_id=wa_id,
        reply_to_wa_id=reply_to_wa_id,
        media_id=None,
        media_url=media_url_db,
        opciones=_serialize_opciones(opciones) if tipo_respuesta == "boton" else None,
        step=step,
        regla_id=regla_id,
    )
    return _result(True)


def _post_to_messages(payload, log_context):
    try:
        runtime = _get_runtime_env()
    except RuntimeError as exc:
        logger.error("No se puede contactar la API de WhatsApp: %s", exc)
        return False

    messages_url = f"{GRAPH_BASE_URL}/{runtime['phone_id']}/messages"
    headers = {
        "Authorization": f"Bearer {runtime['token']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(messages_url, headers=headers, json=payload, timeout=10)
    except requests.RequestException as exc:
        log_extra = {"error": str(exc)}
        log_extra.update(log_context)
        logger.error("Error enviando solicitud a WhatsApp API", extra=log_extra)
        return False

    log_payload = {
        "status_code": response.status_code,
        "response_text": response.text,
    }
    log_payload.update(log_context)

    if not response.ok:
        error_details = _extract_error_details(response)
        log_payload["error_details"] = error_details
        reason = error_details.get("message") or error_details.get("raw_text") or response.text
        logger.error(
            "Fallo al enviar solicitud a WhatsApp API: %s",
            (reason or "sin motivo proporcionado"),
            extra=log_payload,
        )
        return False

    logger.info("Solicitud a WhatsApp API completada", extra=log_payload)
    return True


def _post_to_messenger(payload, log_context):
    try:
        runtime = _get_messenger_env()
    except RuntimeError as exc:
        logger.error("No se puede contactar la API de Messenger: %s", exc)
        return False

    messages_url = f"{GRAPH_BASE_URL}/{runtime['page_id']}/messages"
    headers = {
        "Authorization": f"Bearer {runtime['token']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(messages_url, headers=headers, json=payload, timeout=10)
    except requests.RequestException as exc:
        log_extra = {"error": str(exc)}
        log_extra.update(log_context)
        logger.error("Error enviando solicitud a Messenger API", extra=log_extra)
        return False

    log_payload = {
        "status_code": response.status_code,
        "response_text": response.text,
    }
    log_payload.update(log_context)

    if not response.ok:
        error_details = _extract_error_details(response)
        log_payload["error_details"] = error_details
        reason = error_details.get("message") or error_details.get("raw_text") or response.text
        logger.error(
            "Fallo al enviar solicitud a Messenger API: %s",
            (reason or "sin motivo proporcionado"),
            extra=log_payload,
        )
        return False

    logger.info("Solicitud a Messenger API completada", extra=log_payload)
    return True


def _post_to_instagram(payload, log_context):
    try:
        runtime = _get_instagram_env()
    except RuntimeError as exc:
        logger.error("No se puede contactar la API de Instagram: %s", exc)
        return False

    messages_url = f"{INSTAGRAM_GRAPH_BASE_URL}/{runtime['page_id']}/messages"
    headers = {
        "Authorization": f"Bearer {runtime['token']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(messages_url, headers=headers, json=payload, timeout=10)
    except requests.RequestException as exc:
        log_extra = {"error": str(exc)}
        log_extra.update(log_context)
        logger.error("Error enviando solicitud a Instagram API", extra=log_extra)
        return False

    log_payload = {
        "status_code": response.status_code,
        "response_text": response.text,
    }
    log_payload.update(log_context)

    if not response.ok:
        error_details = _extract_error_details(response)
        log_payload["error_details"] = error_details
        reason = error_details.get("message") or error_details.get("raw_text") or response.text
        logger.error(
            "Fallo al enviar solicitud a Instagram API: %s",
            (reason or "sin motivo proporcionado"),
            extra=log_payload,
        )
        return False

    logger.info("Solicitud a Instagram API completada", extra=log_payload)
    return True


def _send_read_and_typing(numero, message_id=None, include_read=True, typing_status="typing"):
    if not numero:
        return False

    channel = _resolve_message_channel(numero)
    if channel == "messenger":
        if not _TYPING_ENABLED:
            return True
        if not _messenger_window_open(numero):
            logger.info(
                "Ventana de Messenger cerrada; omitiendo typing",
                extra={"numero": numero, "typing_status": typing_status},
            )
            return False
        sender_action = "typing_on" if typing_status == "typing" else "typing_off"
        if include_read:
            read_payload = {
                "recipient": {"id": numero},
                "sender_action": "mark_seen",
            }
            if not _post_to_messenger(
                read_payload,
                {"numero": numero, "message_id": message_id, "action": "read"},
            ):
                return False

        typing_payload = {
            "recipient": {"id": numero},
            "sender_action": sender_action,
        }
        return _post_to_messenger(
            typing_payload,
            {"numero": numero, "message_id": message_id, "action": "typing", "typing_status": sender_action},
        )
    if channel == "instagram":
        if not _TYPING_ENABLED:
            return True
        return True

    if include_read and message_id:
        read_payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        if not _post_to_messages(read_payload, {"numero": numero, "message_id": message_id, "action": "read"}):
            return False

    if not _TYPING_ENABLED:
        return True

    if include_read and message_id:
        message_id = None
        include_read = False

    typing_payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "typing",
        "typing": {"status": typing_status},
    }

    return _post_to_messages(
        typing_payload,
        {"numero": numero, "message_id": message_id, "action": "typing", "typing_status": typing_status},
    )


def trigger_typing_indicator(numero, message_id=None, include_read=True, typing_status="typing"):
    return _send_read_and_typing(
        numero,
        message_id=message_id,
        include_read=include_read,
        typing_status=typing_status,
    )


def _typing_tick(numero):
    if not _TYPING_ENABLED:
        return

    with _typing_lock:
        session = _typing_sessions.get(numero)
        if not session:
            return
        stop_event = session["stop"]
        message_id = session.get("message_id")
        has_read = session.get("has_read", False)

    if stop_event.is_set():
        return

    include_read = bool(message_id) and not has_read
    _send_read_and_typing(numero, message_id=message_id if include_read else None, include_read=include_read)

    if include_read:
        with _typing_lock:
            session = _typing_sessions.get(numero)
            if session:
                session["has_read"] = True

    with _typing_lock:
        session = _typing_sessions.get(numero)
        if not session or session["stop"].is_set():
            return
        timer = threading.Timer(_TYPING_INTERVAL, _typing_tick, args=(numero,))
        session["timer"] = timer
    timer.start()


def start_typing_feedback(numero, message_id=None):
    if not numero:
        return

    if not _TYPING_ENABLED:
        if message_id:
            _send_read_and_typing(numero, message_id=message_id, include_read=True)
        return

    with _typing_lock:
        _typing_ui_state.add(numero)
    try:
        from services.realtime import emit_typing_update
    except ImportError:  # pragma: no cover - depende de entorno opcional
        emit_typing_update = None
    if emit_typing_update:
        emit_typing_update(numero, True)

    with _typing_lock:
        session = _typing_sessions.get(numero)
        if session:
            timer = session.get("timer")
            if timer:
                timer.cancel()
        else:
            session = {"stop": threading.Event()}
            _typing_sessions[numero] = session
        session["stop"].clear()
        session["message_id"] = message_id
        session["has_read"] = False
        timer = threading.Timer(_TYPING_INITIAL_DELAY, _typing_tick, args=(numero,))
        session["timer"] = timer

    timer.start()


def stop_typing_feedback(numero):
    with _typing_lock:
        _typing_ui_state.discard(numero)
    try:
        from services.realtime import emit_typing_update
    except ImportError:  # pragma: no cover - depende de entorno opcional
        emit_typing_update = None
    if emit_typing_update:
        emit_typing_update(numero, False)

    with _typing_lock:
        session = _typing_sessions.pop(numero, None)

    if not session:
        return

    session["stop"].set()
    timer = session.get("timer")
    if timer:
        timer.cancel()

    _send_read_and_typing(numero, include_read=False, typing_status="paused")


def is_typing_feedback_active(numero):
    if not numero or not _TYPING_ENABLED:
        return False
    with _typing_lock:
        return numero in _typing_ui_state

def get_media_url(media_id):
    runtime = _get_runtime_env()
    resp1 = requests.get(
        f"{GRAPH_BASE_URL}/{media_id}",
        params={"access_token": runtime["token"]}
    )
    resp1.raise_for_status()
    media_url = resp1.json().get("url")

    resp2 = requests.get(
        media_url, headers={"Authorization": f"Bearer {runtime['token']}"}
    )
    resp2.raise_for_status()

    ext = resp2.headers.get("Content-Type", "").split("/")[-1] or "bin"
    filename = f"{media_id}.{ext}"
    path     = os.path.join(runtime["media_root"], filename)
    with open(path, "wb") as f:
        f.write(resp2.content)

    return url_for(
        "static",
        filename=tenants.get_uploads_url_path(filename),
        _external=True,
    )

def _infer_mime_type(ruta_archivo: str) -> str:
    mime_type, _ = mimetypes.guess_type(ruta_archivo)
    if mime_type:
        return mime_type

    extension = Path(ruta_archivo).suffix.lower()
    fallback_by_extension = {
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".webm": "audio/webm",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".amr": "audio/amr",
    }
    mime_type = fallback_by_extension.get(extension)
    if mime_type:
        logger.info(
            "MIME type inferido por extensión",
            extra={"ruta_archivo": ruta_archivo, "mime_type": mime_type},
        )
        return mime_type

    try:
        with open(ruta_archivo, "rb") as archivo:
            header = archivo.read(16)
        if header.startswith(b"OggS"):
            mime_type = "audio/ogg"
        elif header.startswith(b"ID3") or header.startswith(b"\xff\xfb"):
            mime_type = "audio/mpeg"
    except OSError as exc:
        logger.warning(
            "No se pudo leer el archivo para inferir MIME type",
            extra={"ruta_archivo": ruta_archivo, "error": str(exc)},
        )
        mime_type = None

    if mime_type:
        logger.info(
            "MIME type inferido por cabecera del archivo",
            extra={"ruta_archivo": ruta_archivo, "mime_type": mime_type},
        )
        return mime_type

    logger.warning(
        "No se pudo inferir MIME type; se usará 'application/octet-stream'",
        extra={"ruta_archivo": ruta_archivo},
    )
    return "application/octet-stream"


def subir_media(ruta_archivo):
    mime_type = _infer_mime_type(ruta_archivo)

    try:
        runtime = _get_runtime_env()
    except RuntimeError as exc:
        logger.error("No se puede subir media sin credenciales de tenant: %s", exc)
        return None

    url = f"{GRAPH_BASE_URL}/{runtime['phone_id']}/media"
    headers = {"Authorization": f"Bearer {runtime['token']}"}
    data = {
        "messaging_product": "whatsapp",
        "type": mime_type
    }
    with open(ruta_archivo, "rb") as f:
        files = {"file": (os.path.basename(ruta_archivo), f, mime_type)}
        resp = requests.post(url, headers=headers, data=data, files=files)
    resp.raise_for_status()
    return resp.json().get("id")

def download_audio(media_id):
    # sirve tanto para audio como para video
    runtime = _get_runtime_env()
    url_media = f"{GRAPH_BASE_URL}/{media_id}"
    r1        = requests.get(url_media, params={"access_token": runtime['token']})
    r1.raise_for_status()
    media_url = r1.json()["url"]
    r2        = requests.get(
        media_url,
        headers={"Authorization": f"Bearer {runtime['token']}"},
        stream=True,
    )
    r2.raise_for_status()
    return r2.content


class MediaTooLargeError(RuntimeError):
    """Raised when a media file exceeds the maximum allowed size."""


def download_media_to_path(media_id: str, dest_path: str, *, max_bytes: int | None = None) -> int:
    runtime = _get_runtime_env()
    url_media = f"{GRAPH_BASE_URL}/{media_id}"
    r1 = requests.get(url_media, params={"access_token": runtime['token']})
    r1.raise_for_status()
    media_url = r1.json()["url"]

    total_bytes = 0
    with requests.get(
        media_url,
        headers={"Authorization": f"Bearer {runtime['token']}"},
        stream=True,
    ) as r2:
        r2.raise_for_status()
        content_length = r2.headers.get("Content-Length")
        if max_bytes and content_length:
            try:
                if int(content_length) > max_bytes:
                    raise MediaTooLargeError("El archivo supera el tamaño permitido.")
            except ValueError:
                pass

        with open(dest_path, "wb") as f:
            for chunk in r2.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total_bytes += len(chunk)
                if max_bytes and total_bytes > max_bytes:
                    raise MediaTooLargeError("El archivo supera el tamaño permitido.")
                f.write(chunk)

    return total_bytes
