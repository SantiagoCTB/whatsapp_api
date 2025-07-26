import json
from config import Config
from services.db import guardar_mensaje
import requests
import os
from flask import url_for


TOKEN = Config.META_TOKEN
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
                "action": {
                    "buttons": botones  # debe ser una lista de máx. 3
                }
            }
        }
    
    elif tipo_respuesta == 'image':
        # opciones aquí es la URL pública de la imagen
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "image",
            "image": {"link": opciones}
        }
        resp = requests.post(url, headers=headers, json=data)
        print(f"[WA API] {resp.status_code} — {resp.text}")
        # guardamos también en la base
        guardar_mensaje(numero, mensaje, tipo, media_id=None, media_url=opciones)
        return

    else:
        # fallback por si se configura mal
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    resp = requests.post(url, headers=headers, json=data)
    print(f"[WA API] {resp.status_code} — {resp.text}")

    guardar_mensaje(numero, mensaje, tipo)

def get_media_url(media_id):
    # 1) Obtener la URL temporal del media object
    resp = requests.get(
      f"https://graph.facebook.com/v19.0/{media_id}",
      params={"access_token": Config.META_TOKEN}
    )
    resp.raise_for_status()
    media_url = resp.json().get("url")

    # 2) Descargar el binario
    media_resp = requests.get(media_url, headers={
      "Authorization": f"Bearer {Config.META_TOKEN}"
    })
    media_resp.raise_for_status()

    # 3) Guardar en disco
    ext = media_resp.headers.get("Content-Type", "").split("/")[-1] or "bin"
    filename = f"{media_id}.{ext}"
    path     = os.path.join(Config.UPLOAD_FOLDER, filename)
    with open(path, "wb") as f:
        f.write(media_resp.content)

    # 4) Devolver URL pública
    return url_for("static", filename=f"uploads/{filename}", _external=True)
