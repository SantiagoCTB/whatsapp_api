from __future__ import annotations

import os

from flask_socketio import SocketIO

ASYNC_MODE = "asgi" if os.getenv("SOCKETIO_ASGI") == "1" else "threading"
socketio = SocketIO(async_mode=ASYNC_MODE, cors_allowed_origins="*")


def init_app(app):
    socketio.init_app(app)
    return socketio


def _is_socket_ready():
    return socketio.server is not None


def emit_chat_update(numero: str):
    if not _is_socket_ready():
        return
    socketio.emit("chat_update", {"numero": numero}, to=f"chat:{numero}")


def emit_chat_list_update():
    if not _is_socket_ready():
        return
    socketio.emit("chat_list_update", {})


def emit_typing_update(numero: str, is_typing: bool):
    if not _is_socket_ready():
        return
    socketio.emit(
        "typing_update",
        {"numero": numero, "is_typing": bool(is_typing)},
        to=f"chat:{numero}",
    )
