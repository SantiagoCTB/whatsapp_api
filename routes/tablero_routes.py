from flask import Blueprint, render_template, session, redirect, url_for, jsonify

from services.db import get_connection


tablero_bp = Blueprint('tablero', __name__)


@tablero_bp.route('/tablero')
def tablero():
    """Renderiza la página del tablero con gráficos de Chart.js."""
    if "user" not in session:
        return redirect(url_for('auth.login'))
    return render_template('tablero.html')


@tablero_bp.route('/datos_tablero')
def datos_tablero():
    """Devuelve métricas del tablero en formato JSON."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT numero, mensaje FROM mensajes")
    rows = cur.fetchall()
    conn.close()

    metrics = {}
    for numero, mensaje in rows:
        palabras = len((mensaje or "").split())
        metrics[numero] = metrics.get(numero, 0) + palabras

    data = [{"numero": num, "palabras": count} for num, count in metrics.items()]
    return jsonify(data)
