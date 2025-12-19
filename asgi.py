# asgi.py
import asyncio
import logging
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

    try:
        from asyncio import proactor_events
    except ImportError:  # pragma: no cover - solo en entornos sin Proactor
        proactor_events = None

    if proactor_events is not None:
        _original_call_connection_lost = (
            proactor_events._ProactorBasePipeTransport._call_connection_lost
        )

        def _quiet_connection_lost(self, exc):
            """Ignora errores 10054 al cerrar conexiones HTTP abruptas."""

            try:
                return _original_call_connection_lost(self, exc)
            except ConnectionResetError as err:  # pragma: no cover - dependiente de SO
                winerror = getattr(err, "winerror", None)
                errno = getattr(err, "errno", None)
                if winerror == 10054 or errno == 10054:
                    logging.getLogger(__name__).debug(
                        "Conexión reseteada por el cliente al cerrar el transporte; "
                        "error suprimido para evitar ruido en los logs."
                    )
                    return None
                raise

        proactor_events._ProactorBasePipeTransport._call_connection_lost = (
            _quiet_connection_lost
        )

from app import create_app
from services.realtime import socketio

flask_app = create_app()

try:  # pragma: no cover - python-socketio expone ASGIApp
    from socketio import ASGIApp
except ImportError:  # pragma: no cover - fallback a WSGI si falta soporte ASGI
    ASGIApp = None

if ASGIApp is not None:
    asgi_app = ASGIApp(socketio, flask_app)
else:
    try:  # pragma: no cover - la importación falla únicamente si falta la dependencia extra
        from a2wsgi import WSGIMiddleware
    except ImportError:  # pragma: no cover - a2wsgi es parte de requirements pero añadimos un plan B
        from uvicorn.middleware.wsgi import WSGIMiddleware

    asgi_app = WSGIMiddleware(flask_app)
