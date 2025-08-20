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
        """
        SELECT r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s AND r.input_text=%s
         GROUP BY r.id
        """,
        ('menu_principal', 'iniciar')
    )
    row = c.fetchone(); conn.close()

    if row:
        resp, next_step, tipo_resp, media_urls, opts, rol_kw = row
        media_list = media_urls.split('||') if media_urls else []
        if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
            enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
            for extra in media_list[1:]:
                enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
        else:
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
