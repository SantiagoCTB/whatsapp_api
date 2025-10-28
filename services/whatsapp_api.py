import os
import json
import logging
import mimetypes
import threading
import requests
from flask import url_for
from config import Config
from services.db import guardar_mensaje


logger = logging.getLogger(__name__)

TOKEN    = Config.META_TOKEN
PHONE_ID = Config.PHONE_NUMBER_ID
API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v19.0")
GRAPH_BASE_URL = f"https://graph.facebook.com/{API_VERSION}"
MESSAGES_URL = f"{GRAPH_BASE_URL}/{PHONE_ID}/messages"

_typing_lock = threading.Lock()
_typing_sessions = {}
_TYPING_INITIAL_DELAY = 2.0
_TYPING_INTERVAL = 6.0

os.makedirs(Config.MEDIA_ROOT, exist_ok=True)

def enviar_mensaje(numero, mensaje, tipo='bot', tipo_respuesta='texto', opciones=None, reply_to_wa_id=None, step=None, regla_id=None):
    url = MESSAGES_URL
    headers = {
        "Authorization": f"Bearer {TOKEN}",
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
            return enviar_mensaje(numero, fallback, tipo, 'texto', None, reply_to_wa_id)

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

        if mensaje:
            audio_obj["caption"] = mensaje

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
            try:
                opts = json.loads(opciones)
            except json.JSONDecodeError as exc:
                logger.error(
                    "Opciones inválidas para flow",
                    extra={
                        "numero": numero,
                        "tipo_respuesta": tipo_respuesta,
                        "error": str(exc),
                    },
                )
                return False
        elif isinstance(opciones, dict):
            opts = opciones
        else:
            logger.error(
                "Opciones inválidas para flow",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                },
            )
            return False

        flow_message_version = opts.get("flow_message_version", "3")
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
            return False

        if bool(flow_id) == bool(flow_name):
            logger.error(
                "Debe proporcionarse únicamente flow_id o flow_name",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                },
            )
            return False

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

    # Validar URLs externas antes de enviar a la API de WhatsApp
    if media_link and isinstance(media_link, str) and media_link.startswith(('http://', 'https://')):
        try:
            check = requests.head(media_link, allow_redirects=True, timeout=5)
        except requests.RequestException as exc:
            logger.error(
                "Error al validar la URL de medios",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                    "media_link": media_link,
                    "error": str(exc),
                }
            )
            return False
        if check.status_code != 200:
            logger.error(
                "Respuesta no exitosa al validar la URL de medios",
                extra={
                    "numero": numero,
                    "tipo_respuesta": tipo_respuesta,
                    "media_link": media_link,
                    "status_code": check.status_code,
                }
            )
            return False
    resp = requests.post(url, headers=headers, json=data)
    log_payload = {
        "numero": numero,
        "tipo_respuesta": tipo_respuesta,
        "status_code": resp.status_code,
        "response_text": resp.text,
    }
    if not resp.ok:
        logger.error("Error en la respuesta de WhatsApp API", extra=log_payload)
        return False
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
    return True


def _send_read_and_typing(numero, message_id=None, include_read=True, typing_type="text"):
    if not numero:
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": numero,
        "typing_indicator": {"type": typing_type},
    }

    if include_read and message_id:
        payload["status"] = "read"
        payload["message_id"] = message_id

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(MESSAGES_URL, headers=headers, json=payload, timeout=10)
    except requests.RequestException as exc:
        logger.error(
            "Error enviando indicador de escritura/lectura",
            extra={"numero": numero, "message_id": message_id, "error": str(exc)},
        )
        return False

    log_payload = {
        "numero": numero,
        "message_id": message_id,
        "status_code": response.status_code,
        "response_text": response.text,
    }

    if not response.ok:
        logger.error("Fallo al enviar indicador de escritura/lectura", extra=log_payload)
        return False

    logger.info("Indicador de escritura/lectura enviado", extra=log_payload)
    return True


def trigger_typing_indicator(numero, message_id=None, include_read=True, typing_type="text"):
    return _send_read_and_typing(numero, message_id=message_id, include_read=include_read, typing_type=typing_type)


def _typing_tick(numero):
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
        session = _typing_sessions.pop(numero, None)

    if not session:
        return

    session["stop"].set()
    timer = session.get("timer")
    if timer:
        timer.cancel()

def get_media_url(media_id):
    resp1 = requests.get(
        f"{GRAPH_BASE_URL}/{media_id}",
        params={"access_token": TOKEN}
    )
    resp1.raise_for_status()
    media_url = resp1.json().get("url")

    resp2 = requests.get(media_url, headers={"Authorization": f"Bearer {TOKEN}"})
    resp2.raise_for_status()

    ext = resp2.headers.get("Content-Type", "").split("/")[-1] or "bin"
    filename = f"{media_id}.{ext}"
    path     = os.path.join(Config.MEDIA_ROOT, filename)
    with open(path, "wb") as f:
        f.write(resp2.content)

    return url_for("static", filename=f"uploads/{filename}", _external=True)

def subir_media(ruta_archivo):
    mime_type, _ = mimetypes.guess_type(ruta_archivo)
    if not mime_type:
        raise ValueError(f"No se pudo inferir el MIME type de {ruta_archivo}")

    url = f"{GRAPH_BASE_URL}/{PHONE_ID}/media"
    headers = {"Authorization": f"Bearer {TOKEN}"}
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
    url_media = f"{GRAPH_BASE_URL}/{media_id}"
    r1        = requests.get(url_media, params={"access_token": TOKEN})
    r1.raise_for_status()
    media_url = r1.json()["url"]
    r2        = requests.get(media_url, headers={"Authorization": f"Bearer {TOKEN}"}, stream=True)
    r2.raise_for_status()
    return r2.content
