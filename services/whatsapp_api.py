import json
import os
import mimetypes
import requests
from config import Config
from services.db import guardar_mensaje
from flask import url_for

TOKEN   = Config.META_TOKEN
PHONE_ID = Config.PHONE_NUMBER_ID


def enviar_mensaje(numero, mensaje, tipo='bot', tipo_respuesta='texto', opciones=None):
    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/messages"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json"
    }

    if tipo_respuesta == 'texto':
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    elif tipo_respuesta == 'image':
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
            secciones = json.loads(opciones) if opciones else []
        except Exception as e:
            print(f"Error en JSON de opciones: {e}")
            secciones = []
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {"type": "text", "text": "Menú"},
                "body": {"text": mensaje},
                "footer": {"text": "Selecciona una opción"},
                "action": {
                    "button": "Ver opciones",
                    "sections": secciones
                }
            }
        }

    elif tipo_respuesta == 'boton':
        try:
            botones = json.loads(opciones) if opciones else []
        except Exception as e:
            print(f"Error en JSON de botones: {e}")
            botones = []
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": mensaje},
                "action": {"buttons": botones}
            }
        }

    elif tipo_respuesta == 'audio':
        # Si 'opciones' es ruta a archivo existente, lo subimos primero
        if opciones and os.path.isfile(opciones):
            media_id = subir_media(opciones)
            audio_obj = {"id": media_id}
        else:
            # Tratamos 'opciones' como URL pública del audio
            audio_obj = {"link": opciones}

        # Si mensaje no está vacío, lo usamos como caption
        if mensaje:
            audio_obj["caption"] = mensaje

        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "audio",
            "audio": audio_obj
        }

    else:
        # Fallback a texto
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    resp = requests.post(url, headers=headers, json=data)
    print(f"[WA API] {resp.status_code} — {resp.text}")

    # Guardamos en la BDD
    if tipo_respuesta == 'audio':
        # asumimos media_id en audio_obj o None, media_url None
        guardar_mensaje(numero, mensaje, tipo, media_id=audio_obj.get("id"), media_url=audio_obj.get("link"))
    else:
        # para texto, imagen, lista y botones
        guardar_mensaje(numero, mensaje, tipo, media_id=None, media_url=opciones)


def get_media_url(media_id):
    # 1) Obtener URL temporal
    resp = requests.get(
        f"https://graph.facebook.com/v19.0/{media_id}",
        params={"access_token": TOKEN}
    )
    resp.raise_for_status()
    media_url = resp.json().get("url")

    # 2) Descargar el binario
    media_resp = requests.get(media_url, headers={
        "Authorization": f"Bearer {TOKEN}"
    })
    media_resp.raise_for_status()

    # 3) Guardar en disco
    ext = media_resp.headers.get("Content-Type", "").split("/")[-1] or "bin"
    filename = f"{media_id}.{ext}"
    path     = os.path.join(Config.UPLOAD_FOLDER, filename)
    with open(path, "wb") as f:
        f.write(media_resp.content)

    # 4) Devolver URL pública para servir vía static/uploads/
    return url_for("static", filename=f"uploads/{filename}", _external=True)


def subir_media(ruta_archivo):
    """
    Sube un archivo multimedia (audio, video, etc.) y devuelve el media_id.
    """
    mime_type, _ = mimetypes.guess_type(ruta_archivo)
    if not mime_type:
        raise ValueError(f"No se pudo inferir el MIME type de {ruta_archivo}")

    url = f"https://graph.facebook.com/v19.0/{PHONE_ID}/media"
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
