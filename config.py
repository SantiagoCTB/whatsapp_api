import os
import json
import re


def _load_chat_state_definitions():
    """Return sanitized chat state configuration from environment."""

    raw_definitions = os.getenv('CHAT_STATE_DEFINITIONS')
    try:
        definitions = json.loads(raw_definitions) if raw_definitions else None
    except json.JSONDecodeError:
        definitions = None

    if not definitions:
        definitions = [
            {
                "key": "esperando_respuesta",
                "label": "Esperando respuesta",
                "color": "#f0ad4e",
                "text_color": "#1f1f1f",
            },
            {
                "key": "asesor",
                "label": "Asesor",
                "color": "#28a745",
                "text_color": "#ffffff",
            },
            {
                "key": "en_flujo",
                "label": "En flujo",
                "color": "#0d6efd",
                "text_color": "#ffffff",
            },
            {
                "key": "inactivo",
                "label": "Inactivo",
                "color": "#6c757d",
                "text_color": "#ffffff",
            },
            {
                "key": "error_flujo",
                "label": "Error de flujo",
                "color": "#dc3545",
                "text_color": "#ffffff",
            },
        ]

    sanitized = []
    seen_keys = set()
    for entry in definitions:
        if not isinstance(entry, dict):
            continue

        key = entry.get("key")
        if not isinstance(key, str):
            continue

        normalized_key = re.sub(r"[^a-z0-9_-]+", "_", key.strip().lower()).strip("_")
        if len(normalized_key) > 20:
            normalized_key = normalized_key[:20]
        if not normalized_key or normalized_key in seen_keys:
            continue

        label = entry.get("label")
        if not isinstance(label, str) or not label.strip():
            label = normalized_key.replace("_", " ").title()

        color = entry.get("color")
        text_color = entry.get("text_color")
        color = color.strip() if isinstance(color, str) else ""
        text_color = text_color.strip() if isinstance(text_color, str) else ""

        sanitized.append(
            {
                "key": normalized_key,
                "label": label,
                "color": color or "#666666",
                "text_color": text_color or "#ffffff",
            }
        )
        seen_keys.add(normalized_key)

    required = [
        {
            "key": "asesor",
            "label": "Asesor",
            "color": "#28a745",
            "text_color": "#ffffff",
        },
        {
            "key": "en_flujo",
            "label": "En flujo",
            "color": "#0d6efd",
            "text_color": "#ffffff",
        },
    ]

    for entry in required:
        key = entry["key"]
        if key in seen_keys:
            continue
        sanitized.append(entry)
        seen_keys.add(key)

    return sanitized

class Config:
    # Las sesiones deben expirar al cerrar el navegador (no persistentes).
    SESSION_PERMANENT = False
    SECRET_KEY = os.getenv('SECRET_KEY')
    META_TOKEN = os.getenv('META_TOKEN')
    PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
    VERIFY_TOKEN = os.getenv('VERIFY_TOKEN',"my_secret_token")
    SESSION_TIMEOUT = int(os.getenv('SESSION_TIMEOUT_SECONDS', 1800))
    SESSION_TIMEOUT_MESSAGE = os.getenv(
        'SESSION_TIMEOUT_MESSAGE',
        'Tu sesión ha terminado por inactividad. Hemos reiniciado la conversación.',
    )
    IA_API_TOKEN = os.getenv('IA_API_TOKEN')
    IA_MODEL = os.getenv('IA_MODEL', 'o4-mini')
    IA_SYSTEM_MESSAGE = os.getenv(
        'IA_SYSTEM_MESSAGE',
        (
            'Eres un asesor comercial humano, cálido y conversacional. Respondes en español (Colombia) ' \
            'con tono profesional y cercano. Hablas como si conocieras el portafolio de la empresa. ' \
            'Resume y reformula, evitando bloques largos literales. No reveles la procedencia de la ' \
            'información ni nombres internos. Estructura la respuesta con: un saludo breve; una frase ' \
            'de qué hace la empresa (máximo 20 palabras); 1 a 3 productos relevantes con nombre o ' \
            'modelo, beneficio principal y precio si aplica, usando bullets simples sin tecnicismos; ' \
            'y un cierre con una pregunta para avanzar (ciudad, medidas, disponibilidad, envío o forma ' \
            'de pago). Si faltan datos, pregunta solo lo mínimo necesario. El objetivo es sonar natural ' \
            'y llevar a una siguiente acción como cotizar, confirmar ciudad o agendar asesoría.'
        ),
    )
    PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', '')
    PREFERRED_URL_SCHEME = os.getenv('PREFERRED_URL_SCHEME', 'https')
    IA_HISTORY_LIMIT = int(os.getenv('IA_HISTORY_LIMIT', 30))
    INITIAL_STEP = os.getenv('INITIAL_STEP', 'menu_principal')
    MAX_TRANSCRIPTION_DURATION_MS = int(os.getenv('MAX_TRANSCRIPTION_DURATION_MS', 60000))
    TRANSCRIPTION_MAX_AVG_TIME_SEC = float(os.getenv('TRANSCRIPTION_MAX_AVG_TIME_SEC', 10))
    VOSK_MODEL_PATH = os.getenv('VOSK_MODEL_PATH')

    DB_HOST     = os.getenv('DB_HOST')
    DB_PORT     = int(os.getenv('DB_PORT', 3306))
    DB_USER     = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_ROOT_PASSWORD = os.getenv('DB_ROOT_PASSWORD')
    DB_NAME     = os.getenv('DB_NAME')
    TENANT_HEADER = os.getenv('TENANT_HEADER', 'X-Tenant-ID')
    DEFAULT_TENANT = os.getenv('DEFAULT_TENANT')
    DEFAULT_TENANT_NAME = os.getenv('DEFAULT_TENANT_NAME')
    FACEBOOK_APP_ID = os.getenv('FACEBOOK_APP_ID')
    SIGNUP_FACEBOOK = os.getenv('SIGNUP_FACEBOOK')
    DEFAULT_ADMIN_PASSWORD_HASH = os.getenv(
        'DEFAULT_ADMIN_PASSWORD_HASH',
        'scrypt:32768:8:1$JAUhBgIzT6IIoM5Y$6c5c9870fb039e600a045345fbe67029001173247f3143ef19b94cddd919996a7a82742083aeeb6927591fa2a0d0eb6bb3c4e3501a1964d53f39157d31f81bd4',
    )

    BASEDIR    = os.path.dirname(os.path.abspath(__file__))
    # La app siempre guarda los medios en ``static/uploads`` para que sea
    # sencillo montarlo como volumen persistente en Docker. No permitimos
    # sobrescribir esta ruta vía variable de entorno para evitar que los
    # archivos desaparezcan al recrear el contenedor.
    _DEFAULT_MEDIA_ROOT = os.path.join(BASEDIR, "static", "uploads")
    MEDIA_ROOT = os.path.abspath(_DEFAULT_MEDIA_ROOT)
    CHAT_STATE_DEFINITIONS = _load_chat_state_definitions()
    ENABLE_TYPING_INDICATOR = os.getenv('ENABLE_TYPING_INDICATOR', 'false').strip().lower() in {
        '1',
        'true',
        'yes',
        'on',
    }
