import json
from config import Config
from services.db import guardar_mensaje
import requests

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

    else:
        # fallback por si se configura mal
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    if tipo != 'asesor':  # solo enviar a WhatsApp si no es mensaje manual
        resp = requests.post(url, headers=headers, json=data)
        print(f"[WA API] {resp.status_code} — {resp.text}")

    guardar_mensaje(numero, mensaje, tipo)