from flask import Blueprint, render_template, session, redirect, url_for, jsonify, request
from collections import Counter
import re

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

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT numero, mensaje FROM mensajes"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    cur.execute(query, params)
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

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT mensaje FROM mensajes"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    contador = Counter()
    for (mensaje,) in rows:
        if mensaje:
            palabras = re.findall(r"\w+", mensaje.lower())
            contador.update(palabras)

    palabras_comunes = contador.most_common(limite)
    data = [{"palabra": palabra, "frecuencia": frecuencia} for palabra, frecuencia in palabras_comunes]
    return jsonify(data)


@tablero_bp.route('/datos_roles')
def datos_roles():
    """Devuelve la cantidad de mensajes de clientes agrupados por rol."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = (
        """
        SELECT COALESCE(r.keyword, r.name) AS rol, COUNT(*) AS mensajes
          FROM mensajes AS m
          JOIN chat_roles AS cr ON m.numero = cr.numero
          JOIN roles AS r ON cr.role_id = r.id
         WHERE m.tipo LIKE 'cliente%'
        """
    )
    params = []
    if start and end:
        query += " AND m.timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    query += " GROUP BY rol"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    data = [{"rol": rol, "mensajes": count} for rol, count in rows]
    return jsonify(data)


@tablero_bp.route('/datos_top_numeros')
def datos_top_numeros():
    """Devuelve los números con más mensajes de clientes."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    limite = request.args.get('limit', 5, type=int)

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = (
        """
        SELECT numero, COUNT(*) AS total
          FROM mensajes
         WHERE tipo LIKE 'cliente%'
        """
    )
    params = []
    if start and end:
        query += " AND timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    query += " GROUP BY numero ORDER BY total DESC LIMIT ?"
    params.append(limite)
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    data = [{"numero": numero, "mensajes": total} for numero, total in rows]
    return jsonify(data)


@tablero_bp.route('/datos_mensajes_diarios')
def datos_mensajes_diarios():
    """Devuelve el total de mensajes agrupados por fecha."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = (
        """
        SELECT DATE(timestamp) AS fecha, COUNT(*) AS total
          FROM mensajes
        """
    )
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    query += " GROUP BY DATE(timestamp) ORDER BY fecha"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    data = [{"fecha": fecha.strftime("%Y-%m-%d"), "total": total} for fecha, total in rows]
    return jsonify(data)


@tablero_bp.route('/datos_mensajes_hora')
def datos_mensajes_hora():
    """Devuelve el total de mensajes agrupados por hora."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = (
        """
        SELECT HOUR(timestamp) AS hora, COUNT(*) AS total
          FROM mensajes
        """
    )
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    query += " GROUP BY HOUR(timestamp) ORDER BY hora"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    data = [{"hora": int(hora), "total": total} for hora, total in rows]
    return jsonify(data)


@tablero_bp.route('/datos_tipos')
def datos_tipos():
    """Devuelve la cantidad de mensajes agrupados por tipo."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT tipo, COUNT(*) FROM mensajes"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    query += " GROUP BY tipo"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    data = [{"tipo": tipo or "desconocido", "total": count} for tipo, count in rows]
    return jsonify(data)


@tablero_bp.route('/datos_totales')
def datos_totales():
    """Devuelve el total de mensajes enviados y recibidos."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')

    conn = get_connection()
    cur = conn.cursor()
    query = "SELECT tipo, COUNT(*) FROM mensajes"
    params = []
    if start and end:
        query += " WHERE timestamp BETWEEN ? AND ?"
        params.extend([start, end])
    query += " GROUP BY tipo"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    enviados = sum(
        count
        for tipo, count in rows
        if tipo and (tipo.startswith('bot') or tipo.startswith('asesor'))
    )
    recibidos = sum(
        count
        for tipo, count in rows
        if not (tipo and (tipo.startswith('bot') or tipo.startswith('asesor')))
    )

    return jsonify({"enviados": enviados, "recibidos": recibidos})
