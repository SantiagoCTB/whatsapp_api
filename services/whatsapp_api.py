import os
import json
import logging
import mimetypes
import threading
import time
from typing import Any, Dict, Optional

import requests
from flask import url_for

from config import Config
from services import tenants
from services.db import guardar_mensaje


logger = logging.getLogger(__name__)

API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v19.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{API_VERSION}"

_typing_lock = threading.Lock()
_typing_sessions = {}
_typing_ui_state = set()
_TYPING_INITIAL_DELAY = 2.0
_TYPING_INTERVAL = 6.0
_TYPING_ENABLED = bool(getattr(Config, "ENABLE_TYPING_INDICATOR", False))


def _get_runtime_env():
    env = tenants.get_current_tenant_env()
    token = env.get("META_TOKEN") or Config.META_TOKEN
    phone_id = env.get("PHONE_NUMBER_ID") or Config.PHONE_NUMBER_ID
    media_root = tenants.get_media_root()
    return {
        "token": token,
        "phone_id": phone_id,
        "media_root": media_root,
    }


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
    runtime = _get_runtime_env()
    url = f"{GRAPH_BASE_URL}/{runtime['phone_id']}/messages"
    headers = {
        "Authorization": f"Bearer {runtime['token']}",
        "Content-Type": "application/json"
    }
    media_link = None

    def _result(success, reason=None):
        if return_error:
            return success, reason
        return success

    def _fail(reason=None):
        stop_typing_feedback(numero)
        return _result(False, reason)

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
        if opciones and os.path.isfile(opciones):
            filename   = os.path.basename(opciones)
            public_url = url_for('static', filename=f'uploads/{filename}', _external=True)
            audio_obj  = {"link": public_url}
        else:
            audio_obj = {"link": opciones}

        media_link = audio_obj.get("link")
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "audio",
            "audio": audio_obj
        }

    elif tipo_respuesta == 'video':
        if opciones and os.path.isfile(opciones):
            filename   = os.path.basename(opciones)
            public_url = url_for('static', filename=f'uploads/{filename}', _external=True)
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
    if tipo_respuesta in {"image", "audio", "video", "document"} and "_" not in tipo:
        tipo_db = f"{tipo}_{tipo_respuesta}"

    media_url_db = None
    if tipo_respuesta == 'video':
        media_url_db = video_obj.get("link")
    elif tipo_respuesta == 'audio':
        media_url_db = audio_obj.get("link")
    else:
        media_url_db = opciones

    guardar_mensaje(
        numero,
        mensaje,
        tipo_db,
        wa_id=wa_id,
        reply_to_wa_id=reply_to_wa_id,
        media_id=None,
        media_url=media_url_db,
        step=step,
        regla_id=regla_id,
    )
    return _result(True)


def _post_to_messages(payload, log_context):
    runtime = _get_runtime_env()
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


def _send_read_and_typing(numero, message_id=None, include_read=True, typing_status="typing"):
    if not numero:
        return False

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

    return url_for("static", filename=f"uploads/{filename}", _external=True)

def subir_media(ruta_archivo):
    mime_type, _ = mimetypes.guess_type(ruta_archivo)
    if not mime_type:
        raise ValueError(f"No se pudo inferir el MIME type de {ruta_archivo}")

    runtime = _get_runtime_env()
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
