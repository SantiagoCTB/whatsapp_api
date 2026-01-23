from flask import session
from flask_socketio import emit, join_room, leave_room

from services.db import get_connection
from services.presence import update_user_presence
from services.realtime import socketio
from services.whatsapp_api import trigger_typing_indicator


def _is_authorized_for_chat(numero):
    roles = session.get("roles") or []
    single_role = session.get("rol")
    if not roles and single_role:
        roles = [single_role]
    if isinstance(roles, str):
        roles = [roles]

    if "admin" in roles:
        return True

    if not roles:
        return False

    conn = get_connection()
    try:
        c = conn.cursor()
        placeholders = ",".join(["%s"] * len(roles))
        c.execute(
            f"SELECT id FROM roles WHERE keyword IN ({placeholders})",
            tuple(roles),
        )
        role_ids = [row[0] for row in c.fetchall()]
        if not role_ids:
            return False
        placeholders = ",".join(["%s"] * len(role_ids))
        c.execute(
            f"SELECT 1 FROM chat_roles WHERE numero = %s AND role_id IN ({placeholders}) LIMIT 1",
            (numero, *role_ids),
        )
        return c.fetchone() is not None
    finally:
        conn.close()


@socketio.on("connect")
def handle_connect():
    if "user" not in session:
        return False
    update_user_presence(session.get("user"), is_active=True)
    emit("socket_ready", {"status": "ok"})


@socketio.on("disconnect")
def handle_disconnect():
    update_user_presence(session.get("user"), is_active=False)


@socketio.on("join_chat")
def handle_join_chat(data):
    numero = (data or {}).get("numero")
    if not numero:
        emit("chat_error", {"error": "Número requerido"})
        return
    if not _is_authorized_for_chat(numero):
        emit("chat_error", {"error": "No autorizado"})
        return
    join_room(f"chat:{numero}")
    emit("chat_joined", {"numero": numero})


@socketio.on("leave_chat")
def handle_leave_chat(data):
    numero = (data or {}).get("numero")
    if not numero:
        return
    leave_room(f"chat:{numero}")


@socketio.on("typing_signal")
def handle_typing_signal(data):
    if "user" not in session:
        return False
    payload = data or {}
    numero = payload.get("numero")
    message_id = payload.get("message_id")
    include_read = payload.get("include_read", True)
    if not numero:
        emit("typing_error", {"error": "Número requerido"})
        return
    if not _is_authorized_for_chat(numero):
        emit("typing_error", {"error": "No autorizado"})
        return
    ok = trigger_typing_indicator(
        numero,
        message_id=message_id,
        include_read=bool(include_read),
    )
    if not ok:
        emit("typing_error", {"error": "No se pudo enviar el indicador"})
        return
    emit("typing_ack", {"status": "ok"})
