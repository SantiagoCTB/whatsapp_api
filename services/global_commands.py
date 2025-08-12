from services.db import get_connection
from services.whatsapp_api import enviar_mensaje

# Diccionario que mapea comandos globales con sus handlers
GLOBAL_COMMANDS = {}

def reiniciar_handler(numero):
    """Reinicia el flujo para el usuario y envía el mensaje inicial."""
    # Importación diferida para evitar dependencias circulares
    from routes.webhook import set_user_step

    set_user_step(numero, 'menu_principal')
    enviar_mensaje(numero, "Perfecto, volvamos a empezar.")

    conn = get_connection(); c = conn.cursor()
    c.execute(
        "SELECT respuesta, siguiente_step, tipo, opciones, rol_keyword "
        "FROM reglas WHERE step=%s AND input_text=%s",
        ('menu_principal', 'iniciar')
    )
    row = c.fetchone(); conn.close()
    if row:
        resp, next_step, tipo_resp, opts, rol_kw = row
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
    GLOBAL_COMMANDS[cmd] = reiniciar_handler

def handle_global_command(numero, text):
    """Procesa comandos globales. Devuelve True si se manejó alguno."""
    handler = GLOBAL_COMMANDS.get(text)
    if handler:
        handler(numero)
        return True
    return False
