from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
from services.db import get_connection
from openpyxl import load_workbook

config_bp = Blueprint('configuracion', __name__)

def _require_admin():
    # Debe haber usuario logueado y el rol 'admin' en la lista de roles
    return "user" in session and 'admin' in (session.get('roles') or [])

@config_bp.route('/configuracion', methods=['GET', 'POST'])
def configuracion():
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
                    datos = list(fila) + [None] * 9
                    step, input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler = datos[:9]
                    # Normalizar campos clave
                    step = (step or '').strip().lower()
                    input_text = (input_text or '').strip().lower()
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
                                   opciones = %s,
                                   rol_keyword = %s,
                                   calculo = %s,
                                   handler = %s
                             WHERE id = %s
                            """,
                            (respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler, regla_id)
                        )
                    else:
                        c.execute(
                            "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (step, input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler)
                        )
                conn.commit()
            else:
                # Entrada manual desde formulario
                step = (request.form['step'] or '').strip().lower() or None
                input_text = (request.form['input_text'] or '').strip().lower() or None
                respuesta = request.form['respuesta']
                siguiente_step = (request.form.get('siguiente_step') or '').strip().lower() or None
                tipo = request.form.get('tipo', 'texto')
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
                               opciones = %s,
                               rol_keyword = %s,
                               calculo = %s,
                               handler = %s
                         WHERE id = %s
                        """,
                        (respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler, regla_id)
                    )
                else:
                    c.execute(
                        "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (step, input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler),
                    )
                conn.commit()

        # Listar todas las reglas
        c.execute(
            "SELECT id, step, input_text, respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler "
            "FROM reglas "
            "ORDER BY step, id"
        )
        reglas = c.fetchall()
        return render_template('configuracion.html', reglas=reglas)
    finally:
        conn.close()

@config_bp.route('/eliminar_regla/<int:regla_id>', methods=['POST'])
def eliminar_regla(regla_id):
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM reglas WHERE id = %s", (regla_id,))
        conn.commit()
        return redirect(url_for('configuracion.configuracion'))
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
                    if mensaje:
                        c.execute("INSERT INTO botones (mensaje) VALUES (%s)", (mensaje,))
                conn.commit()
            # Agregar botón manual
            elif 'mensaje' in request.form:
                nuevo_mensaje = request.form['mensaje']
                if nuevo_mensaje:
                    c.execute("INSERT INTO botones (mensaje) VALUES (%s)", (nuevo_mensaje,))
                    conn.commit()

        c.execute("SELECT id, mensaje FROM botones ORDER BY id")
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
        c.execute("SELECT id, mensaje FROM botones ORDER BY id")
        rows = c.fetchall()
        return jsonify([{'id': r[0], 'mensaje': r[1]} for r in rows])
    finally:
        conn.close()
