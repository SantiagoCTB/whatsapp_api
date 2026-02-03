import importlib.util
import json
import logging
import mimetypes
import posixpath
import os
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

if importlib.util.find_spec("mysql.connector"):
    from mysql.connector.errors import ProgrammingError
else:  # pragma: no cover - fallback cuando no está instalado el conector
    class ProgrammingError(Exception):
        pass
from config import Config
from services import tenants
from services.assignments import assign_chat_to_non_admin_user
from services.whatsapp_api import (
    enviar_mensaje,
    trigger_typing_indicator,
    is_typing_feedback_active,
    subir_media,
    _resolve_message_channel,
    INSTAGRAM_GRAPH_BASE_URL,
)
from routes.webhook import (
    _schedule_followup_messages,
    clear_chat_runtime_state,
    notify_session_closed,
)
from services.db import (
    get_connection,
    get_chat_state,
    update_chat_state,
    delete_chat_state,
    hide_chat,
    get_chat_state_definitions,
    obtener_ultimo_mensaje_cliente_info,
)
from services.normalize_text import normalize_text
from routes.configuracion import _ensure_ia_config_table

chat_bp = Blueprint('chat', __name__)
logger = logging.getLogger(__name__)

BOGOTA_TZ = ZoneInfo('America/Bogota')
INSTAGRAM_PROFILE_REFRESH = timedelta(hours=24)
MESSENGER_PROFILE_REFRESH = timedelta(hours=24)


def _fetch_instagram_profile(numero: str, access_token: str) -> dict | None:
    url = f"{INSTAGRAM_GRAPH_BASE_URL}/{numero}"
    params = {"fields": "username,profile_pic", "access_token": access_token}
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        logger.warning("No se pudo consultar el perfil de Instagram: %s", exc)
        return None
    if not response.ok:
        logger.warning(
            "Instagram devolvió un error al consultar perfil: %s",
            response.text,
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Respuesta inválida al consultar perfil de Instagram.")
        return None
    if not isinstance(payload, dict):
        return None
    return {
        "username": payload.get("username"),
        "profile_pic": payload.get("profile_pic"),
    }


def _fetch_messenger_profile(numero: str, access_token: str) -> dict | None:
    graph_version = (getattr(Config, "FACEBOOK_GRAPH_API_VERSION", "") or "v19.0").strip()
    url = f"https://graph.facebook.com/{graph_version}/{numero}"
    params = {"fields": "first_name,last_name,profile_pic,name", "access_token": access_token}
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        logger.warning("No se pudo consultar el perfil de Messenger: %s", exc)
        return None
    if not response.ok:
        logger.warning(
            "Messenger devolvió un error al consultar perfil: %s",
            response.text,
        )
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Respuesta inválida al consultar perfil de Messenger.")
        return None
    if not isinstance(payload, dict):
        return None
    first_name = payload.get("first_name")
    last_name = payload.get("last_name")
    name_parts = [part.strip() for part in (first_name, last_name) if part and str(part).strip()]
    username = " ".join(name_parts) if name_parts else (payload.get("name") or None)
    return {
        "username": username,
        "profile_pic": payload.get("profile_pic"),
    }


def _preferred_url_scheme() -> str:
    scheme = tenants.get_runtime_setting(
        "PREFERRED_URL_SCHEME", default=Config.PREFERRED_URL_SCHEME
    )
    if scheme:
        return str(scheme).strip()
    return "https"
MEDIA_ROOT = Config.MEDIA_ROOT

ALLOWED_RULE_TYPES = {
    "texto",
    "boton",
    "lista",
    "flow",
    "image",
    "document",
    "video",
    "audio",
}

LEGACY_STATE_MAP = {
    "sin_regla": "asesor",
    "sin_respuesta": "esperando_respuesta",
    "espera_usuario": "esperando_respuesta",
    "bot": "en_flujo",
    "ia_activa": "en_flujo",
}
EXCLUDED_ROLE_KEYWORDS = {"superadmin", "tiquetes", "soporte"}


EXCLUDED_FLOW_FIELDS = {"flow_token"}


def _media_root():
    return tenants.get_media_root()


def _media_path(filename: str):
    return os.path.join(_media_root(), filename)


def _load_chat_state_definitions(include_hidden: bool = False):
    definitions = get_chat_state_definitions(include_hidden=include_hidden)
    keys = {item["key"] for item in definitions if item.get("key")}
    return definitions, keys


def _session_timeout_seconds() -> int:
    runtime = tenants.get_current_tenant_env()
    timeout = runtime.get("SESSION_TIMEOUT") if runtime else None
    if isinstance(timeout, int):
        return timeout
    try:
        return int(timeout)
    except (TypeError, ValueError):
        return Config.SESSION_TIMEOUT


def _inactive_assignment_seconds() -> int:
    runtime = tenants.get_current_tenant_env()
    timeout = runtime.get("INACTIVE_ASSIGNMENT_SECONDS") if runtime else None
    if isinstance(timeout, int):
        return timeout
    try:
        return int(timeout)
    except (TypeError, ValueError):
        return Config.INACTIVE_ASSIGNMENT_SECONDS


def _maybe_close_expired_session(
    *,
    numero: str,
    step: str | None,
    last_activity: datetime | None,
    stored_estado: str | None,
    timeout_seconds: int,
    now: datetime,
) -> str | None:
    if not step:
        return None
    if not isinstance(last_activity, datetime):
        return None
    if not timeout_seconds or timeout_seconds <= 0:
        return None
    normalized_estado = LEGACY_STATE_MAP.get(stored_estado, stored_estado)
    if normalized_estado == "inactivo":
        return None
    elapsed = (now - last_activity).total_seconds()
    if elapsed <= timeout_seconds:
        return None

    delete_chat_state(numero)
    clear_chat_runtime_state(numero)
    notify_session_closed(numero, origin="timeout")
    return "inactivo"


def _extract_words(text: str) -> list[str]:
    normalized = normalize_text(text)
    if not normalized:
        return []
    return [word for word in normalized.split() if word]


def _get_session_roles(*, include_legacy: bool = True) -> list[str]:
    roles = session.get('roles') or []
    single_role = session.get('rol')
    if not roles and single_role:
        roles = [single_role]
    if isinstance(roles, str):
        roles = [roles]
    if include_legacy and single_role and single_role not in roles:
        roles.append(single_role)
    return [role for role in roles if role]


def _get_role_ids(cursor, roles: list[str]) -> list[int]:
    if not roles:
        return []
    placeholders = ','.join(['%s'] * len(roles))
    cursor.execute(
        f"SELECT id FROM roles WHERE keyword IN ({placeholders})",
        tuple(roles),
    )
    return [row[0] for row in cursor.fetchall() if row and row[0] is not None]


def _get_session_user_id(cursor) -> int | None:
    username = (session.get('user') or '').strip()
    if not username:
        return None
    cursor.execute("SELECT id FROM usuarios WHERE username = %s", (username,))
    row = cursor.fetchone()
    return row[0] if row else None


def _has_chat_access(cursor, numero: str, role_ids: list[int], user_id: int | None) -> bool:
    if not role_ids or user_id is None:
        return False
    placeholders = ','.join(['%s'] * len(role_ids))
    cursor.execute(
        f"""
        SELECT 1
          FROM chat_roles cr
          LEFT JOIN chat_assignments ca
            ON ca.numero = cr.numero
           AND ca.role_id = cr.role_id
         WHERE cr.numero = %s
           AND cr.role_id IN ({placeholders})
           AND (ca.user_id = %s OR ca.user_id IS NULL)
         LIMIT 1
        """,
        (numero, *role_ids, user_id),
    )
    return cursor.fetchone() is not None


def _require_chat_access(cursor, numero: str) -> bool:
    roles = _get_session_roles()
    if 'admin' in roles:
        return True
    role_ids = _get_role_ids(cursor, roles)
    user_id = _get_session_user_id(cursor)
    return _has_chat_access(cursor, numero, role_ids, user_id)


def _select_matching_rule(rules, text_norm: str | None):
    wildcard = None
    for rule in rules:
        input_text = (rule[1] or '').strip()
        if input_text == '*':
            wildcard = rule
            continue
        if input_text and text_norm and normalize_text(input_text) == text_norm:
            return rule
    return wildcard


def _rule_is_invalid(rule, cursor) -> bool:
    tipo = (rule[3] or '').strip().lower()
    if tipo not in ALLOWED_RULE_TYPES:
        return True

    opciones = rule[4]
    if tipo in {"boton", "lista", "flow"} and opciones:
        try:
            if isinstance(opciones, str):
                json.loads(opciones)
        except (TypeError, ValueError):
            return True

    next_steps = [step.strip().lower() for step in (rule[2] or '').split(',') if step.strip()]
    if next_steps:
        placeholders = ','.join(['%s'] * len(next_steps))
        cursor.execute(
            f"SELECT DISTINCT step FROM reglas WHERE step IN ({placeholders})",
            tuple(next_steps),
        )
        existing = {row[0] for row in cursor.fetchall()}
        missing = [step for step in next_steps if step not in existing]
        if missing:
            return True

    return False


def _apply_autocorrections(text: str, matches: list[dict]) -> str:
    if not matches:
        return text

    replacements = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        offset = match.get("offset")
        length = match.get("length")
        options = match.get("replacements") or []
        if (
            not isinstance(offset, int)
            or not isinstance(length, int)
            or not options
            or not isinstance(options, list)
        ):
            continue
        first = options[0] if options else None
        value = first.get("value") if isinstance(first, dict) else None
        if not value or not isinstance(value, str):
            continue
        replacements.append((offset, length, value))

    if not replacements:
        return text

    corrected = text
    for offset, length, value in sorted(replacements, key=lambda item: item[0], reverse=True):
        corrected = corrected[:offset] + value + corrected[offset + length :]
    return corrected


@chat_bp.route('/media/<path:filename>')
def serve_media(filename: str):
    """Sirve archivos multimedia con el *mimetype* correcto.

    Siempre fuerza un ``Content-Type`` basado en la extensión para que los
    navegadores y WhatsApp lo reconozcan como audio reproducible.
    """

    normalized = os.path.normpath(filename).lstrip("/\\")
    default_root = os.path.realpath(_media_root())
    target_path = os.path.realpath(os.path.join(default_root, normalized))

    if not target_path.startswith(default_root):
        return jsonify({'error': 'Ruta no permitida'}), 403

    if not os.path.exists(target_path):
        alt_parts = normalized.split(os.sep, 1)
        if len(alt_parts) > 1:
            candidate_key, candidate_rel = alt_parts
            candidate_root = os.path.realpath(
                tenants.get_media_root(tenant_key=candidate_key)
            )
            candidate_path = os.path.realpath(
                os.path.join(candidate_root, candidate_rel)
            )
            if candidate_path.startswith(candidate_root) and os.path.exists(candidate_path):
                target_path = candidate_path
            else:
                return jsonify({'error': 'Archivo no encontrado'}), 404
        else:
            return jsonify({'error': 'Archivo no encontrado'}), 404

    guessed, _ = mimetypes.guess_type(target_path)
    ext = os.path.splitext(target_path)[1].lower()
    if ext == '.webm':
        guessed = 'audio/webm'
    elif ext == '.ogg':
        guessed = 'audio/ogg'
    elif ext == '.m4a':
        guessed = 'audio/mp4'
    elif ext == '.mp3':
        guessed = 'audio/mpeg'
    mimetype = guessed or 'application/octet-stream'

    return send_file(target_path, mimetype=mimetype)


def _convert_audio_to_mp3(src_path: str):
    if not shutil.which("ffmpeg"):
        return None, "No se encontró ffmpeg para convertir el audio."

    original_name, _ = os.path.splitext(os.path.basename(src_path))
    dest_mp3_name = f"{uuid.uuid4().hex}_{original_name}.mp3"
    dest_mp3_path = os.path.join(_media_root(), dest_mp3_name)

    def _try_convert(cmd, destination):
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0 or not os.path.exists(destination):
            error_output = (result.stderr or result.stdout or "").strip()
            return False, error_output
        return True, None

    mp3_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src_path,
        "-vn",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        "-ac",
        "1",
        "-ar",
        "44100",
        dest_mp3_path,
    ]

    converted, error_output = _try_convert(mp3_cmd, dest_mp3_path)
    if converted:
        return dest_mp3_path, None

    detail = f" Detalle: {error_output}" if error_output else ""
    return None, f"No se pudo convertir el audio a un formato compatible.{detail}"


def _convert_audio_to_m4a(src_path: str):
    if not shutil.which("ffmpeg"):
        return None, "No se encontró ffmpeg para convertir el audio."

    original_name, _ = os.path.splitext(os.path.basename(src_path))
    dest_m4a_name = f"{uuid.uuid4().hex}_{original_name}.m4a"
    dest_m4a_path = os.path.join(_media_root(), dest_m4a_name)

    def _try_convert(cmd, destination):
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0 or not os.path.exists(destination):
            error_output = (result.stderr or result.stdout or "").strip()
            return False, error_output
        return True, None

    m4a_cmd = [
        "ffmpeg",
        "-y",
        "-i",
        src_path,
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "1",
        "-ar",
        "44100",
        dest_m4a_path,
    ]

    converted, error_output = _try_convert(m4a_cmd, dest_m4a_path)
    if converted:
        return dest_m4a_path, None

    detail = f" Detalle: {error_output}" if error_output else ""
    return None, f"No se pudo convertir el audio a un formato compatible.{detail}"


def _is_excluded_flow_key(key):
    """Return True if ``key`` should be hidden from Flow summaries."""
    if key is None:
        return False
    return str(key).lower() in EXCLUDED_FLOW_FIELDS


def _is_empty_flow_value(value):
    """Return True if ``value`` is an empty structure for Flow display."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, dict):
        return all(_is_empty_flow_value(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return all(_is_empty_flow_value(item) for item in value)
    return False


def _to_bogota_iso(value):
    """Return ``value`` converted to America/Bogota ISO string with offset."""
    if not value:
        return None

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    elif not isinstance(value, datetime):
        return value

    if value.tzinfo is None:
        value = value.replace(tzinfo=BOGOTA_TZ)
    else:
        value = value.astimezone(BOGOTA_TZ)
    return value.isoformat()


def _table_exists(cursor, table_name):
    """Return True if the given table exists in the current schema."""
    try:
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        return cursor.fetchone() is not None
    except ProgrammingError:
        # If the SHOW TABLES fails for any reason, behave as if the table is missing.
        return False


def _parse_flow_json(raw_value):
    """Attempt to decode ``raw_value`` as JSON and return the parsed object."""
    if raw_value is None:
        return None
    if isinstance(raw_value, (dict, list)):
        return raw_value
    if isinstance(raw_value, (bytes, bytearray)):
        try:
            raw_value = raw_value.decode('utf-8')
        except Exception:
            raw_value = raw_value.decode('utf-8', errors='ignore')
    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if not raw_value:
            return ''
        try:
            return json.loads(raw_value)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


def _format_flow_value(value):
    """Return a human friendly representation for a Flow field value."""
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'Sí' if value else 'No'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text if text else '—'
    return str(value)


def _flatten_flow_data(value, prefix=None):
    """Flatten a JSON-like structure into labelled key/value summaries."""
    prefix = list(prefix or [])
    items = []
    if isinstance(value, list):
        for idx, item in enumerate(value, start=1):
            label = f'Elemento {idx}' if prefix else f'Respuesta {idx}'
            new_prefix = prefix + [label]
            if isinstance(item, (dict, list)):
                items.extend(_flatten_flow_data(item, new_prefix))
            else:
                items.append({
                    'label': ' › '.join(new_prefix) if new_prefix else label,
                    'value': _format_flow_value(item),
                })
        return items
    if isinstance(value, dict):
        for key, val in value.items():
            if _is_excluded_flow_key(key):
                continue
            nested = _flatten_flow_data(val, prefix + [str(key)])
            if nested:
                items.extend(nested)
        return items
    label = ' › '.join(prefix) if prefix else 'Respuesta'
    items.append({'label': label, 'value': _format_flow_value(value)})
    return items


def _normalize_flow_node(node):
    """Return ``node`` with JSON-like containers normalised for display."""
    if isinstance(node, dict):
        result = {}
        for key, val in node.items():
            if _is_excluded_flow_key(key):
                continue
            normalised_key = str(key)
            normalised_val = _normalize_flow_node(val)
            if _is_empty_flow_value(normalised_val):
                continue
            result[normalised_key] = normalised_val
        return result
    if isinstance(node, list):
        normalised = []
        for item in node:
            normalised_item = _normalize_flow_node(item)
            if _is_empty_flow_value(normalised_item):
                continue
            normalised.append(normalised_item)
        return normalised
    return node


def _parse_flow_segments(raw_message):
    """Split ``raw_message`` into structured/text segments for Flow replies."""
    if not raw_message:
        return []

    if not isinstance(raw_message, str):
        raw_message = str(raw_message)

    segments = []
    for line in raw_message.splitlines():
        trimmed = line.strip()
        if not trimmed:
            continue

        parsed = None
        if trimmed.startswith('{') or trimmed.startswith('['):
            try:
                parsed = json.loads(trimmed)
            except json.JSONDecodeError:
                parsed = None

        if parsed is None:
            segments.append({'kind': 'text', 'content': trimmed})
        else:
            segments.append({'kind': 'data', 'content': _normalize_flow_node(parsed)})

    return segments


def _select_audio_variant(audio_urls):
    """Return the best audio URL available from a dict payload."""
    if not audio_urls:
        return None

    if isinstance(audio_urls, str):
        trimmed = audio_urls.strip()
        return trimmed or None

    if not isinstance(audio_urls, dict):
        return None

    for key in ("audio_mp3_url", "audio_m4a_url", "audio_ogg_url", "audio_url"):
        value = audio_urls.get(key)
        if value is None:
            continue
        value_str = str(value).strip()
        if value_str and value_str.lower() != "none":
            return value_str

    return None


def sanitize_media_url(url):
    """Normaliza URLs de medios para evitar mixed content."""

    if url in (None, ""):
        return url

    uploads_marker = "/static/uploads/"
    try:
        normalized = str(url).strip()
    except Exception:
        return url

    if not normalized:
        return url

    if normalized.startswith("{"):
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and (
            "audio_mp3_url" in parsed or "audio_ogg_url" in parsed or "audio_m4a_url" in parsed
        ):
            preferred = _select_audio_variant(parsed)
            if preferred:
                normalized = str(preferred).strip()

    lower = normalized.lower()

    def _relative_from_uploads(current):
        idx = current.lower().find(uploads_marker)
        if idx != -1:
            filename = current[idx + len(uploads_marker):]
            return f"{uploads_marker}{filename.lstrip('/') }"
        return current

    if lower.startswith("http://"):
        normalized = "https://" + normalized[len("http://"):]
        lower = normalized.lower()

    if "app.whapco.site" in lower and uploads_marker in lower:
        return _relative_from_uploads(normalized)

    if uploads_marker in lower:
        return _relative_from_uploads(normalized)

    if lower.startswith(f"https://app.whapco.site{uploads_marker}"):
        return _relative_from_uploads(normalized)

    if lower.startswith(uploads_marker):
        return normalized

    if not normalized.startswith(("http://", "https://", "/")):
        return f"{uploads_marker}{normalized.lstrip('/') }"

    return normalized


def _extract_flow_segments(raw_message):
    """Return parsed segments enriched with summaries for UI rendering."""
    if not raw_message:
        return []

    segments = _parse_flow_segments(raw_message)
    if not segments:
        return []

    prepared = []
    for segment in segments:
        kind = segment.get('kind')
        content = segment.get('content')
        if kind == 'data':
            if _is_empty_flow_value(content):
                continue
            summary = []
            display = ''
            if isinstance(content, (dict, list)):
                summary = _flatten_flow_data(content)
                display = json.dumps(content, ensure_ascii=False, indent=2)
                if display in ('{}', '[]'):
                    display = ''
            else:
                display = _format_flow_value(content)
            if display:
                display = display.strip()
            prepared.append(
                {
                    'kind': 'data',
                    'content': content,
                    'summary': summary,
                    'display': display or None,
                }
            )
        else:
            text = (content or '').strip()
            if not text:
                continue
            prepared.append({'kind': 'text', 'content': text})

    if not prepared:
        return []

    if len(prepared) == 1 and prepared[0]['kind'] == 'text':
        original = raw_message.strip() if isinstance(raw_message, str) else ''
        if prepared[0]['content'] == original:
            return []

    return prepared


def _is_ai_enabled(cursor) -> bool:
    try:
        if not _table_exists(cursor, 'ia_config'):
            return True

        cursor.execute("SHOW COLUMNS FROM ia_config LIKE 'enabled';")
        has_enabled = cursor.fetchone() is not None
        if not has_enabled:
            return True

        cursor.execute(
            "SELECT enabled FROM ia_config ORDER BY id DESC LIMIT 1"
        )
        row = cursor.fetchone()
        return bool(row[0]) if row else True
    except Exception:
        return True


@chat_bp.route('/')
def index():
    oauth_code = (request.args.get("code") or "").strip()
    if oauth_code:
        return redirect(
            url_for(
                "configuracion.instagram_oauth_callback",
                code=oauth_code,
            )
        )
    # Autenticación
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    roles = _get_session_roles()
    role_ids = _get_role_ids(c, roles)
    role_id = role_ids[0] if role_ids else None
    user_id = _get_session_user_id(c)
    ai_enabled = _is_ai_enabled(c)

    # Lista de chats únicos filtrados por rol
    if 'admin' in roles:
        c.execute(
            "SELECT DISTINCT numero FROM mensajes "
            "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
        )
    else:
        if role_ids and user_id is not None:
            placeholders = ','.join(['%s'] * len(role_ids))
            c.execute(
                f"""
                SELECT DISTINCT m.numero
                FROM mensajes m
                INNER JOIN chat_roles cr ON m.numero = cr.numero
                LEFT JOIN chat_assignments ca
                  ON ca.numero = cr.numero
                 AND ca.role_id = cr.role_id
                WHERE cr.role_id IN ({placeholders})
                  AND (ca.user_id = %s OR ca.user_id IS NULL)
                  AND m.numero NOT IN (SELECT numero FROM hidden_chats)
                """,
                (*role_ids, user_id),
            )
        else:
            numeros = []
    if 'numeros' not in locals():
        numeros = [row[0] for row in c.fetchall()]

    chats = []
    for numero in numeros:
        # Último mensaje para determinar si requiere asesor
        c.execute(
            "SELECT mensaje FROM mensajes WHERE numero = %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (numero,)
        )
        fila = c.fetchone()
        ultimo = fila[0] if fila else ""
        requiere_asesor = "asesor" in ultimo.lower()
        chats.append((numero, requiere_asesor))

    # Botones configurados
    c.execute(
        """
        SELECT b.id, b.mensaje, b.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos
          FROM botones b
          LEFT JOIN boton_medias m ON b.id = m.boton_id
         GROUP BY b.id
         ORDER BY b.id
        """
    )
    botones = c.fetchall()

    # Roles disponibles (excluyendo admin y roles ocultos)
    excluded_role_values = ('admin', *EXCLUDED_ROLE_KEYWORDS)
    placeholders = ','.join(['%s'] * len(excluded_role_values))
    c.execute(
        f"SELECT id, name, keyword FROM roles WHERE keyword NOT IN ({placeholders})",
        excluded_role_values,
    )
    roles_db = c.fetchall()

    c.execute(
        """
        SELECT u.id,
               COALESCE(NULLIF(u.nombre, ''), u.username) AS display_name,
               u.username,
               COALESCE(
                   GROUP_CONCAT(
                       CASE
                           WHEN r.keyword NOT IN ('superadmin', 'tiquetes', 'soporte')
                           THEN r.name
                       END
                       ORDER BY r.name SEPARATOR ', '
                   ),
                   ''
               )
          FROM usuarios u
          LEFT JOIN user_roles ur ON u.id = ur.user_id
          LEFT JOIN roles r ON ur.role_id = r.id
         GROUP BY u.id, u.username, u.nombre
         ORDER BY display_name
        """
    )
    users_db = c.fetchall()

    conn.close()
    chat_state_definitions, _ = _load_chat_state_definitions()
    current_tenant = tenants.get_current_tenant()
    tenant_name = None
    if current_tenant:
        tenant_name = current_tenant.name or current_tenant.tenant_key

    return render_template(
        'index.html',
        chats=chats,
        botones=botones,
        rol=roles[0] if roles else None,
        role_id=role_id,
        roles=roles_db,
        users=users_db,
        chat_state_definitions=chat_state_definitions,
        ai_enabled=ai_enabled,
        tenant_name=tenant_name,
    )


@chat_bp.route('/toggle_ai_enabled', methods=['POST'])
def toggle_ai_enabled():
    if "user" not in session:
        return jsonify({"ok": False, "error": "No autorizado"}), 401
    roles = _get_session_roles()
    if 'admin' not in roles:
        return jsonify({"ok": False, "error": "Solo administradores"}), 403

    payload = request.get_json(silent=True) or {}
    enabled_raw = payload.get("enabled")
    enabled = 1 if str(enabled_raw).lower() in {"1", "true", "t", "yes", "on"} else 0

    conn = get_connection()
    c = conn.cursor()
    try:
        _ensure_ia_config_table(c)
        c.execute("SELECT id FROM ia_config ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        if row:
            c.execute("UPDATE ia_config SET enabled = %s WHERE id = %s", (enabled, row[0]))
        else:
            c.execute("INSERT INTO ia_config (enabled) VALUES (%s)", (enabled,))
        conn.commit()
    finally:
        conn.close()

    return jsonify({"ok": True, "enabled": bool(enabled)})

@chat_bp.route('/get_chat/<numero>')
def get_chat(numero):
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    roles = _get_session_roles()
    is_admin = 'admin' in roles
    role_ids = _get_role_ids(c, roles)
    user_id = _get_session_user_id(c)

    # Verificar que el usuario tenga acceso al número
    if not is_admin:
        if not _has_chat_access(c, numero, role_ids, user_id):
            conn.close()
            return jsonify({'error': 'No autorizado'}), 403
    if _table_exists(c, 'flow_responses'):
        c.execute("""
          SELECT m.mensaje, m.tipo, m.media_url, m.opciones, m.timestamp,
                 m.link_url, m.link_title, m.link_body, m.link_thumb,
                 m.wa_id, m.reply_to_wa_id,
                 r.id AS reply_id,
                 r.mensaje AS reply_text, r.tipo AS reply_tipo, r.media_url AS reply_media_url,
                 fr.flow_name, fr.response_json
          FROM mensajes m
          LEFT JOIN mensajes r ON r.wa_id = m.reply_to_wa_id
          LEFT JOIN flow_responses fr ON fr.wa_id = m.wa_id
          WHERE m.numero = %s
          ORDER BY m.timestamp ASC
        """, (numero,))
    else:
        c.execute("""
          SELECT m.mensaje, m.tipo, m.media_url, m.opciones, m.timestamp,
                 m.link_url, m.link_title, m.link_body, m.link_thumb,
                 m.wa_id, m.reply_to_wa_id,
                 r.id AS reply_id,
                 r.mensaje AS reply_text, r.tipo AS reply_tipo, r.media_url AS reply_media_url,
                 NULL AS flow_name, NULL AS response_json
          FROM mensajes m
          LEFT JOIN mensajes r ON r.wa_id = m.reply_to_wa_id
          WHERE m.numero = %s
          ORDER BY m.timestamp ASC
        """, (numero,))
    mensajes = c.fetchall()
    c.execute("SELECT estado FROM chat_state WHERE numero = %s", (numero,))
    row_estado = c.fetchone()
    estado = row_estado[0] if row_estado else None
    conn.close()

    formatted = []
    for row in mensajes:
        row = list(row)
        row[2] = sanitize_media_url(row[2])  # media_url
        row[4] = _to_bogota_iso(row[4])
        row[5] = sanitize_media_url(row[5])  # link_url
        row[8] = sanitize_media_url(row[8])  # link_thumb
        row[14] = sanitize_media_url(row[14])  # reply_media_url
        mensaje_txt = row[0] or ''
        tipo_msg = row[1] or ''
        segments = _extract_flow_segments(mensaje_txt) if tipo_msg.startswith('cliente') else []
        row.append(segments)
        formatted.append(row)

    typing_active = is_typing_feedback_active(numero)

    return jsonify({'mensajes': formatted, 'is_typing': typing_active, 'estado': estado})


@chat_bp.route('/typing_signal', methods=['POST'])
def typing_signal():
    if "user" not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json(silent=True) or {}
    numero = data.get('numero')
    message_id = data.get('message_id')
    include_read = data.get('include_read', True)

    if not numero:
        return jsonify({'error': 'Número requerido'}), 400

    roles = _get_session_roles()
    if 'admin' not in roles:
        conn = get_connection()
        c = conn.cursor()
        role_ids = _get_role_ids(c, roles)
        user_id = _get_session_user_id(c)
        autorizado = _has_chat_access(c, numero, role_ids, user_id)
        conn.close()
        if not autorizado:
            return jsonify({'error': 'No autorizado'}), 403

    ok = trigger_typing_indicator(
        numero,
        message_id=message_id,
        include_read=bool(include_read),
    )
    if not ok:
        return jsonify({'error': 'No se pudo enviar el indicador'}), 502

    return jsonify({'status': 'ok'}), 200


@chat_bp.route('/autocorrect', methods=['POST'])
def autocorrect():
    if "user" not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json(silent=True) or {}
    original_text = data.get('text') or ''
    if not original_text.strip():
        return jsonify({'text': original_text})

    tool_url = tenants.get_runtime_setting(
        "LANGUAGETOOL_URL", default=Config.LANGUAGETOOL_URL
    )
    language = tenants.get_runtime_setting(
        "LANGUAGETOOL_LANGUAGE", default=Config.LANGUAGETOOL_LANGUAGE
    )
    if not tool_url:
        return jsonify({'text': original_text, 'disabled': True})

    try:
        response = requests.post(
            tool_url,
            data={'language': language or 'es', 'text': original_text},
            timeout=4,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("Error consultando LanguageTool: %s", exc)
        return jsonify({'text': original_text, 'error': 'Servicio no disponible'}), 502

    matches = payload.get('matches') if isinstance(payload, dict) else []
    corrected = _apply_autocorrections(
        original_text, matches if isinstance(matches, list) else []
    )
    return jsonify({'text': corrected})


@chat_bp.route('/respuestas')
def respuestas():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    roles = _get_session_roles()
    is_admin = 'admin' in roles
    role_ids = _get_role_ids(c, roles)
    user_id = _get_session_user_id(c)
    if not is_admin:
        if role_ids and user_id is not None:
            placeholders = ','.join(['%s'] * len(role_ids))
            c.execute(
                f"""
                SELECT DISTINCT m.numero
                  FROM mensajes m
                  JOIN chat_roles cr ON m.numero = cr.numero
                  LEFT JOIN chat_assignments ca
                    ON ca.numero = cr.numero
                   AND ca.role_id = cr.role_id
                 WHERE cr.role_id IN ({placeholders})
                   AND (ca.user_id = %s OR ca.user_id IS NULL)
                   AND m.numero NOT IN (SELECT numero FROM hidden_chats)
                """,
                (*role_ids, user_id),
            )
        else:
            numeros = []
    else:
        c.execute(
            "SELECT DISTINCT numero FROM mensajes "
            "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
        )
    if 'numeros' not in locals():
        numeros = [row[0] for row in c.fetchall()]

    if not is_admin and not numeros and role_ids and user_id is not None:
        placeholders = ','.join(['%s'] * len(role_ids))
        c.execute(
            f"""
            SELECT DISTINCT cr.numero
              FROM chat_roles cr
              LEFT JOIN chat_assignments ca
                ON ca.numero = cr.numero
               AND ca.role_id = cr.role_id
             WHERE cr.role_id IN ({placeholders})
               AND (ca.user_id = %s OR ca.user_id IS NULL)
            """,
            (*role_ids, user_id),
        )
        numeros = [row[0] for row in c.fetchall()]

    flow_rows = []
    has_flow_table = _table_exists(c, 'flow_responses')
    base_query = (
        """
        SELECT fr.numero, fr.flow_name, fr.response_json, fr.wa_id, fr.timestamp,
               m.mensaje AS user_message,
               EXISTS(
                   SELECT 1
                     FROM mensajes ma
                    WHERE ma.numero = fr.numero
                      AND ma.tipo LIKE 'asesor%'
                      AND ma.timestamp > fr.timestamp
               ) AS agent_replied,
               (
                   SELECT ma.timestamp
                     FROM mensajes ma
                    WHERE ma.numero = fr.numero
                      AND ma.tipo LIKE 'asesor%'
                      AND ma.timestamp > fr.timestamp
                    ORDER BY ma.timestamp ASC
                    LIMIT 1
               ) AS agent_reply_timestamp
          FROM flow_responses fr
          LEFT JOIN mensajes m ON m.wa_id = fr.wa_id AND m.tipo = 'cliente'
        """
    )

    if has_flow_table:
        if is_admin:
            c.execute(
                base_query + " ORDER BY fr.timestamp DESC",
            )
            flow_rows = c.fetchall()
        elif numeros:
            formato = ','.join(['%s'] * len(numeros))
            c.execute(
                base_query
                + f" WHERE fr.numero IN ({formato}) ORDER BY fr.timestamp DESC",
                numeros,
            )
            flow_rows = c.fetchall()

    flow_entries = []
    for (
        numero,
        flow_name,
        response_json,
        wa_id,
        timestamp,
        user_message,
        agent_replied,
        agent_reply_timestamp,
    ) in flow_rows:
        parsed_json = _parse_flow_json(response_json)
        normalized_json = _normalize_flow_node(parsed_json)

        summary_items = []
        if isinstance(normalized_json, (dict, list)) and not _is_empty_flow_value(normalized_json):
            summary_items = _flatten_flow_data(normalized_json)
        elif isinstance(normalized_json, str):
            text = normalized_json.strip()
            if text:
                summary_items = [{'label': 'Respuesta', 'value': text}]
        elif normalized_json not in (None, ''):
            summary_items = [{'label': 'Respuesta', 'value': _format_flow_value(normalized_json)}]

        message = None
        if not summary_items:
            raw_message = (response_json or flow_name or '').strip()
            if isinstance(parsed_json, (dict, list)):
                message = 'Sin contenido'
            else:
                message = raw_message
        if not message or message in {'{}', '[]'}:
            message = 'Sin contenido'

        flow_entries.append(
            {
                'numero': numero,
                'timestamp': timestamp,
                'wa_id': wa_id,
                'flow_name': flow_name,
                'summary': summary_items,
                'mensaje': message or 'Sin contenido',
                'user_message': (user_message or '').strip() or None,
                'agent_replied': bool(agent_replied),
                'agent_reply_timestamp': agent_reply_timestamp,
                'raw_json': normalized_json if isinstance(normalized_json, (dict, list)) and not _is_empty_flow_value(normalized_json) else None,
                'segments': _extract_flow_segments(user_message) if user_message else [],
            }
        )

    flow_responses = sorted(
        flow_entries,
        key=lambda item: item['timestamp'] or datetime.min,
        reverse=True,
    )

    conn.close()
    return render_template(
        'respuestas.html',
        flow_responses=flow_responses,
    )

@chat_bp.route('/send_message', methods=['POST'])
def send_message():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    data   = request.get_json()
    numero = data.get('numero')
    texto  = data.get('mensaje')
    tipo_respuesta = data.get('tipo_respuesta', 'texto')
    opciones = data.get('opciones')
    list_header = data.get('list_header')
    list_footer = data.get('list_footer')
    list_button = data.get('list_button')
    sections    = data.get('sections')
    if tipo_respuesta == 'lista':
        if not opciones:
            try:
                sections_data = json.loads(sections) if sections else []
            except Exception:
                sections_data = []
            opts = {
                'header': list_header,
                'footer': list_footer,
                'button': list_button,
                'sections': sections_data
            }
            opciones = json.dumps(opts)
    reply_to_wa_id = data.get('reply_to_wa_id')

    conn = get_connection()
    c    = conn.cursor()

    roles = _get_session_roles()
    is_admin = 'admin' in roles
    autorizado = False

    if is_admin:
        autorizado = True
    else:
        role_ids = _get_role_ids(c, roles)
        user_id = _get_session_user_id(c)
        autorizado = _has_chat_access(c, numero, role_ids, user_id)
    conn.close()
    if not autorizado:
        return jsonify({'error': 'No autorizado'}), 403

    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    last_client_tipo = (last_client_info or {}).get("tipo") or ""
    last_client_tipo_lower = str(last_client_tipo).lower()
    is_messenger_chat = "messenger" in last_client_tipo_lower
    is_instagram_chat = "instagram" in last_client_tipo_lower
    if is_messenger_chat or is_instagram_chat:
        last_client_ts = (last_client_info or {}).get("timestamp")
        if not isinstance(last_client_ts, datetime):
            error_message = (
                'El usuario de Instagram tiene que haber enviado mensajes a esta cuenta antes de escribirle.'
                if is_instagram_chat
                else 'El usuario de Facebook tiene que haber enviado mensajes a esta página antes de escribirle.'
            )
            return jsonify({
                'error': error_message
            }), 400
        elapsed_seconds = (datetime.utcnow() - last_client_ts).total_seconds()
        if elapsed_seconds > 24 * 3600:
            error_message = (
                'El usuario de Instagram tiene que haber enviado mensajes a esta cuenta antes de escribirle.'
                if is_instagram_chat
                else 'El usuario de Facebook tiene que haber enviado mensajes a esta página antes de escribirle.'
            )
            return jsonify({
                'error': error_message
            }), 400

    # Envía por la API y guarda internamente
    ok, error_message = enviar_mensaje(
        numero,
        texto,
        tipo='asesor',
        tipo_respuesta=tipo_respuesta,
        opciones=opciones,
        reply_to_wa_id=reply_to_wa_id,
        return_error=True,
    )
    if not ok:
        return jsonify({'error': error_message or 'No se pudo enviar el mensaje.'}), 400
    row = get_chat_state(numero)
    step = row[0] if row else ''
    current_state = row[2] if row and len(row) > 2 else None
    _schedule_followup_messages(numero, step)
    next_state = 'asesor' if current_state else None
    update_chat_state(numero, step, next_state)
    return jsonify({'status': 'success'}), 200

@chat_bp.route('/get_chat_list')
def get_chat_list():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    search_term = (request.args.get("q") or "").strip()
    search_terms = _extract_words(search_term)

    roles = _get_session_roles()
    is_admin = 'admin' in roles
    role_ids = _get_role_ids(c, roles)
    user_id = _get_session_user_id(c)

    # Únicos números filtrados por rol
    if search_terms:
        if is_admin:
            c.execute(
                "SELECT numero, mensaje FROM mensajes "
                "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
            )
        elif role_ids and user_id is not None:
            placeholders = ','.join(['%s'] * len(role_ids))
            c.execute(
                f"""
                SELECT m.numero, m.mensaje
                FROM mensajes m
                INNER JOIN chat_roles cr ON m.numero = cr.numero
                LEFT JOIN chat_assignments ca
                  ON ca.numero = cr.numero
                 AND ca.role_id = cr.role_id
                WHERE cr.role_id IN ({placeholders})
                  AND (ca.user_id = %s OR ca.user_id IS NULL)
                  AND m.numero NOT IN (SELECT numero FROM hidden_chats)
                """,
                (*role_ids, user_id),
            )
        else:
            numeros = []
        if 'numeros' not in locals():
            chat_words: dict[str, set[str]] = {}
            for numero, mensaje in c.fetchall():
                if not numero or not mensaje:
                    continue
                words = _extract_words(mensaje)
                if not words:
                    continue
                word_set = chat_words.setdefault(numero, set())
                word_set.update(words)
            numeros = [
                numero for numero, words in chat_words.items()
                if all(term in words for term in search_terms)
            ]
    else:
        if is_admin:
            c.execute(
                "SELECT DISTINCT numero FROM mensajes "
                "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
            )
        elif role_ids and user_id is not None:
            placeholders = ','.join(['%s'] * len(role_ids))
            c.execute(
                f"""
                SELECT DISTINCT m.numero
                FROM mensajes m
                INNER JOIN chat_roles cr ON m.numero = cr.numero
                LEFT JOIN chat_assignments ca
                  ON ca.numero = cr.numero
                 AND ca.role_id = cr.role_id
                WHERE cr.role_id IN ({placeholders})
                  AND (ca.user_id = %s OR ca.user_id IS NULL)
                  AND m.numero NOT IN (SELECT numero FROM hidden_chats)
                """,
                (*role_ids, user_id),
            )
        else:
            numeros = []

    if 'numeros' not in locals():
        numeros = [row[0] for row in c.fetchall()]

    chat_state_definitions, _ = _load_chat_state_definitions(include_hidden=True)
    chat_state_def_map = {item["key"]: item for item in chat_state_definitions if item.get("key")}
    timeout_seconds = _session_timeout_seconds()
    inactive_assignment_seconds = _inactive_assignment_seconds()
    now = datetime.utcnow()
    tenant_env = tenants.get_current_tenant_env() or {}
    instagram_token = (tenant_env.get("INSTAGRAM_TOKEN") or "").strip()
    messenger_token = (
        (tenant_env.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
        or (tenant_env.get("MESSENGER_TOKEN") or "").strip()
    )
    refreshed_profiles: set[str] = set()
    refreshed_messenger_profiles: set[str] = set()
    profiles_updated = False
    chats = []
    for numero in numeros:
        # Alias
        c.execute("SELECT nombre FROM alias WHERE numero = %s", (numero,))
        fila = c.fetchone()
        alias = fila[0] if fila else None

        c.execute(
            """
            SELECT username, profile_pic
              FROM chat_profiles
             WHERE numero = %s AND platform = %s
            """,
            (numero, "instagram"),
        )
        fila = c.fetchone()
        instagram_username = fila[0] if fila else None
        instagram_profile_pic = fila[1] if fila else None

        c.execute(
            """
            SELECT username, profile_pic
              FROM chat_profiles
             WHERE numero = %s AND platform = %s
            """,
            (numero, "messenger"),
        )
        fila = c.fetchone()
        messenger_username = fila[0] if fila else None
        messenger_profile_pic = fila[1] if fila else None

        c.execute(
            """
            SELECT ca.user_id, COALESCE(NULLIF(u.nombre, ''), u.username)
              FROM chat_assignments ca
              JOIN usuarios u ON ca.user_id = u.id
             WHERE ca.numero = %s
            """,
            (numero,),
        )
        fila_asignado = c.fetchone()
        asignado_id = fila_asignado[0] if fila_asignado else None
        asignado_nombre = fila_asignado[1] if fila_asignado else None

        # Último mensaje y su timestamp
        c.execute(
            "SELECT mensaje, timestamp, tipo FROM mensajes WHERE numero = %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (numero,)
        )
        fila = c.fetchone()
        last_ts = _to_bogota_iso(fila[1]) if fila and fila[1] else None
        last_ts_raw = fila[1] if fila else None
        ultimo = fila[0] if fila else ""
        last_tipo = fila[2] if fila else None
        requiere_asesor = "asesor" in ultimo.lower()

        c.execute(
            "SELECT link_url FROM mensajes WHERE numero = %s "
            "ORDER BY timestamp ASC, id ASC LIMIT 1",
            (numero,),
        )
        fila = c.fetchone()
        primer_link = fila[0] if fila else ""
        c.execute(
            """
            SELECT tipo FROM mensajes
             WHERE numero = %s
               AND (tipo = 'cliente' OR tipo LIKE 'cliente_%')
             ORDER BY timestamp ASC, id ASC
             LIMIT 1
            """,
            (numero,),
        )
        fila = c.fetchone()
        primer_tipo = fila[0] if fila else ""
        if not primer_link and primer_tipo:
            primer_tipo_lower = str(primer_tipo).lower()
            if "messenger" in primer_tipo_lower:
                primer_link = "messenger"
            elif "instagram" in primer_tipo_lower:
                primer_link = "instagram"
        if not primer_link and last_tipo:
            last_tipo_lower = str(last_tipo).lower()
            if "messenger" in last_tipo_lower:
                primer_link = "messenger"
            elif "instagram" in last_tipo_lower:
                primer_link = "instagram"

        c.execute(
            """
            SELECT username, profile_pic, updated_at
              FROM chat_profiles
             WHERE numero = %s AND platform = %s
            """,
            (numero, "instagram"),
        )
        fila = c.fetchone()
        instagram_username = fila[0] if fila else None
        instagram_profile_pic = fila[1] if fila else None
        profile_updated_at = fila[2] if fila else None
        if (
            primer_link == "instagram"
            and instagram_token
            and numero
            and numero not in refreshed_profiles
        ):
            refresh_needed = False
            if not instagram_username or not instagram_profile_pic:
                refresh_needed = True
            elif profile_updated_at and isinstance(profile_updated_at, datetime):
                age = datetime.utcnow() - profile_updated_at
                if age > INSTAGRAM_PROFILE_REFRESH:
                    refresh_needed = True
            if refresh_needed:
                refreshed_profiles.add(numero)
                profile = _fetch_instagram_profile(numero, instagram_token)
                if profile:
                    instagram_username = profile.get("username") or instagram_username
                    instagram_profile_pic = profile.get("profile_pic") or instagram_profile_pic
                    c.execute(
                        """
                        INSERT INTO chat_profiles (numero, platform, username, profile_pic)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          username = VALUES(username),
                          profile_pic = VALUES(profile_pic),
                          updated_at = CURRENT_TIMESTAMP
                        """,
                        (numero, "instagram", instagram_username, instagram_profile_pic),
                    )
                    profiles_updated = True
                    if (not alias or not str(alias).strip()) and instagram_username:
                        c.execute(
                            """
                            INSERT INTO alias (numero, nombre)
                            VALUES (%s, %s)
                            ON DUPLICATE KEY UPDATE nombre = VALUES(nombre)
                            """,
                            (numero, instagram_username),
                        )
                        alias = instagram_username

        c.execute(
            """
            SELECT username, profile_pic, updated_at
              FROM chat_profiles
             WHERE numero = %s AND platform = %s
            """,
            (numero, "messenger"),
        )
        fila = c.fetchone()
        messenger_username = fila[0] if fila else None
        messenger_profile_pic = fila[1] if fila else None
        messenger_profile_updated_at = fila[2] if fila else None
        if (
            primer_link == "messenger"
            and messenger_token
            and numero
            and numero not in refreshed_messenger_profiles
        ):
            refresh_needed = False
            if not messenger_username or not messenger_profile_pic:
                refresh_needed = True
            elif messenger_profile_updated_at and isinstance(messenger_profile_updated_at, datetime):
                age = datetime.utcnow() - messenger_profile_updated_at
                if age > MESSENGER_PROFILE_REFRESH:
                    refresh_needed = True
            if refresh_needed:
                refreshed_messenger_profiles.add(numero)
                profile = _fetch_messenger_profile(numero, messenger_token)
                if profile:
                    messenger_username = profile.get("username") or messenger_username
                    messenger_profile_pic = profile.get("profile_pic") or messenger_profile_pic
                    c.execute(
                        """
                        INSERT INTO chat_profiles (numero, platform, username, profile_pic)
                        VALUES (%s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                          username = VALUES(username),
                          profile_pic = VALUES(profile_pic),
                          updated_at = CURRENT_TIMESTAMP
                        """,
                        (numero, "messenger", messenger_username, messenger_profile_pic),
                    )
                    profiles_updated = True
                    if (not alias or not str(alias).strip()) and messenger_username:
                        c.execute(
                            """
                            INSERT INTO alias (numero, nombre)
                            VALUES (%s, %s)
                            ON DUPLICATE KEY UPDATE nombre = VALUES(nombre)
                            """,
                            (numero, messenger_username),
                        )
                        alias = messenger_username

        # Roles asociados al número y nombre/keyword
        c.execute(
            """
            SELECT GROUP_CONCAT(cr.role_id) AS ids,
                   GROUP_CONCAT(COALESCE(r.keyword, r.name) ORDER BY r.id) AS nombres
            FROM chat_roles cr
            LEFT JOIN roles r ON cr.role_id = r.id
            WHERE cr.numero = %s
            """,
            (numero,),
        )
        fila_roles = c.fetchone()
        roles = fila_roles[0] if fila_roles else None
        nombres_roles = fila_roles[1] if fila_roles else None
        role_ids_for_chat = [int(role_id) for role_id in roles.split(',')] if roles else []
        role_keywords = [n.strip() for n in nombres_roles.split(',')] if nombres_roles else []
        role_keywords = [kw for kw in role_keywords if kw and kw not in EXCLUDED_ROLE_KEYWORDS]
        inicial_rol = role_keywords[0][0].upper() if role_keywords else None

        # Estado actual del chat
        c.execute(
            "SELECT step, last_activity, estado FROM chat_state WHERE numero = %s",
            (numero,),
        )
        fila_estado = c.fetchone()
        step = fila_estado[0] if fila_estado else None
        last_activity = fila_estado[1] if fila_estado else None
        stored_estado = fila_estado[2] if fila_estado else None

        estado = None
        closed_estado = _maybe_close_expired_session(
            numero=numero,
            step=step,
            last_activity=last_activity,
            stored_estado=stored_estado,
            timeout_seconds=timeout_seconds,
            now=now,
        )
        if closed_estado:
            estado = closed_estado
            stored_estado = closed_estado
            step = None
        else:
            inactivity_reference = last_activity or last_ts_raw
            if (
                inactivity_reference
                and timeout_seconds
                and timeout_seconds > 0
                and isinstance(inactivity_reference, datetime)
            ):
                elapsed = (now - inactivity_reference).total_seconds()
                if elapsed > timeout_seconds:
                    estado = "inactivo"

        if not estado:
            if stored_estado in LEGACY_STATE_MAP:
                stored_estado = LEGACY_STATE_MAP[stored_estado]

            if stored_estado:
                estado = stored_estado
            elif requiere_asesor:
                estado = "asesor"
            elif last_tipo and str(last_tipo).startswith("bot"):
                estado = "esperando_respuesta"
            elif last_tipo and str(last_tipo).startswith("asesor"):
                estado = "asesor"
            elif last_tipo and str(last_tipo).startswith("cliente"):
                if not step:
                    estado = "asesor"
                else:
                    c.execute(
                        """
                        SELECT id, input_text, siguiente_step, tipo, opciones
                          FROM reglas
                         WHERE step = %s
                         ORDER BY id
                        """,
                        (step,),
                    )
                    rules = c.fetchall()
                    if not rules:
                        estado = "asesor"
                    else:
                        text_norm = normalize_text(ultimo or "")
                        matched_rule = _select_matching_rule(rules, text_norm)
                        if not matched_rule:
                            estado = "asesor"
                        elif _rule_is_invalid(matched_rule, c):
                            estado = "error_flujo"
                        else:
                            estado = "en_flujo"

        inactivity_reference = last_activity or last_ts_raw
        if (
            not asignado_id
            and inactivity_reference
            and isinstance(inactivity_reference, datetime)
            and inactive_assignment_seconds > 0
        ):
            elapsed_assign = (now - inactivity_reference).total_seconds()
            if elapsed_assign > inactive_assignment_seconds and estado in {"esperando_respuesta", "en_flujo"}:
                assignment = assign_chat_to_non_admin_user(numero, role_ids_for_chat)
                if assignment:
                    asignado_id = int(assignment["user_id"])
                    asignado_nombre = assignment["username"]

        if asignado_id:
            c.execute(
                """
                SELECT r.keyword
                  FROM user_roles ur
                  JOIN roles r ON ur.role_id = r.id
                 WHERE ur.user_id = %s
                   AND r.keyword NOT IN ('superadmin', 'tiquetes', 'soporte')
                """,
                (asignado_id,),
            )
            assigned_roles = [row[0] for row in c.fetchall() if row and row[0]]
            for keyword in assigned_roles:
                if keyword not in role_keywords:
                    role_keywords.append(keyword)
            if role_keywords and not inicial_rol:
                inicial_rol = role_keywords[0][0].upper()

        estado_def = chat_state_def_map.get(estado) if estado else None

        chats.append({
            "numero": numero,
            "alias":  alias,
            "instagram_username": instagram_username,
            "instagram_profile_pic": instagram_profile_pic,
            "messenger_username": messenger_username,
            "messenger_profile_pic": messenger_profile_pic,
            "assigned_user_id": asignado_id,
            "assigned_user_name": asignado_nombre,
            "asesor": requiere_asesor,
            "roles": roles,
            "roles_kw": role_keywords,
            "inicial_rol": inicial_rol,
            "estado": estado,
            "estado_label": estado_def.get("label") if estado_def else None,
            "estado_color": estado_def.get("color") if estado_def else None,
            "estado_text_color": estado_def.get("text_color") if estado_def else None,
            "last_timestamp": last_ts,
            "last_message": ultimo,
            "first_link_url": primer_link,
        })

    if profiles_updated:
        conn.commit()
    conn.close()
    chats.sort(key=lambda chat: chat["last_timestamp"] or "", reverse=True)
    return jsonify(chats)

@chat_bp.route('/set_alias', methods=['POST'])
def set_alias():
    if "user" not in session:
        return jsonify({"error": "No autorizado"}), 401

    data   = request.get_json()
    numero = data.get('numero')
    nombre = data.get('nombre')

    if not numero:
        return jsonify({"error": "Número requerido"}), 400

    conn = get_connection()
    c    = conn.cursor()
    if not _require_chat_access(c, numero):
        conn.close()
        return jsonify({"error": "No autorizado"}), 403
    c.execute(
        "INSERT INTO alias (numero, nombre) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE nombre = VALUES(nombre)",
        (numero, nombre)
    )
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"}), 200


@chat_bp.route('/finalizar_chat', methods=['POST'])
def finalizar_chat():
    if "user" not in session:
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json() or {}
    numero = data.get('numero')

    if not numero:
        return jsonify({"error": "Número requerido"}), 400

    conn = get_connection()
    c = conn.cursor()
    if not _require_chat_access(c, numero):
        conn.close()
        return jsonify({"error": "No autorizado"}), 403
    conn.close()

    _, chat_state_keys = _load_chat_state_definitions(include_hidden=True)
    if "inactivo" not in chat_state_keys:
        return jsonify({"error": "Estado inactivo no definido"}), 400

    state_row = get_chat_state(numero)
    step = state_row[0] if state_row else None
    update_chat_state(numero, step, "inactivo")
    clear_chat_runtime_state(numero)

    notify_session_closed(numero, origin="manual")

    return jsonify({"status": "ok"}), 200


@chat_bp.route('/delete_chat', methods=['POST'])
def delete_chat():
    if 'user' not in session:
        return jsonify({"error": "No autorizado"}), 403

    data = request.get_json() or {}
    numero = data.get('numero')
    if not numero:
        return jsonify({"error": "Número requerido"}), 400

    conn = get_connection()
    c = conn.cursor()
    if not _require_chat_access(c, numero):
        conn.close()
        return jsonify({"error": "No autorizado"}), 403
    conn.close()

    hide_chat(numero)
    clear_chat_runtime_state(numero)

    return jsonify({"status": "ok"}), 200

@chat_bp.route('/set_chat_state', methods=['POST'])
def set_chat_state():
    if "user" not in session:
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json() or {}
    numero = data.get('numero')
    estado = data.get('estado')

    if not numero:
        return jsonify({"error": "Número requerido"}), 400

    conn = get_connection()
    c = conn.cursor()
    if not _require_chat_access(c, numero):
        conn.close()
        return jsonify({"error": "No autorizado"}), 403
    conn.close()

    if isinstance(estado, str):
        estado = estado.strip().lower()
        if estado == "":
            estado = None
    elif estado is not None:
        estado = None

    _, chat_state_keys = _load_chat_state_definitions(include_hidden=True)
    if estado is not None and estado not in chat_state_keys:
        return jsonify({"error": "Estado no permitido"}), 400

    if estado is None:
        delete_chat_state(numero)
    else:
        state_row = get_chat_state(numero)
        step = state_row[0] if state_row else ''
        update_chat_state(numero, step, estado)

    return jsonify({"status": "ok", "estado": estado}), 200

@chat_bp.route('/assign_chat_role', methods=['POST'])
def assign_chat_role():
    if 'user' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json()
    numero = data.get('numero')
    if not numero:
        return jsonify({'error': 'Número requerido'}), 400
    # "role" es el campo enviado desde el frontend, pero aceptamos
    # opcionalmente "role_kw" para mayor claridad al llamar la API.
    role_kw = data.get('role') or data.get('role_kw')
    if not role_kw:
        return jsonify({'error': 'Rol requerido'}), 400
    if role_kw in EXCLUDED_ROLE_KEYWORDS:
        return jsonify({'error': 'Rol no permitido'}), 400
    action  = data.get('action', 'add')

    conn = get_connection()
    c    = conn.cursor()
    if not _require_chat_access(c, numero):
        conn.close()
        return jsonify({'error': 'No autorizado'}), 403

    c.execute("SELECT id FROM roles WHERE keyword=%s", (role_kw,))
    row = c.fetchone()
    role_id = row[0] if row else None

    status = 'role_not_found'
    if role_id is not None:
        if action == 'remove':
            c.execute(
                "DELETE FROM chat_roles WHERE numero = %s AND role_id = %s",
                (numero, role_id),
            )
            conn.commit()
            status = 'removed' if c.rowcount else 'not_found'
        else:
            c.execute(
                "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                (numero, role_id),
            )
            conn.commit()
            status = 'added' if c.rowcount else 'exists'
    conn.close()

    return jsonify({'status': status})


@chat_bp.route('/assign_chat_user', methods=['POST'])
def assign_chat_user():
    if 'user' not in session:
        return jsonify({'error': 'No autorizado'}), 401

    data = request.get_json() or {}
    numero = data.get('numero')
    action = (data.get('action') or 'assign').strip().lower()
    user_id = data.get('user_id')

    if not numero:
        return jsonify({'error': 'Número requerido'}), 400

    conn = get_connection()
    c = conn.cursor()
    if not _require_chat_access(c, numero):
        conn.close()
        return jsonify({'error': 'No autorizado'}), 403

    if action == 'remove' or not user_id:
        c.execute("DELETE FROM chat_assignments WHERE numero = %s", (numero,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'removed'})

    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        conn.close()
        return jsonify({'error': 'Usuario inválido'}), 400

    c.execute(
        "SELECT id, COALESCE(NULLIF(nombre, ''), username) FROM usuarios WHERE id = %s",
        (user_id_int,),
    )
    user_row = c.fetchone()
    if not user_row:
        conn.close()
        return jsonify({'error': 'Usuario no encontrado'}), 404

    c.execute(
        """
        SELECT r.id
          FROM user_roles ur
          JOIN roles r ON ur.role_id = r.id
         WHERE ur.user_id = %s
           AND r.keyword NOT IN ('superadmin', 'tiquetes', 'soporte')
         ORDER BY r.id ASC
        """,
        (user_id_int,),
    )
    role_rows = c.fetchall()
    if not role_rows:
        conn.close()
        return jsonify({'error': 'El usuario no tiene rol asignado'}), 400
    role_ids = [row[0] for row in role_rows if row and row[0] is not None]
    if not role_ids:
        conn.close()
        return jsonify({'error': 'El usuario no tiene rol asignado'}), 400
    role_id = role_ids[0]

    c.execute("DELETE FROM chat_roles WHERE numero = %s", (numero,))
    for user_role_id in role_ids:
        c.execute(
            "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
            (numero, user_role_id),
        )
    c.execute(
        """
        INSERT INTO chat_assignments (numero, user_id, role_id, assigned_at)
        VALUES (%s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
          user_id = VALUES(user_id),
          role_id = VALUES(role_id),
          assigned_at = VALUES(assigned_at)
        """,
        (numero, user_id_int, role_id),
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'assigned', 'user_id': str(user_row[0]), 'user_name': user_row[1]})

@chat_bp.route('/send_image', methods=['POST'])
def send_image():
    # Validación de sesión
    if 'user' not in session:
        return jsonify({'error':'No autorizado'}), 401

    numero  = request.form.get('numero')
    caption = request.form.get('caption','')
    img     = request.files.get('image')
    origen  = request.form.get('origen', 'asesor')

    # Verificar rol
    roles = _get_session_roles()
    if 'admin' not in roles:
        conn = get_connection()
        c = conn.cursor()
        role_ids = _get_role_ids(c, roles)
        user_id = _get_session_user_id(c)
        autorizado = _has_chat_access(c, numero, role_ids, user_id)
        conn.close()
        if not autorizado:
            return jsonify({'error':'No autorizado'}), 403

    if not numero or not img:
        return jsonify({'error':'Falta número o imagen'}), 400

    # Guarda archivo en disco
    filename = secure_filename(img.filename)
    unique   = f"{uuid.uuid4().hex}_{filename}"
    path = _media_path(unique)
    img.save(path)

    # URL pública
    image_url = url_for(
        'static',
        filename=tenants.get_uploads_url_path(unique),
        _external=True,
        _scheme=_preferred_url_scheme(),
    )

    # Envía la imagen por la API
    tipo_envio = 'bot_image' if origen == 'bot' else 'asesor'
    success, error_reason = enviar_mensaje(
        numero,
        caption,
        tipo=tipo_envio,
        tipo_respuesta='image',
        opciones=image_url,
        return_error=True,
    )
    if not success:
        return jsonify({'error': error_reason or 'No se pudo enviar la imagen.'}), 502
    row = get_chat_state(numero)
    step = row[0] if row else ''
    _schedule_followup_messages(numero, step)
    if origen != 'bot':
        update_chat_state(numero, step, 'asesor')

    return jsonify({'status':'sent_image'}), 200

@chat_bp.route('/send_document', methods=['POST'])
def send_document():
    # Validación de sesión
    if 'user' not in session:
        return jsonify({'error':'No autorizado'}), 401

    numero   = request.form.get('numero')
    caption  = request.form.get('caption','')
    document = request.files.get('document')

    roles = _get_session_roles()
    if 'admin' not in roles:
        conn = get_connection()
        c    = conn.cursor()
        role_ids = _get_role_ids(c, roles)
        user_id = _get_session_user_id(c)
        autorizado = _has_chat_access(c, numero, role_ids, user_id)
        conn.close()
        if not autorizado:
            return jsonify({'error':'No autorizado'}), 403

    if not numero or not document or not document.filename.lower().endswith('.pdf'):
        return jsonify({'error':'Falta número o documento PDF'}), 400

    filename = secure_filename(document.filename)
    unique   = f"{uuid.uuid4().hex}_{filename}"
    path     = _media_path(unique)
    document.save(path)

    doc_url = url_for(
        'static',
        filename=tenants.get_uploads_url_path(unique),
        _external=True,
        _scheme=_preferred_url_scheme(),
    )

    success, error_reason = enviar_mensaje(
        numero,
        caption,
        tipo='bot_document',
        tipo_respuesta='document',
        opciones=doc_url,
        return_error=True,
    )
    if not success:
        return jsonify({'error': error_reason or 'No se pudo enviar el documento.'}), 502
    row = get_chat_state(numero)
    step = row[0] if row else ''
    _schedule_followup_messages(numero, step)
    update_chat_state(numero, step, 'asesor')

    return jsonify({'status':'sent_document'}), 200

@chat_bp.route('/send_audio', methods=['POST'])
def send_audio():
    # Validación de sesión
    if 'user' not in session:
        return jsonify({'error':'No autorizado'}), 401

    numero  = request.form.get('numero')
    caption = request.form.get('caption','')
    audio   = request.files.get('audio')
    origen  = request.form.get('origen', 'asesor')
    channel = _resolve_message_channel(numero) if numero else "whatsapp"
    is_instagram = channel == "instagram"
    is_whatsapp = channel == "whatsapp"

    roles = _get_session_roles()
    if 'admin' not in roles:
        conn = get_connection()
        c = conn.cursor()
        role_ids = _get_role_ids(c, roles)
        user_id = _get_session_user_id(c)
        autorizado = _has_chat_access(c, numero, role_ids, user_id)
        conn.close()
        if not autorizado:
            return jsonify({'error':'No autorizado'}), 403

    if not numero or not audio:
        logger.warning(
            "Solicitud de audio rechazada por datos faltantes",
            extra={"numero": numero, "has_audio": bool(audio)},
        )
        return jsonify({'error':'Falta número o audio'}), 400

    original_filename = audio.filename or ''
    mime_type = (audio.mimetype or '').lower()
    if not mime_type.startswith('audio/'):
        guessed_mime, _ = mimetypes.guess_type(original_filename)
        if guessed_mime:
            mime_type = guessed_mime.lower()

    ext = os.path.splitext(original_filename)[1].lower()
    if not ext and mime_type:
        guessed_ext = mimetypes.guess_extension(mime_type)
        if guessed_ext:
            ext = '.ogg' if guessed_ext == '.oga' else guessed_ext
        elif mime_type in {'audio/webm', 'audio/webm;codecs=opus'}:
            ext = '.webm'
        elif mime_type.startswith('audio/ogg'):
            ext = '.ogg'

    if not mime_type.startswith('audio/'):
        logger.warning(
            "Archivo subido no es audio",
            extra={
                "numero": numero,
                "original_filename": original_filename,
                "mime_type": mime_type,
            },
        )
        return jsonify({'error': 'El archivo subido no parece ser un audio válido'}), 400

    if not ext:
        return jsonify({'error': 'No se pudo determinar el formato del audio. Asegúrate de que el archivo tenga una extensión o tipo válido.'}), 400

    safe_stem = secure_filename(os.path.splitext(original_filename)[0]) or 'grabacion'
    filename = f"{safe_stem}{ext}"
    unique = f"{uuid.uuid4().hex}_{filename}"
    path = _media_path(unique)

    logger.info(
        "Recibido audio para envío",
        extra={
            "numero": numero,
            "original_filename": original_filename,
            "mime_type": mime_type,
            "ext": ext,
            "target_path": path,
            "origen": origen,
        },
    )

    try:
        audio.save(path)
    except Exception:
        logger.exception(
            "No se pudo guardar el audio subido",
            extra={"numero": numero, "path": path, "original_filename": original_filename},
        )
        return jsonify({'error': 'No se pudo guardar el audio. Intenta nuevamente.'}), 500

    if os.path.getsize(path) == 0:
        logger.warning(
            "Archivo de audio vacío tras guardado",
            extra={"numero": numero, "path": path},
        )
        os.remove(path)
        return jsonify({'error': 'El archivo de audio está vacío. Intenta grabar o subirlo de nuevo.'}), 400

    conversion_error = None
    media_id = None
    instagram_supported_exts = {".aac", ".m4a", ".wav", ".mp4"}

    if is_instagram and ext not in instagram_supported_exts:
        converted_path, conversion_error = _convert_audio_to_m4a(path)
        if converted_path:
            logger.info(
                "Audio convertido a m4a para Instagram",
                extra={"numero": numero, "converted_path": converted_path},
            )
            path = converted_path
            unique = os.path.basename(converted_path)
            ext = ".m4a"
            mime_type = "audio/mp4"
    elif is_whatsapp:
        converted_path, conversion_error = _convert_audio_to_mp3(path)
        if converted_path:
            logger.info(
                "Audio convertido a mp3",
                extra={"numero": numero, "converted_path": converted_path},
            )
            path = converted_path
            unique = os.path.basename(converted_path)
            ext = ".mp3"
            mime_type = "audio/mpeg"

    if conversion_error:
        logger.warning(
            "Conversión de audio con advertencias",
            extra={
                "numero": numero,
                "conversion_error": conversion_error,
                "channel": channel,
            },
        )
        try:
            os.remove(path)
        except OSError:
            pass
        return jsonify({'error': conversion_error}), 422

    media_filename = unique or os.path.basename(path)
    if not media_filename:
        logger.error(
            "No se pudo determinar el nombre del archivo de audio",
            extra={"numero": numero, "path": path, "unique": unique},
        )
        return jsonify({'error': 'No se pudo generar la URL del audio.'}), 500

    tenant_key = tenants.get_active_tenant_key()
    if tenant_key:
        media_filename = posixpath.join(tenant_key, media_filename)

    audio_url = url_for(
        'chat.serve_media',
        filename=media_filename,
        _external=True,
        _scheme=_preferred_url_scheme(),
    )
    if ext == ".mp3":
        audio_urls = {"audio_mp3_url": audio_url}
    elif ext == ".m4a":
        audio_urls = {"audio_m4a_url": audio_url}
    elif ext == ".ogg":
        audio_urls = {"audio_ogg_url": audio_url}
    else:
        audio_urls = {"audio_url": audio_url}

    preferred_audio_url = audio_url if is_instagram else _select_audio_variant(audio_urls)

    if is_whatsapp:
        try:
            media_id = subir_media(path)
            logger.info(
                "Audio subido a WhatsApp",
                extra={"numero": numero, "media_id": media_id, "path": path},
            )
        except Exception as exc:
            logger.exception(
                "Fallo al subir audio a WhatsApp",
                extra={"numero": numero, "path": path, "error": str(exc)},
            )
            return jsonify({'error': 'No se pudo subir el audio a WhatsApp.'}), 502

        if not media_id:
            logger.warning(
                "Audio sin media_id tras subida",
                extra={"numero": numero, "path": path},
            )
            return jsonify({'error': 'No se pudo obtener el media_id del audio.'}), 502
    else:
        logger.info(
            "Omitiendo subida a WhatsApp para audio",
            extra={"numero": numero, "channel": channel, "path": path},
        )

    # Envía el audio por la API
    tipo_envio = 'bot_audio' if origen == 'bot' else 'asesor'

    media_caption = ''  # No enviar caption dentro del payload de audio/documento
    audio_payload = {"id": media_id, "link": preferred_audio_url, "voice": True}

    logger.info(
        "Enviando audio por canal",
        extra={
            "numero": numero,
            "tipo_envio": tipo_envio,
            "media_id": media_id,
            "audio_url": preferred_audio_url,
            "audio_urls": audio_urls,
            "caption_in_payload": bool(media_caption),
            "channel": channel,
        },
    )

    success, error_reason = enviar_mensaje(
        numero,
        media_caption,
        tipo=tipo_envio,
        tipo_respuesta='audio',
        opciones=audio_payload,
        return_error=True,
    )
    if not success:
        return jsonify({'error': error_reason or 'No se pudo enviar el audio.'}), 502

    caption_text = caption.strip()
    if caption_text:
        enviar_mensaje(
            numero,
            caption_text,
            tipo=tipo_envio,
            tipo_respuesta='texto',
        )
    row = get_chat_state(numero)
    step = row[0] if row else ''
    _schedule_followup_messages(numero, step)
    if origen != 'bot':
        update_chat_state(numero, step, 'asesor')

    response_payload = {'status': 'sent_audio', 'url': preferred_audio_url, 'urls': audio_urls}
    if conversion_error:
        response_payload['warning'] = conversion_error

    logger.info(
        "Audio enviado correctamente",
        extra={
            "numero": numero,
            "audio_url": preferred_audio_url,
            "audio_urls": audio_urls,
            "media_id": media_id,
            "warnings": response_payload.get("warning"),
        },
    )

    return jsonify(response_payload), 200

@chat_bp.route('/send_video', methods=['POST'])
def send_video():
    if 'user' not in session:
        return jsonify({'error':'No autorizado'}), 401

    numero  = request.form.get('numero')
    caption = request.form.get('caption','')
    video   = request.files.get('video')
    origen  = request.form.get('origen', 'asesor')

    roles = _get_session_roles()
    if 'admin' not in roles:
        conn = get_connection()
        c = conn.cursor()
        role_ids = _get_role_ids(c, roles)
        user_id = _get_session_user_id(c)
        autorizado = _has_chat_access(c, numero, role_ids, user_id)
        conn.close()
        if not autorizado:
            return jsonify({'error':'No autorizado'}), 403

    if not numero or not video:
        return jsonify({'error':'Falta número o video'}), 400

    max_bytes = Config.MAX_VIDEO_BYTES
    try:
        video.stream.seek(0, os.SEEK_END)
        video_size = video.stream.tell()
        video.stream.seek(0)
    except (AttributeError, OSError):
        video_size = None

    if video_size is not None and video_size > max_bytes:
        return (
            jsonify(
                {
                    "error": (
                        f"El video supera el máximo permitido de "
                        f"{Config.MAX_VIDEO_MB} MB."
                    )
                }
            ),
            413,
        )

    filename = secure_filename(video.filename)
    unique   = f"{uuid.uuid4().hex}_{filename}"
    path     = _media_path(unique)
    video.save(path)

    tipo_envio = 'bot_video' if origen == 'bot' else 'asesor'
    if _resolve_message_channel(numero) == "instagram":
        current_tenant = tenants.get_current_tenant()

        def _send_instagram_video_async():
            try:
                if current_tenant:
                    tenants.set_current_tenant(current_tenant)
                else:
                    tenants.clear_current_tenant()
                success, error_reason = enviar_mensaje(
                    numero,
                    caption,
                    tipo=tipo_envio,
                    tipo_respuesta='video',
                    opciones=path,
                    return_error=True,
                )
                if not success:
                    logger.error(
                        "Error enviando video de Instagram",
                        extra={"numero": numero, "error": error_reason},
                    )
            finally:
                tenants.clear_current_tenant()

        threading.Thread(target=_send_instagram_video_async, daemon=True).start()
    else:
        success, error_reason = enviar_mensaje(
            numero,
            caption,
            tipo=tipo_envio,
            tipo_respuesta='video',
            opciones=path,
            return_error=True,
        )
        if not success:
            return jsonify({'error': error_reason or 'No se pudo enviar el video.'}), 502
    row = get_chat_state(numero)
    step = row[0] if row else ''
    _schedule_followup_messages(numero, step)
    if origen != 'bot':
        update_chat_state(numero, step, 'asesor')

    return jsonify({'status':'sent_video'}), 200
