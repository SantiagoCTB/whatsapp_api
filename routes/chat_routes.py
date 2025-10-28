import os
import uuid
import json
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
from werkzeug.utils import secure_filename
from mysql.connector.errors import ProgrammingError
from config import Config
from services.whatsapp_api import enviar_mensaje
from services.db import get_connection, get_chat_state, update_chat_state

chat_bp = Blueprint('chat', __name__)

# Carpeta de subida debe coincidir con la de whatsapp_api
MEDIA_ROOT = Config.MEDIA_ROOT
os.makedirs(Config.MEDIA_ROOT, exist_ok=True)


EXCLUDED_FLOW_FIELDS = {"flow_token"}


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

    # Lista de chats únicos filtrados por rol
    if rol == 'admin':
        c.execute("SELECT DISTINCT numero FROM mensajes")
    else:
        c.execute(
            """
            SELECT DISTINCT m.numero
            FROM mensajes m
            INNER JOIN chat_roles cr ON m.numero = cr.numero
            WHERE cr.role_id = %s
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
    return render_template('index.html', chats=chats, botones=botones, rol=rol, role_id=role_id, roles=roles_db)

@chat_bp.route('/get_chat/<numero>')
def get_chat(numero):
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    rol  = session.get('rol')
    role_id = None
    if rol != 'admin':
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None

    # Verificar que el usuario tenga acceso al número
    if rol != 'admin':
        c.execute(
            "SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s",
            (numero, role_id)
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
    conn.close()

    formatted = []
    for row in mensajes:
        row = list(row)
        mensaje_txt = row[0] or ''
        tipo_msg = row[1] or ''
        segments = _extract_flow_segments(mensaje_txt) if tipo_msg.startswith('cliente') else []
        row.append(segments)
        formatted.append(row)

    return jsonify({'mensajes': formatted})


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
            """,
            (role_id,),
        )
    else:
        c.execute("SELECT DISTINCT numero FROM mensajes")
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
    rol  = session.get('rol')
    role_id = None
    if rol != 'admin':
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None
        c.execute(
            "SELECT 1 FROM chat_roles WHERE numero = %s AND role_id = %s",
            (numero, role_id)
        )
        autorizado = c.fetchone()
    else:
        autorizado = True
    conn.close()
    if not autorizado:
        return jsonify({'error': 'No autorizado'}), 403

    # Envía por la API y guarda internamente
    ok = enviar_mensaje(
        numero,
        texto,
        tipo='asesor',
        tipo_respuesta=tipo_respuesta,
        opciones=opciones,
        reply_to_wa_id=reply_to_wa_id,
    )
    if not ok:
        return jsonify({'error': 'URL no válida'}), 400
    row = get_chat_state(numero)
    step = row[0] if row else ''
    update_chat_state(numero, step, 'asesor')
    return jsonify({'status': 'success'}), 200

@chat_bp.route('/get_chat_list')
def get_chat_list():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c    = conn.cursor()
    rol  = session.get('rol')
    role_id = None
    if rol != 'admin':
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
        row = c.fetchone()
        role_id = row[0] if row else None

    # Únicos números filtrados por rol
    if rol == 'admin':
        c.execute("SELECT DISTINCT numero FROM mensajes")
    else:
        c.execute(
            """
            SELECT DISTINCT m.numero
            FROM mensajes m
            INNER JOIN chat_roles cr ON m.numero = cr.numero
            WHERE cr.role_id = %s
            """,
            (role_id,)
        )
    numeros = [row[0] for row in c.fetchall()]

    chats = []
    for numero in numeros:
        # Alias
        c.execute("SELECT nombre FROM alias WHERE numero = %s", (numero,))
        fila = c.fetchone()
        alias = fila[0] if fila else None

        # Último mensaje y su timestamp
        c.execute(
            "SELECT mensaje, timestamp FROM mensajes WHERE numero = %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (numero,)
        )
        fila = c.fetchone()
        last_ts = fila[1].isoformat() if fila and fila[1] else None
        ultimo = fila[0] if fila else ""
        requiere_asesor = "asesor" in ultimo.lower()

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
        c.execute("SELECT estado FROM chat_state WHERE numero = %s", (numero,))
        fila = c.fetchone()
        estado = fila[0] if fila else None

        chats.append({
            "numero": numero,
            "alias":  alias,
            "asesor": requiere_asesor,
            "roles": roles,
            "roles_kw": role_keywords,
            "inicial_rol": inicial_rol,
            "estado": estado,
            "last_timestamp": last_ts,
        })

    conn.close()
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

@chat_bp.route('/assign_chat_role', methods=['POST'])
def assign_chat_role():
    if 'user' not in session:
        return jsonify({'error': 'No autorizado'}), 401

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
    path = os.path.join(MEDIA_ROOT, unique)
    img.save(path)

    # URL pública
    image_url = url_for('static', filename=f'uploads/{unique}', _external=True)

    # Envía la imagen por la API
    tipo_envio = 'bot_image' if origen == 'bot' else 'asesor'
    enviar_mensaje(
        numero,
        caption,
        tipo=tipo_envio,
        tipo_respuesta='image',
        opciones=image_url
    )
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
    path     = os.path.join(MEDIA_ROOT, unique)
    document.save(path)

    doc_url = url_for('static', filename=f'uploads/{unique}', _external=True)

    enviar_mensaje(
        numero,
        caption,
        tipo='bot_document',
        tipo_respuesta='document',
        opciones=doc_url
    )
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
        return jsonify({'error':'Falta número o audio'}), 400

    # Guarda archivo en disco
    filename = secure_filename(audio.filename)
    unique   = f"{uuid.uuid4().hex}_{filename}"
    path = os.path.join(MEDIA_ROOT, unique)
    audio.save(path)

    # Envía el audio por la API
    tipo_envio = 'bot_audio' if origen == 'bot' else 'asesor'
    enviar_mensaje(
        numero,
        caption,
        tipo=tipo_envio,
        tipo_respuesta='audio',
        opciones=path
    )
    if origen != 'bot':
        row = get_chat_state(numero)
        step = row[0] if row else ''
        update_chat_state(numero, step, 'asesor')

    return jsonify({'status':'sent_audio'}), 200

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
    path     = os.path.join(MEDIA_ROOT, unique)
    video.save(path)

    tipo_envio = 'bot_video' if origen == 'bot' else 'asesor'
    enviar_mensaje(
        numero,
        caption,
        tipo=tipo_envio,
        tipo_respuesta='video',
        opciones=path
    )
    if origen != 'bot':
        row = get_chat_state(numero)
        step = row[0] if row else ''
        update_chat_state(numero, step, 'asesor')

    return jsonify({'status':'sent_video'}), 200
