import os
import json
import mimetypes
import requests
from flask import url_for
from config import Config
from services.db import guardar_mensaje

TOKEN    = Config.META_TOKEN
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
        except:
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
        except:
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
        # 'opciones' es la ruta local en static/uploads
        if opciones and os.path.isfile(opciones):
            filename  = os.path.basename(opciones)
            public_url = url_for('static', filename=f'uploads/{filename}', _external=True)
            audio_obj = {"link": public_url}
        else:
            audio_obj = {"link": opciones}

        if mensaje:
            audio_obj["caption"] = mensaje

        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "audio",
            "audio": audio_obj
        }

    else:
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    resp = requests.post(url, headers=headers, json=data)
    print(f"[WA API] {resp.status_code} — {resp.text}")

    # Guardar en BD
    if tipo_respuesta == 'audio':
        guardar_mensaje(
            numero,
            mensaje,
            tipo,
            media_id=None,
            media_url=audio_obj.get("link")
        )
    else:
        guardar_mensaje(
            numero,
            mensaje,
            tipo,
            media_id=None,
            media_url=opciones
        )

def download_audio(media_id):
    # 1) Obtener URL temporal
    url_media = f"https://graph.facebook.com/v19.0/{media_id}"
    resp1     = requests.get(url_media, params={"access_token": TOKEN})
    resp1.raise_for_status()
    media_url = resp1.json().get("url")

    # 2) Descargar bytes
    resp2 = requests.get(media_url, headers={"Authorization": f"Bearer {TOKEN}"}, stream=True)
    resp2.raise_for_status()
    return resp2.content
