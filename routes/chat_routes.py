import os
import uuid
from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
from werkzeug.utils import secure_filename
from config import Config
from services.whatsapp_api import enviar_mensaje
from services.db import get_connection, get_chat_state, update_chat_state

chat_bp = Blueprint('chat', __name__)

# Carpeta de subida debe coincidir con la de whatsapp_api
UPLOAD_FOLDER = Config.UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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
    c.execute("SELECT id, mensaje, tipo, media_url FROM botones ORDER BY id")
    botones = c.fetchall()

    conn.close()
    return render_template('index.html', chats=chats, botones=botones, rol=rol, role_id=role_id)

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
    c.execute("""
      SELECT m.mensaje, m.tipo, m.media_url, m.timestamp,
             m.link_url, m.link_title, m.link_body, m.link_thumb,
             m.wa_id, m.reply_to_wa_id,
             r.mensaje AS reply_text, r.tipo AS reply_tipo, r.media_url AS reply_media_url
      FROM mensajes m
      LEFT JOIN mensajes r ON r.wa_id = m.reply_to_wa_id
      WHERE m.numero = %s
      ORDER BY m.timestamp
    """, (numero,))
    mensajes = c.fetchall()
    conn.close()
    return jsonify({'mensajes': [list(m) for m in mensajes]})

@chat_bp.route('/send_message', methods=['POST'])
def send_message():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    data   = request.get_json()
    numero = data.get('numero')
    texto  = data.get('mensaje')
    tipo_respuesta = data.get('tipo_respuesta', 'texto')
    opciones = data.get('opciones')
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
    enviar_mensaje(
        numero,
        texto,
        tipo='asesor',
        tipo_respuesta=tipo_respuesta,
        opciones=opciones,
        reply_to_wa_id=reply_to_wa_id,
    )
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

        # Último mensaje para asesor
        c.execute(
            "SELECT mensaje FROM mensajes WHERE numero = %s "
            "ORDER BY timestamp DESC LIMIT 1",
            (numero,)
        )
        fila = c.fetchone()
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
        inicial_rol = None
        if nombres_roles:
            primer_nombre = nombres_roles.split(',')[0]
            if primer_nombre:
                inicial_rol = primer_nombre[0].upper()

        # Estado actual del chat
        c.execute("SELECT estado FROM chat_state WHERE numero = %s", (numero,))
        fila = c.fetchone()
        estado = fila[0] if fila else None

        chats.append({
            "numero": numero,
            "alias":  alias,
            "asesor": requiere_asesor,
            "roles": roles,
            "inicial_rol": inicial_rol,
            "estado": estado,
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
    path = os.path.join(UPLOAD_FOLDER, unique)
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
    path     = os.path.join(UPLOAD_FOLDER, unique)
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
    path = os.path.join(UPLOAD_FOLDER, unique)
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
    path     = os.path.join(UPLOAD_FOLDER, unique)
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
