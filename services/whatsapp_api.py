import requests
import os
from config import Config
from services.db import guardar_mensaje

TOKEN = Config.META_TOKEN
PHONE_ID = Config.PHONE_NUMBER_ID

def enviar_mensaje(numero, mensaje, tipo='bot', tipo_respuesta='texto', opciones=None):
    url = f"https://graph.facebook.com/v17.0/{Config.PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {Config.META_TOKEN}",
        "Content-Type": "application/json"
    }

    if tipo_respuesta == 'texto':
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "text",
            "text": {"body": mensaje}
        }

    elif tipo_respuesta == 'boton':
        buttons = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": op}}
                   for i, op in enumerate(opciones[:3])]
        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": mensaje},
                "action": {"buttons": buttons}
            }
        }

    elif tipo_respuesta == 'lista':
        # Construir secciones de una sola con título fijo
        rows = [{
            "id": f"opcion_{i+1}",
            "title": op,
            "description": ""
        } for i, op in enumerate(opciones[:10])]  # WhatsApp permite máx 10 filas por sección

        sections = [{
            "title": "Opciones disponibles",
            "rows": rows
        }]

        data = {
            "messaging_product": "whatsapp",
            "to": numero,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "header": {
                    "type": "text",
                    "text": "Menú"
                },
                "body": {
                    "text": mensaje
                },
                "footer": {
                    "text": "Selecciona una opción"
                },
                "action": {
                    "button": "Ver opciones",
                    "sections": sections
                }
            }
        }

    resp = requests.post(url, headers=headers, json=data)
    print(f"[WhatsApp API] {resp.status_code} — {resp.text}")
    guardar_mensaje(numero, f"[{tipo_respuesta.upper()}] {mensaje}", tipo)
