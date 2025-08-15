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


@tablero_bp.route('/datos_roles')
def datos_roles():
    """Devuelve la cantidad de mensajes de clientes agrupados por rol."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COALESCE(r.keyword, r.name) AS rol, COUNT(*) AS mensajes
          FROM mensajes m
          INNER JOIN chat_roles cr ON m.numero = cr.numero
          INNER JOIN roles r ON cr.role_id = r.id
         WHERE m.tipo LIKE 'cliente%'
         GROUP BY r.keyword, r.name
        """
    )
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

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT numero, COUNT(*) AS total
          FROM mensajes
         WHERE tipo LIKE 'cliente%'
         GROUP BY numero
         ORDER BY total DESC
         LIMIT ?
        """,
        (limite,),
    )
    rows = cur.fetchall()
    conn.close()

    data = [{"numero": numero, "mensajes": total} for numero, total in rows]
    return jsonify(data)


@tablero_bp.route('/datos_totales')
def datos_totales():
    """Devuelve el total de mensajes enviados y recibidos."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            SUM(CASE WHEN tipo LIKE 'bot%' OR tipo LIKE 'asesor%' THEN 1 ELSE 0 END) AS enviados,
            SUM(CASE WHEN tipo LIKE 'cliente%' OR tipo IN (
                'audio', 'video', 'cliente_image', 'cliente_audio', 'cliente_video', 'cliente_document', 'referral'
            ) THEN 1 ELSE 0 END) AS recibidos
        FROM mensajes
        """
    )
    row = cur.fetchone()
    conn.close()

    enviados, recibidos = row if row else (0, 0)
    return jsonify({"enviados": enviados, "recibidos": recibidos})
