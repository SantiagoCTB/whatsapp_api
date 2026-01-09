import importlib.util
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

if importlib.util.find_spec("mysql.connector"):
    from mysql.connector.errors import ProgrammingError
else:  # pragma: no cover - fallback cuando no está instalado el conector
    class ProgrammingError(Exception):
        pass
from config import Config
from services import tenants
from services.whatsapp_api import (
    enviar_mensaje,
    trigger_typing_indicator,
    is_typing_feedback_active,
    subir_media,
)
from routes.webhook import clear_chat_runtime_state, notify_session_closed
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

chat_bp = Blueprint('chat', __name__)
logger = logging.getLogger(__name__)

BOGOTA_TZ = ZoneInfo('America/Bogota')


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


@chat_bp.route('/media/<path:filename>')
def serve_media(filename: str):
    """Sirve archivos multimedia con el *mimetype* correcto.

    Siempre fuerza un ``Content-Type`` basado en la extensión para que los
    navegadores y WhatsApp lo reconozcan como audio reproducible.
    """

    normalized = os.path.normpath(filename).lstrip("/\\")
    target_path = os.path.realpath(os.path.join(_media_root(), normalized))
    base_root = os.path.realpath(_media_root())

    if not target_path.startswith(base_root):
        return jsonify({'error': 'Ruta no permitida'}), 403

    if not os.path.exists(target_path):
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
    # Autenticación
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    rol  = session.get('rol')
    c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
    row = c.fetchone()
    role_id = row[0] if row else None
    ai_enabled = _is_ai_enabled(c)

    # Lista de chats únicos filtrados por rol
    if rol == 'admin':
        c.execute(
            "SELECT DISTINCT numero FROM mensajes "
            "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
        )
    else:
        c.execute(
            """
            SELECT DISTINCT m.numero
            FROM mensajes m
            INNER JOIN chat_roles cr ON m.numero = cr.numero
            WHERE cr.role_id = %s
              AND m.numero NOT IN (SELECT numero FROM hidden_chats)
            """,
            (role_id,)
        )
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

    # Roles disponibles (excluyendo admin)
    c.execute("SELECT id, name, keyword FROM roles WHERE keyword != 'admin'")
    roles_db = c.fetchall()

    conn.close()
    chat_state_definitions, _ = _load_chat_state_definitions()
    return render_template(
        'index.html',
        chats=chats,
        botones=botones,
        rol=rol,
        role_id=role_id,
        roles=roles_db,
        chat_state_definitions=chat_state_definitions,
        ai_enabled=ai_enabled,
    )

@chat_bp.route('/get_chat/<numero>')
def get_chat(numero):
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    roles = session.get('roles') or []
    single_role = session.get('rol')
    if not roles and single_role:
        roles = [single_role]
    if isinstance(roles, str):
        roles = [roles]

    is_admin = 'admin' in roles
    role_ids = []

    if not is_admin and roles:
        placeholders = ','.join(['%s'] * len(roles))
        c.execute(
            f"SELECT id FROM roles WHERE keyword IN ({placeholders})",
            tuple(roles),
        )
        role_ids = [row[0] for row in c.fetchall()]

    # Verificar que el usuario tenga acceso al número
    if not is_admin:
        if not role_ids:
            conn.close()
            return jsonify({'error': 'No autorizado'}), 403

        placeholders = ','.join(['%s'] * len(role_ids))
        c.execute(
            f"SELECT 1 FROM chat_roles WHERE numero = %s AND role_id IN ({placeholders})",
            (numero, *role_ids),
        )
        if not c.fetchone():
            conn.close()
            return jsonify({'error': 'No autorizado'}), 403
    if _table_exists(c, 'flow_responses'):
        c.execute("""
          SELECT m.mensaje, m.tipo, m.media_url, m.timestamp,
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
          SELECT m.mensaje, m.tipo, m.media_url, m.timestamp,
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
        row[3] = _to_bogota_iso(row[3])
        row[4] = sanitize_media_url(row[4])  # link_url
        row[7] = sanitize_media_url(row[7])  # link_thumb
        row[13] = sanitize_media_url(row[13])  # reply_media_url
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

    rol = session.get('rol')
    if rol != 'admin':
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute(
            "SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s",
            (numero, role_id),
        )
        autorizado = c.fetchone()
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


@chat_bp.route('/respuestas')
def respuestas():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    rol = session.get('rol')
    role_id = None
    if rol != 'admin':
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute(
            """
            SELECT DISTINCT m.numero
              FROM mensajes m
              JOIN chat_roles cr ON m.numero = cr.numero
             WHERE cr.role_id = %s
              AND m.numero NOT IN (SELECT numero FROM hidden_chats)
            """,
            (role_id,),
        )
    else:
        c.execute(
            "SELECT DISTINCT numero FROM mensajes "
            "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
        )
    numeros = [row[0] for row in c.fetchall()]

    if rol != 'admin' and not numeros and role_id is not None:
        c.execute(
            "SELECT DISTINCT numero FROM chat_roles WHERE role_id = %s",
            (role_id,),
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
        if rol == 'admin':
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

    roles = session.get('roles') or []
    single_role = session.get('rol')
    if not roles and single_role:
        roles = [single_role]
    if isinstance(roles, str):
        roles = [roles]

    # Compatibilidad con código antiguo que aún lee ``session['rol']``
    legacy_role = session.get('rol')
    if legacy_role and legacy_role not in roles:
        roles.append(legacy_role)

    is_admin = 'admin' in roles
    autorizado = False

    if is_admin:
        autorizado = True
    elif roles:
        placeholders = ','.join(['%s'] * len(roles))
        c.execute(
            f"SELECT id FROM roles WHERE keyword IN ({placeholders})",
            tuple(roles),
        )
        role_ids = [row[0] for row in c.fetchall() if row and row[0] is not None]

        if role_ids:
            placeholders = ','.join(['%s'] * len(role_ids))
            c.execute(
                f"SELECT 1 FROM chat_roles WHERE numero = %s AND role_id IN ({placeholders}) LIMIT 1",
                (numero, *role_ids),
            )
            autorizado = c.fetchone()
    conn.close()
    if not autorizado:
        return jsonify({'error': 'No autorizado'}), 403

    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    last_client_tipo = (last_client_info or {}).get("tipo") or ""
    is_messenger_chat = "messenger" in str(last_client_tipo).lower()
    if is_messenger_chat:
        last_client_ts = (last_client_info or {}).get("timestamp")
        if not isinstance(last_client_ts, datetime):
            return jsonify({
                'error': 'El usuario de Facebook tiene que haber enviado mensajes a esta página antes de escribirle.'
            }), 400
        elapsed_seconds = (datetime.utcnow() - last_client_ts).total_seconds()
        if elapsed_seconds > 24 * 3600:
            return jsonify({
                'error': 'El usuario de Facebook tiene que haber enviado mensajes a esta página antes de escribirle.'
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
    next_state = 'asesor' if current_state else None
    update_chat_state(numero, step, next_state)
    return jsonify({'status': 'success'}), 200

@chat_bp.route('/get_chat_list')
def get_chat_list():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()

    roles = session.get('roles') or []
    single_role = session.get('rol')
    if not roles and single_role:
        roles = [single_role]
    if isinstance(roles, str):
        roles = [roles]

    is_admin = 'admin' in roles
    role_ids = []

    if not is_admin and roles:
        placeholders = ','.join(['%s'] * len(roles))
        c.execute(
            f"SELECT id FROM roles WHERE keyword IN ({placeholders})",
            tuple(roles),
        )
        role_ids = [row[0] for row in c.fetchall()]

    # Únicos números filtrados por rol
    if is_admin:
        c.execute(
            "SELECT DISTINCT numero FROM mensajes "
            "WHERE numero NOT IN (SELECT numero FROM hidden_chats)"
        )
    elif role_ids:
        placeholders = ','.join(['%s'] * len(role_ids))
        c.execute(
            f"""
            SELECT DISTINCT m.numero
            FROM mensajes m
            INNER JOIN chat_roles cr ON m.numero = cr.numero
            WHERE cr.role_id IN ({placeholders})
              AND m.numero NOT IN (SELECT numero FROM hidden_chats)
            """,
            tuple(role_ids),
        )
    else:
        numeros = []

    if 'numeros' not in locals():
        numeros = [row[0] for row in c.fetchall()]

    chat_state_definitions, _ = _load_chat_state_definitions(include_hidden=True)
    chat_state_def_map = {item["key"]: item for item in chat_state_definitions if item.get("key")}
    timeout_seconds = _session_timeout_seconds()
    now = datetime.utcnow()
    chats = []
    for numero in numeros:
        # Alias
        c.execute("SELECT nombre FROM alias WHERE numero = %s", (numero,))
        fila = c.fetchone()
        alias = fila[0] if fila else None

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
        platform_hint = None
        if primer_tipo:
            primer_tipo_lower = str(primer_tipo).lower()
            if "messenger" in primer_tipo_lower:
                platform_hint = "messenger"
            elif "instagram" in primer_tipo_lower:
                platform_hint = "instagram"
        if not platform_hint and last_tipo:
            last_tipo_lower = str(last_tipo).lower()
            if "messenger" in last_tipo_lower:
                platform_hint = "messenger"
            elif "instagram" in last_tipo_lower:
                platform_hint = "instagram"
        if platform_hint:
            primer_link = platform_hint

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
        role_keywords = [n.strip() for n in nombres_roles.split(',')] if nombres_roles else []
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

            if stored_estado == "asesor" or requiere_asesor:
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
            elif stored_estado:
                estado = stored_estado

        estado_def = chat_state_def_map.get(estado) if estado else None

        chats.append({
            "numero": numero,
            "alias":  alias,
            "asesor": requiere_asesor,
            "roles": roles,
            "roles_kw": role_keywords,
            "inicial_rol": inicial_rol,
            "estado": estado,
            "estado_label": estado_def.get("label") if estado_def else None,
            "estado_color": estado_def.get("color") if estado_def else None,
            "estado_text_color": estado_def.get("text_color") if estado_def else None,
            "last_timestamp": last_ts,
            "first_link_url": primer_link,
        })

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

    conn = get_connection()
    c    = conn.cursor()
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

    delete_chat_state(numero)
    clear_chat_runtime_state(numero)

    notify_session_closed(numero, origin="manual")

    return jsonify({"status": "ok"}), 200


@chat_bp.route('/delete_chat', methods=['POST'])
def delete_chat():
    if 'user' not in session:
        return jsonify({"error": "No autorizado"}), 403

    roles = session.get('roles') or []
    if isinstance(roles, str):
        roles = [roles]
    is_admin = 'admin' in roles
    if not is_admin:
        is_admin = session.get('rol') == 'admin'
    if not is_admin:
        return jsonify({"error": "No autorizado"}), 403

    data = request.get_json() or {}
    numero = data.get('numero')
    if not numero:
        return jsonify({"error": "Número requerido"}), 400

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

    rol = session.get('rol')
    if rol != 'admin':
        return jsonify({'error': 'No autorizado'}), 403

    data = request.get_json()
    numero = data.get('numero')
    # "role" es el campo enviado desde el frontend, pero aceptamos
    # opcionalmente "role_kw" para mayor claridad al llamar la API.
    role_kw = data.get('role') or data.get('role_kw')
    action  = data.get('action', 'add')

    conn = get_connection()
    c    = conn.cursor()

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
    rol = session.get('rol')
    if rol != 'admin':
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute("SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s", (numero, role_id))
        autorizado = c.fetchone()
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
        return jsonify({'error': error_reason or 'No se pudo enviar la imagen a WhatsApp'}), 502
    if origen != 'bot':
        row = get_chat_state(numero)
        step = row[0] if row else ''
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

    rol = session.get('rol')
    if rol != 'admin':
        conn = get_connection()
        c    = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute("SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s", (numero, role_id))
        autorizado = c.fetchone()
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
        return jsonify({'error': error_reason or 'No se pudo enviar el documento a WhatsApp'}), 502
    row = get_chat_state(numero)
    step = row[0] if row else ''
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

    rol = session.get('rol')
    if rol != 'admin':
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute("SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s", (numero, role_id))
        autorizado = c.fetchone()
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

    converted_path, conversion_error = _convert_audio_to_mp3(path)
    if converted_path:
        logger.info(
            "Audio convertido a mp3",
            extra={"numero": numero, "converted_path": converted_path},
        )
        path = converted_path
        unique = os.path.basename(converted_path)
        ext = '.mp3'
        mime_type = 'audio/mpeg'
    if conversion_error:
        logger.warning(
            "Conversión de audio a mp3 con advertencias",
            extra={"numero": numero, "conversion_error": conversion_error},
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

    audio_url = url_for(
        'chat.serve_media',
        filename=media_filename,
        _external=True,
        _scheme=_preferred_url_scheme(),
    )
    audio_urls = {"audio_mp3_url": audio_url}
    preferred_audio_url = _select_audio_variant(audio_urls)

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

    # Envía el audio por la API
    tipo_envio = 'bot_audio' if origen == 'bot' else 'asesor'

    media_caption = ''  # No enviar caption dentro del payload de audio/documento
    audio_payload = {"id": media_id, "link": preferred_audio_url, "voice": True}

    logger.info(
        "Enviando audio por WhatsApp",
        extra={
            "numero": numero,
            "tipo_envio": tipo_envio,
            "media_id": media_id,
            "audio_url": preferred_audio_url,
            "audio_urls": audio_urls,
            "caption_in_payload": bool(media_caption),
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
        return jsonify({'error': error_reason or 'No se pudo enviar el audio a WhatsApp'}), 502

    caption_text = caption.strip()
    if caption_text:
        enviar_mensaje(
            numero,
            caption_text,
            tipo=tipo_envio,
            tipo_respuesta='texto',
        )
    if origen != 'bot':
        row = get_chat_state(numero)
        step = row[0] if row else ''
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

    rol = session.get('rol')
    if rol != 'admin':
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute("SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s", (numero, role_id))
        autorizado = c.fetchone()
        conn.close()
        if not autorizado:
            return jsonify({'error':'No autorizado'}), 403

    if not numero or not video:
        return jsonify({'error':'Falta número o video'}), 400

    filename = secure_filename(video.filename)
    unique   = f"{uuid.uuid4().hex}_{filename}"
    path     = _media_path(unique)
    video.save(path)

    tipo_envio = 'bot_video' if origen == 'bot' else 'asesor'
    success, error_reason = enviar_mensaje(
        numero,
        caption,
        tipo=tipo_envio,
        tipo_respuesta='video',
        opciones=path,
        return_error=True,
    )
    if not success:
        return jsonify({'error': error_reason or 'No se pudo enviar el video a WhatsApp'}), 502
    if origen != 'bot':
        row = get_chat_state(numero)
        step = row[0] if row else ''
        update_chat_state(numero, step, 'asesor')

    return jsonify({'status':'sent_video'}), 200
