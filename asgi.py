# asgi.py
import asyncio
import sys

if sys.platform.startswith("win"):
    try:
        # La política debe configurarse antes de que cualquier otra parte del
        # proceso cree un event loop. En algunos despliegues de Uvicorn en
        # Windows la importación del módulo ASGI se realiza después de que el
        # servidor prepare su bucle por defecto (Proactor), lo que termina
        # provocando errores "CurrentThreadExecutor already quit" al ejecutar
        # aplicaciones WSGI. Establecer la política aquí garantiza que siempre
        # se use ``WindowsSelectorEventLoopPolicy`` antes de importar Flask.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:
        # Compatibilidad con versiones antiguas de Python donde la política no existe.
        pass

from app import app as flask_app  # <-- si tu Flask está en app.py y la instancia se llama 'app'
from asgiref.wsgi import WsgiToAsgi

asgi_app = WsgiToAsgi(flask_app)
