from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
from services.db import get_connection
from openpyxl import load_workbook
from werkzeug.utils import secure_filename
from config import Config
import os
import uuid

config_bp = Blueprint('configuracion', __name__)
UPLOAD_FOLDER = Config.UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _require_admin():
    # Debe haber usuario logueado y el rol 'admin' en la lista de roles
    return "user" in session and 'admin' in (session.get('roles') or [])


def _normalize_input(text):
    """Normaliza una lista separada por comas."""
    return ','.join(t.strip().lower() for t in (text or '').split(',') if t.strip())

def _reglas_view(template_name):
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        # --- Migraciones defensivas de nuevas columnas ---
        c.execute("SHOW COLUMNS FROM reglas LIKE 'rol_keyword';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN rol_keyword VARCHAR(20) NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'calculo';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN calculo TEXT NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'handler';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN handler VARCHAR(50) NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'media_url';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN media_url TEXT NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'media_tipo';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN media_tipo VARCHAR(20) NULL;")
            conn.commit()

        if request.method == 'POST':
            # Importar desde Excel
            if 'archivo' in request.files and request.files['archivo']:
                archivo = request.files['archivo']
                wb = load_workbook(archivo)
                hoja = wb.active
                for fila in hoja.iter_rows(min_row=2, values_only=True):
                    if not fila:
                        continue
                    # Permitir archivos con columnas opcionales
                    datos = list(fila) + [None] * 11
                    step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler = datos[:11]
                    # Normalizar campos clave
                    step = (step or '').strip().lower()
                    input_text = _normalize_input(input_text)
                    siguiente_step = (siguiente_step or '').strip().lower() or None

                    c.execute(
                        "SELECT id FROM reglas WHERE step = %s AND input_text = %s",
                        (step, input_text)
                    )
                    existente = c.fetchone()
                    if existente:
                        regla_id = existente[0]
                        c.execute(
                            """
                            UPDATE reglas
                               SET respuesta = %s,
                                   siguiente_step = %s,
                                   tipo = %s,
                                   media_url = %s,
                                   media_tipo = %s,
                                   opciones = %s,
                                   rol_keyword = %s,
                                   calculo = %s,
                                   handler = %s
                             WHERE id = %s
                            """,
                            (respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler, regla_id)
                        )
                        c.execute("DELETE FROM regla_medias WHERE regla_id=%s", (regla_id,))
                        if media_url:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, media_url, media_tipo),
                            )
                    else:
                        c.execute(
                            "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler)
                        )
                        regla_id = c.lastrowid
                        if media_url:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, media_url, media_tipo),
                            )
                conn.commit()
            else:
                # Entrada manual desde formulario
                step = (request.form['step'] or '').strip().lower() or None
                input_text = _normalize_input(request.form['input_text']) or None
                respuesta = request.form['respuesta']
                siguiente_step = (request.form.get('siguiente_step') or '').strip().lower() or None
                tipo = request.form.get('tipo', 'texto')
                media_files = request.files.getlist('media')
                media_url_field = request.form.get('media_url')
                medias = []
                for media_file in media_files:
                    if media_file and media_file.filename:
                        filename = secure_filename(media_file.filename)
                        unique = f"{uuid.uuid4().hex}_{filename}"
                        path = os.path.join(UPLOAD_FOLDER, unique)
                        media_file.save(path)
                        url = url_for('static', filename=f'uploads/{unique}', _external=True)
                        medias.append((url, media_file.mimetype))
                if media_url_field:
                    for url in [u.strip() for u in media_url_field.split(',') if u.strip()]:
                        medias.append((url, None))
                media_url = medias[0][0] if medias else None
                media_tipo = medias[0][1] if medias else None
                opciones = request.form.get('opciones', '')
                rol_keyword = request.form.get('rol_keyword')
                calculo = request.form.get('calculo')
                handler = request.form.get('handler')

                c.execute(
                    "SELECT id FROM reglas WHERE step = %s AND input_text = %s",
                    (step, input_text)
                )
                existente = c.fetchone()
                if existente:
                    regla_id = existente[0]
                    c.execute(
                        """
                        UPDATE reglas
                           SET respuesta = %s,
                               siguiente_step = %s,
                               tipo = %s,
                               media_url = %s,
                               media_tipo = %s,
                               opciones = %s,
                               rol_keyword = %s,
                               calculo = %s,
                               handler = %s
                         WHERE id = %s
                        """,
                        (respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler, regla_id)
                    )
                    c.execute("DELETE FROM regla_medias WHERE regla_id=%s", (regla_id,))
                    for url, tipo_media in medias:
                        c.execute(
                            "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                            (regla_id, url, tipo_media),
                        )
                else:
                    c.execute(
                        "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler),
                    )
                    regla_id = c.lastrowid
                    for url, tipo_media in medias:
                        c.execute(
                            "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                            (regla_id, url, tipo_media),
                        )
                conn.commit()

        # Listar todas las reglas
        c.execute(
            """
            SELECT r.id, r.step, r.input_text, r.respuesta, r.siguiente_step, r.tipo,
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos,
                   r.opciones, r.rol_keyword, r.calculo, r.handler
              FROM reglas r
              LEFT JOIN regla_medias m ON r.id = m.regla_id
             GROUP BY r.id
             ORDER BY r.step, r.id
            """
        )
        reglas = c.fetchall()
        return render_template(template_name, reglas=reglas)
    finally:
        conn.close()

@config_bp.route('/configuracion', methods=['GET', 'POST'])
def configuracion():
    return _reglas_view('configuracion.html')

@config_bp.route('/reglas', methods=['GET', 'POST'])
def reglas():
    return _reglas_view('reglas.html')

@config_bp.route('/eliminar_regla/<int:regla_id>', methods=['POST'])
def eliminar_regla(regla_id):
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM reglas WHERE id = %s", (regla_id,))
        conn.commit()
        return redirect(url_for('configuracion.reglas'))
    finally:
        conn.close()

@config_bp.route('/botones', methods=['GET', 'POST'])
def botones():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        if request.method == 'POST':
            # Importar botones desde Excel
            if 'archivo' in request.files and request.files['archivo']:
                archivo = request.files['archivo']
                wb = load_workbook(archivo)
                hoja = wb.active
                for fila in hoja.iter_rows(min_row=2, values_only=True):
                    if not fila:
                        continue
                    mensaje = fila[0]
                    tipo = fila[1] if len(fila) > 1 else None
                    media_url = fila[2] if len(fila) > 2 else None
                    if mensaje:
                        c.execute(
                            "INSERT INTO botones (mensaje, tipo, media_url) VALUES (%s, %s, %s)",
                            (mensaje, tipo, media_url)
                        )
                conn.commit()
            # Agregar botón manual
            elif 'mensaje' in request.form:
                nuevo_mensaje = request.form['mensaje']
                tipo = request.form.get('tipo')
                media_file = request.files.get('media')
                media_url = None
                if media_file and media_file.filename:
                    filename = secure_filename(media_file.filename)
                    unique = f"{uuid.uuid4().hex}_{filename}"
                    path = os.path.join(UPLOAD_FOLDER, unique)
                    media_file.save(path)
                    media_url = url_for('static', filename=f'uploads/{unique}', _external=True)
                if nuevo_mensaje:
                    c.execute(
                        "INSERT INTO botones (mensaje, tipo, media_url) VALUES (%s, %s, %s)",
                        (nuevo_mensaje, tipo, media_url)
                    )
                    conn.commit()

        c.execute("SELECT id, mensaje, tipo, media_url FROM botones ORDER BY id")
        botones = c.fetchall()
        return render_template('botones.html', botones=botones)
    finally:
        conn.close()

@config_bp.route('/eliminar_boton/<int:boton_id>', methods=['POST'])
def eliminar_boton(boton_id):
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM botones WHERE id = %s", (boton_id,))
        conn.commit()
        return redirect(url_for('configuracion.botones'))
    finally:
        conn.close()

@config_bp.route('/get_botones')
def get_botones():
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT id, mensaje, tipo, media_url FROM botones ORDER BY id")
        rows = c.fetchall()
        return jsonify([
            {'id': r[0], 'mensaje': r[1], 'tipo': r[2], 'media_url': r[3]}
            for r in rows
        ])
    finally:
        conn.close()
