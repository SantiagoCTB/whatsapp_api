from flask import Blueprint, render_template, session, redirect, url_for, jsonify, request
from collections import Counter
import re

from services.db import get_connection


tablero_bp = Blueprint('tablero', __name__)


def _apply_filters(cur, rol, numero):
    """Valida rol y número y genera joins/condiciones para las consultas."""
    joins = []
    conditions = []
    params = []

    if numero:
        cur.execute("SELECT 1 FROM mensajes WHERE numero = ? LIMIT 1", (numero,))
        if not cur.fetchone():
            raise ValueError("numero")
        conditions.append("m.numero = ?")
        params.append(numero)

    if rol:
        cur.execute("SELECT 1 FROM roles WHERE id = ?", (rol,))
        if not cur.fetchone():
            raise ValueError("rol")
        joins.append("JOIN chat_roles AS cr ON m.numero = cr.numero")
        conditions.append("cr.role_id = ?")
        params.append(rol)

    return " ".join(joins), conditions, params


@tablero_bp.route('/tablero')
def tablero():
    """Renderiza la página del tablero con gráficos de Chart.js."""
    if "user" not in session:
        return redirect(url_for('auth.login'))
    return render_template('tablero.html')


@tablero_bp.route('/lista_roles')
def lista_roles():
    """Devuelve la lista de roles disponibles."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM roles")
    rows = cur.fetchall()
    conn.close()

    roles = [{"id": rid, "name": name} for rid, name in rows]
    return jsonify(roles)


@tablero_bp.route('/lista_numeros')
def lista_numeros():
    """Devuelve la lista de números disponibles."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT numero FROM mensajes")
    rows = cur.fetchall()
    conn.close()

    numeros = [numero for (numero,) in rows]
    return jsonify(numeros)


@tablero_bp.route('/datos_tablero')
def datos_tablero():
    """Devuelve métricas del tablero en formato JSON."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()
    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = "SELECT m.numero, m.mensaje FROM mensajes m"
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    metrics = {}
    for numero, mensaje in rows:
        palabras = len((mensaje or "").split())
        metrics[numero] = metrics.get(numero, 0) + palabras

    data = [{"numero": num, "palabras": count} for num, count in metrics.items()]
    return jsonify(data)


@tablero_bp.route('/datos_tipos_diarios')
def datos_tipos_diarios():
    """Devuelve la cantidad de mensajes por tipo agrupados por fecha."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = (
        """
        SELECT DATE(m.timestamp) AS fecha, m.tipo, COUNT(*)
          FROM mensajes m
        """
    )
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    query += " WHERE " + " AND ".join(conditions) if conditions else ""
    query += " GROUP BY fecha, tipo ORDER BY fecha"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    aggregates = {}
    for fecha, tipo, total in rows:
        fecha_str = fecha.strftime("%Y-%m-%d")
        if fecha_str not in aggregates:
            aggregates[fecha_str] = {"cliente": 0, "bot": 0, "asesor": 0, "otros": 0}
        t = (tipo or "").lower()
        if t.startswith("cliente"):
            aggregates[fecha_str]["cliente"] += total
        elif t.startswith("bot"):
            aggregates[fecha_str]["bot"] += total
        elif t.startswith("asesor"):
            aggregates[fecha_str]["asesor"] += total
        else:
            aggregates[fecha_str]["otros"] += total

    data = [
        {"fecha": fecha, **vals}
        for fecha, vals in sorted(aggregates.items())
    ]
    return jsonify(data)


@tablero_bp.route('/datos_palabras')
def datos_palabras():
    """Devuelve las palabras más frecuentes en los mensajes."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    limite = request.args.get('limit', 10, type=int)

    start = request.args.get('start')
    end = request.args.get('end')
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = "SELECT m.mensaje FROM mensajes m"
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)

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
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    if numero:
        cur.execute("SELECT 1 FROM mensajes WHERE numero = ? LIMIT 1", (numero,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": "Número no encontrado"}), 400
    if rol:
        cur.execute("SELECT 1 FROM roles WHERE id = ?", (rol,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"error": "Rol no encontrado"}), 400

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
    if numero:
        query += " AND m.numero = ?"
        params.append(numero)
    if rol:
        query += " AND cr.role_id = ?"
        params.append(rol)
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

    limite = request.args.get('limit', 3, type=int)

    start = request.args.get('start')
    end = request.args.get('end')
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = (
        """
        SELECT m.numero, COUNT(*) AS total
          FROM mensajes m
        """
    )
    if joins:
        query += " " + joins

    conditions = ["m.tipo LIKE 'cliente%'"]
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    # Embed the limit directly to avoid placeholder compatibility issues
    query += f" GROUP BY m.numero ORDER BY total DESC LIMIT {limite}"
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
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = (
        """
        SELECT DATE(m.timestamp) AS fecha, COUNT(*) AS total
          FROM mensajes m
        """
    )
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY DATE(m.timestamp) ORDER BY fecha"
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
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = (
        """
        SELECT HOUR(m.timestamp) AS hora, COUNT(*) AS total
          FROM mensajes m
        """
    )
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY HOUR(m.timestamp) ORDER BY hora"
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
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = "SELECT m.tipo, COUNT(*) FROM mensajes m"
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY m.tipo"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    aggregates = {"cliente": 0, "bot": 0, "asesor": 0, "otros": 0}
    for tipo, count in rows:
        t = (tipo or "").lower()
        if t.startswith("cliente"):
            aggregates["cliente"] += count
        elif t.startswith("bot"):
            aggregates["bot"] += count
        elif t.startswith("asesor"):
            aggregates["asesor"] += count
        else:
            aggregates["otros"] += count

    data = [
        {"tipo": tipo, "total": total}
        for tipo, total in aggregates.items()
        if total > 0
    ]
    return jsonify(data)


@tablero_bp.route('/datos_totales')
def datos_totales():
    """Devuelve el total de mensajes enviados y recibidos."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    start = request.args.get('start')
    end = request.args.get('end')
    rol = request.args.get('rol', type=int)
    numero = request.args.get('numero')

    conn = get_connection()
    cur = conn.cursor()

    try:
        joins, filter_conditions, filter_params = _apply_filters(cur, rol, numero)
    except ValueError as e:
        conn.close()
        msg = 'Rol' if str(e) == 'rol' else 'Número'
        return jsonify({"error": f"{msg} no encontrado"}), 400

    query = "SELECT m.tipo, COUNT(*) FROM mensajes m"
    if joins:
        query += " " + joins

    conditions = []
    params = []
    if start and end:
        conditions.append("m.timestamp BETWEEN ? AND ?")
        params.extend([start, end])
    conditions.extend(filter_conditions)
    params.extend(filter_params)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY m.tipo"
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


@tablero_bp.route('/datos_roles_total')
def datos_roles_total():
    """Devuelve la cantidad total de roles."""
    if "user" not in session:
        return redirect(url_for('auth.login'))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM roles")
    total = cur.fetchone()[0]
    conn.close()

    return jsonify({"total_roles": total})
