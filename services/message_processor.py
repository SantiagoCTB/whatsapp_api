"""Utilidades para delegar procesamiento de mensajes evitando ciclos."""

from typing import Any


def handle_text_message(numero: str, texto: str, *args: Any, **kwargs: Any):
    """Delegar al manejador real sin crear importaciones circulares."""
    from routes.webhook import handle_text_message as _handle_text_message

    return _handle_text_message(numero, texto, *args, **kwargs)
