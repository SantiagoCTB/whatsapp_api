from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
from services.db import get_connection
from openpyxl import load_workbook

config_bp = Blueprint('configuracion', __name__)

@config_bp.route('/configuracion', methods=['GET', 'POST'])
def configuracion():
    # Solo admin
    if "user" not in session or 'admin' not in session.get('roles', []):
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()

    if request.method == 'POST':
        # Importar desde Excel
        if 'archivo' in request.files:
            archivo = request.files['archivo']
            wb = load_workbook(archivo)
            hoja = wb.active
            for fila in hoja.iter_rows(min_row=2, values_only=True):
                step, input_text, respuesta, siguiente_step, tipo, opciones = fila
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
                               opciones = %s
                         WHERE id = %s
                        """,
                        (respuesta, siguiente_step, tipo, opciones, regla_id)
                    )
                else:
                    c.execute(
                        "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, opciones)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (step, input_text, respuesta, siguiente_step, tipo, opciones)
                    )
            conn.commit()
        else:
            # Entrada manual desde formulario
            step = request.form['step']
            input_text = request.form['input_text']
            respuesta = request.form['respuesta']
            siguiente_step = request.form.get('siguiente_step', None)
            tipo = request.form.get('tipo', 'texto')
            opciones = request.form.get('opciones', '')

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
                           opciones = %s
                     WHERE id = %s
                    """,
                    (respuesta, siguiente_step, tipo, opciones, regla_id)
                )
            else:
                c.execute(
                    "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, opciones)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (step, input_text, respuesta, siguiente_step, tipo, opciones)
                )
            conn.commit()

    # Listar todas las reglas
    c.execute(
        "SELECT id, step, input_text, respuesta, siguiente_step, tipo, opciones"
        " FROM reglas"
        " ORDER BY step, id"
    )
    reglas = c.fetchall()
    conn.close()
    return render_template('configuracion.html', reglas=reglas)


@config_bp.route('/eliminar_regla/<int:regla_id>', methods=['POST'])
def eliminar_regla(regla_id):
    if "user" not in session or 'admin' not in session.get('roles', []):
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "DELETE FROM reglas WHERE id = %s",
        (regla_id,)
    )
    conn.commit()
    conn.close()
    return redirect(url_for('configuracion.configuracion'))


@config_bp.route('/botones', methods=['GET', 'POST'])
def botones():
    if "user" not in session or 'admin' not in session.get('roles', []):
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()

    if request.method == 'POST':
        # Importar botones desde Excel
        if 'archivo' in request.files:
            archivo = request.files['archivo']
            wb = load_workbook(archivo)
            hoja = wb.active
            for fila in hoja.iter_rows(min_row=2, values_only=True):
                mensaje = fila[0]
                if mensaje:
                    c.execute(
                        "INSERT INTO botones (mensaje) VALUES (%s)",
                        (mensaje,)
                    )
            conn.commit()
        # Agregar botón manual
        elif 'mensaje' in request.form:
            nuevo_mensaje = request.form['mensaje']
            c.execute(
                "INSERT INTO botones (mensaje) VALUES (%s)",
                (nuevo_mensaje,)
            )
            conn.commit()

    c.execute("SELECT id, mensaje FROM botones ORDER BY id")
    botones = c.fetchall()
    conn.close()
    return render_template('botones.html', botones=botones)


@config_bp.route('/eliminar_boton/<int:boton_id>', methods=['POST'])
def eliminar_boton(boton_id):
    if "user" not in session or 'admin' not in session.get('roles', []):
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "DELETE FROM botones WHERE id = %s",
        (boton_id,)
    )
    conn.commit()
    conn.close()
    return redirect(url_for('configuracion.botones'))


@config_bp.route('/get_botones')
def get_botones():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, mensaje FROM botones ORDER BY id")
    rows = c.fetchall()
    conn.close()
    # Retorna lista de dicts
    return jsonify([{'id': r[0], 'mensaje': r[1]} for r in rows])
