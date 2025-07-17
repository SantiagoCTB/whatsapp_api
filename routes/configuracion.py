from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
import sqlite3
from config import Config

config_bp = Blueprint('configuracion', __name__)

@config_bp.route('/configuracion', methods=['GET', 'POST'])
def configuracion():
    if "user" not in session or session["rol"] != "admin":
        return redirect(url_for("auth.login"))

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()

    # Cargar archivo Excel si se sube
    if request.method == 'POST':
        if 'archivo' in request.files:
            from openpyxl import load_workbook
            archivo = request.files['archivo']
            wb = load_workbook(archivo)
            hoja = wb.active

            for fila in hoja.iter_rows(min_row=2, values_only=True):
                step, input_text, respuesta, siguiente_step, tipo, opciones = fila  # añade opciones

                c.execute("SELECT id FROM reglas WHERE step = ? AND input_text = ?", (step, input_text))
                existente = c.fetchone()
                if existente:
                    c.execute('''
                        UPDATE reglas
                        SET respuesta = ?, siguiente_step = ?, tipo = ?, opciones = ?
                        WHERE id = ?
                    ''', (respuesta, siguiente_step, tipo, opciones, existente[0]))
                else:
                    c.execute('''
                        INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, opciones)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (step, input_text, respuesta, siguiente_step, tipo, opciones))
            conn.commit()

        else:
            # Carga desde formulario manual
            step = request.form['step']
            input_text = request.form['input_text']
            respuesta = request.form['respuesta']
            siguiente_step = request.form['siguiente_step']
            tipo = request.form['tipo']
            opciones = request.form.get('opciones', '')

            c.execute("SELECT id FROM reglas WHERE step = ? AND input_text = ?", (step, input_text))
            existente = c.fetchone()
            if existente:
                c.execute('''
                    UPDATE reglas
                    SET respuesta = ?, siguiente_step = ?, tipo = ?, opciones = ?
                    WHERE id = ?
                ''', (respuesta, siguiente_step, tipo, opciones, existente[0]))
            else:
                c.execute('''
                    INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, opciones)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (step, input_text, respuesta, siguiente_step, tipo, opciones))
            conn.commit()

    c.execute("SELECT * FROM reglas ORDER BY step, id")
    reglas = c.fetchall()
    conn.close()

    return render_template('configuracion.html', reglas=reglas)


@config_bp.route('/eliminar_regla/<int:regla_id>', methods=['POST'])
def eliminar_regla(regla_id):
    if "user" not in session or session["rol"] != "admin":
        return redirect(url_for("auth.login"))

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM reglas WHERE id = ?", (regla_id,))
    conn.commit()
    conn.close()

    return redirect(url_for("configuracion.configuracion"))

@config_bp.route('/botones', methods=['GET', 'POST'])
def botones():
    if "user" not in session or session["rol"] != "admin":
        return redirect(url_for("auth.login"))

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()

    if request.method == 'POST':
        if 'archivo' in request.files:
            from openpyxl import load_workbook
            archivo = request.files['archivo']
            wb = load_workbook(archivo)
            hoja = wb.active

            for fila in hoja.iter_rows(min_row=2, values_only=True):
                mensaje = fila[0]
                if mensaje:
                    c.execute("INSERT INTO botones (mensaje) VALUES (?)", (mensaje,))
            conn.commit()
        elif 'mensaje' in request.form:
            nuevo_mensaje = request.form['mensaje']
            c.execute("INSERT INTO botones (mensaje) VALUES (?)", (nuevo_mensaje,))
            conn.commit()

    c.execute("SELECT id, mensaje FROM botones ORDER BY id")
    botones = c.fetchall()
    conn.close()
    return render_template('botones.html', botones=botones)

@config_bp.route('/eliminar_boton/<int:boton_id>', methods=['POST'])
def eliminar_boton(boton_id):
    if "user" not in session or session["rol"] != "admin":
        return redirect(url_for("auth.login"))

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM botones WHERE id = ?", (boton_id,))
    conn.commit()
    conn.close()

    return redirect(url_for('botones'))

@config_bp.route("/get_botones")
def get_botones():
    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, mensaje FROM botones ORDER BY id")
    botones = [{"id": row[0], "mensaje": row[1]} for row in c.fetchall()]
    conn.close()
    return jsonify(botones)
