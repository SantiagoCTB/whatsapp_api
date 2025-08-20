import re

from services.db import get_connection
from services.whatsapp_api import enviar_mensaje
from services.normalize_text import normalize_text

# Diccionario que mapea comandos globales con sus handlers
GLOBAL_COMMANDS = {}


def reiniciar_handler(numero, text):
    """Reinicia el flujo para el usuario y envía el mensaje inicial."""
    from routes.webhook import set_user_step  # Evitar dependencias circulares

    set_user_step(numero, 'menu_principal')
    enviar_mensaje(numero, "Perfecto, volvamos a empezar.")

    conn = get_connection(); c = conn.cursor()
    c.execute(
        "SELECT input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword "
        "FROM reglas WHERE step=%s",
        ('menu_principal',)
    )
    reglas = c.fetchall(); conn.close()

    normalized_text = normalize_text(text)
    for input_db, resp, next_step, tipo_resp, opts, rol_kw in reglas:
        triggers = [normalize_text(t.strip()) for t in (input_db or '').split(',')]
        for trigger in triggers:
            if re.search(rf"\b{re.escape(trigger)}\b", normalized_text):
                enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)
                if rol_kw:
                    conn2 = get_connection(); c2 = conn2.cursor()
                    c2.execute("SELECT id FROM roles WHERE keyword=%s", (rol_kw,))
                    role = c2.fetchone()
                    if role:
                        c2.execute(
                            "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                            (numero, role[0])
                        )
                        conn2.commit()
                    conn2.close()
                set_user_step(numero, next_step.strip().lower() if next_step else '')
                break
        else:
            continue
        break


# Registrar comandos por defecto
for cmd in ['reiniciar', 'volver al inicio', 'inicio', 'menú', 'menu', 'ayuda']:
    GLOBAL_COMMANDS[normalize_text(cmd)] = reiniciar_handler


def handle_global_command(numero, text):
    """Procesa comandos globales. Devuelve True si se manejó alguno."""
    normalized_text = normalize_text(text)
    for cmd, handler in GLOBAL_COMMANDS.items():
        if re.search(rf"\b{re.escape(cmd)}\b", normalized_text):
            handler(numero, text)
            return True
    return False
