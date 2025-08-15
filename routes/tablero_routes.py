from flask import Blueprint, render_template, session, redirect, url_for, jsonify, request
from collections import Counter

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


@tablero_bp.route('/datos_palabras')
def datos_palabras():
    """Devuelve las palabras más frecuentes en los mensajes."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    limite = request.args.get('limit', 10, type=int)

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT mensaje FROM mensajes")
    rows = cur.fetchall()
    conn.close()

    contador = Counter()
    for (mensaje,) in rows:
        if mensaje:
            contador.update(mensaje.split())

    palabras_comunes = contador.most_common(limite)
    data = [{"palabra": palabra, "frecuencia": frecuencia} for palabra, frecuencia in palabras_comunes]
    return jsonify(data)
