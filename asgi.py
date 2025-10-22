# asgi.py
import asyncio
import sys

from app import app as flask_app  # <-- si tu Flask está en app.py y la instancia se llama 'app'
from asgiref.wsgi import WsgiToAsgi

if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        # Compatibilidad con versiones antiguas de Python donde la política no existe.
        pass

asgi_app = WsgiToAsgi(flask_app)
