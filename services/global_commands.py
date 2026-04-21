import re

from config import Config
from services.whatsapp_api import enviar_mensaje
from services.normalize_text import normalize_text

# Diccionario que mapea comandos globales con sus handlers
GLOBAL_COMMANDS = {}


def reiniciar_handler(numero, text):
    """Reinicia el flujo para el usuario y ejecuta el paso inicial."""
    from routes.webhook import set_user_step, process_step_chain, _resolve_rule_platform

    set_user_step(numero, Config.INITIAL_STEP)
    enviar_mensaje(numero, "Perfecto, volvamos a empezar.")
    platform = _resolve_rule_platform(numero)
    process_step_chain(numero, 'iniciar', platform=platform)


# Registrar comandos por defecto
for cmd in ['reiniciar', 'volver al inicio', 'inicio', 'iniciar', 'menú', 'menu', 'ayuda']:
    GLOBAL_COMMANDS[normalize_text(cmd)] = reiniciar_handler


def handle_global_command(numero, text):
    """Procesa comandos globales. Devuelve True si se manejó alguno."""
    normalized_text = normalize_text(text)
    for cmd, handler in GLOBAL_COMMANDS.items():
        if re.search(rf"\b{re.escape(cmd)}\b", normalized_text):
            handler(numero, text)
            return True
    return False
