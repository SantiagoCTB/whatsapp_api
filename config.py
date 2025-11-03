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
                "key": "verde",
                "label": "Verde",
                "color": "#28a745",
                "text_color": "#ffffff",
            },
            {
                "key": "amarillo",
                "label": "Amarillo",
                "color": "#ffc107",
                "text_color": "#2d2d2d",
            },
            {
                "key": "rojo",
                "label": "Rojo",
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

    return sanitized

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY')
    META_TOKEN = os.getenv('META_TOKEN')
    PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
    VERIFY_TOKEN = os.getenv('VERIFY_TOKEN',"my_secret_token")
    SESSION_TIMEOUT = 600
    INITIAL_STEP = os.getenv('INITIAL_STEP', 'menu_principal')
    MAX_TRANSCRIPTION_DURATION_MS = int(os.getenv('MAX_TRANSCRIPTION_DURATION_MS', 60000))
    TRANSCRIPTION_MAX_AVG_TIME_SEC = float(os.getenv('TRANSCRIPTION_MAX_AVG_TIME_SEC', 10))

    DB_HOST     = os.getenv('DB_HOST')
    DB_PORT     = int(os.getenv('DB_PORT', 3306))
    DB_USER     = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_NAME     = os.getenv('DB_NAME')

    BASEDIR    = os.path.dirname(os.path.abspath(__file__))
    _DEFAULT_MEDIA_ROOT = os.path.join(BASEDIR, "static", "uploads")
    MEDIA_ROOT = os.path.abspath(
        os.getenv("MEDIA_ROOT", _DEFAULT_MEDIA_ROOT)
    )
    CHAT_STATE_DEFINITIONS = _load_chat_state_definitions()
