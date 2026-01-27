import re

from flask import Blueprint, render_template, request, redirect, url_for, session
from services.db import get_connection

roles_bp = Blueprint('roles', __name__)


def _is_admin():
    return 'admin' in session.get('roles', [])


@roles_bp.route('/roles')
def roles():
    if not _is_admin():
        return redirect(url_for('auth.login'))

    conn = get_connection()
    c = conn.cursor()

    c.execute('SELECT id, name, keyword FROM roles ORDER BY name')
    roles = c.fetchall()

    c.execute('''
        SELECT ur.role_id, u.username
        FROM user_roles ur
        JOIN usuarios u ON ur.user_id = u.id
    ''')
    asignaciones = {}
    for role_id, username in c.fetchall():
        asignaciones.setdefault(role_id, []).append(username)

    c.execute('SELECT id, username FROM usuarios ORDER BY username')
    usuarios = c.fetchall()
    conn.close()

    bulk_summary = None
    assigned = request.args.get('bulk_assigned')
    if assigned is not None:
        bulk_summary = {
            "assigned": assigned,
            "role": request.args.get('bulk_role'),
            "keywords": request.args.get('bulk_keywords'),
            "error": request.args.get('bulk_error'),
            "user": request.args.get('bulk_user'),
            "user_assigned": request.args.get('bulk_user_assigned'),
        }

    return render_template(
        'roles.html',
        roles=roles,
        asignaciones=asignaciones,
        usuarios=usuarios,
        bulk_summary=bulk_summary,
    )


@roles_bp.route('/roles/create', methods=['POST'])
def crear_rol():
    if not _is_admin():
        return redirect(url_for('auth.login'))
    name = request.form['name']
    keyword = request.form.get('keyword', '')
    conn = get_connection()
    c = conn.cursor()
    c.execute('INSERT INTO roles (name, keyword) VALUES (%s, %s)', (name, keyword))
    conn.commit()
    conn.close()
    return redirect(url_for('roles.roles'))


@roles_bp.route('/roles/<int:rol_id>/edit', methods=['POST'])
def editar_rol(rol_id):
    if not _is_admin():
        return redirect(url_for('auth.login'))
    name = request.form['name']
    keyword = request.form.get('keyword', '')
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE roles SET name=%s, keyword=%s WHERE id=%s', (name, keyword, rol_id))
    conn.commit()
    conn.close()
    return redirect(url_for('roles.roles'))


@roles_bp.route('/roles/<int:rol_id>/delete', methods=['POST'])
def eliminar_rol(rol_id):
    if not _is_admin():
        return redirect(url_for('auth.login'))
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM user_roles WHERE role_id=%s', (rol_id,))
    c.execute('DELETE FROM roles WHERE id=%s', (rol_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('roles.roles'))


@roles_bp.route('/roles/assign', methods=['POST'])
def asignar_rol():
    if not _is_admin():
        return redirect(url_for('auth.login'))
    user_id = request.form['user_id']
    role_id = request.form['role_id']
    conn = get_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO user_roles (user_id, role_id)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE user_id = user_id
    ''', (user_id, role_id))
    conn.commit()
    conn.close()
    return redirect(url_for('roles.roles'))


@roles_bp.route('/roles/unassign', methods=['POST'])
def quitar_rol():
    if not _is_admin():
        return redirect(url_for('auth.login'))
    user_id = request.form['user_id']
    role_id = request.form['role_id']
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM user_roles WHERE user_id=%s AND role_id=%s', (user_id, role_id))
    conn.commit()
    conn.close()
    return redirect(url_for('roles.roles'))


@roles_bp.route('/roles/bulk_assign', methods=['POST'])
def asignar_roles_masivo():
    if not _is_admin():
        return redirect(url_for('auth.login'))

    role_id = request.form.get('role_id')
    user_id = request.form.get('user_id')
    raw_keywords = request.form.get('keywords', '')
    keywords = [kw.strip().lower() for kw in re.split(r'[,\\n]+', raw_keywords) if kw.strip()]

    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name, keyword FROM roles WHERE id=%s", (role_id,))
    role_row = c.fetchone()
    if not role_row:
        conn.close()
        return redirect(url_for('roles.roles', bulk_assigned=0, bulk_error='rol_no_encontrado'))

    role_name, role_keyword = role_row
    if not keywords and role_keyword:
        keywords = [role_keyword.strip().lower()]

    if not keywords:
        conn.close()
        return redirect(url_for('roles.roles', bulk_assigned=0, bulk_error='sin_keywords'))

    user_name = None
    if user_id:
        c.execute("SELECT username FROM usuarios WHERE id = %s", (user_id,))
        user_row = c.fetchone()
        if not user_row:
            conn.close()
            return redirect(url_for('roles.roles', bulk_assigned=0, bulk_error='usuario_no_encontrado'))
        user_name = user_row[0]

    conditions = " OR ".join(["LOWER(m.mensaje) LIKE %s"] * len(keywords))
    like_params = [f"%{kw}%" for kw in keywords]
    c.execute(
        f"""
        INSERT IGNORE INTO chat_roles (numero, role_id)
        SELECT DISTINCT m.numero, %s
          FROM mensajes m
         WHERE {conditions}
        """,
        [role_id, *like_params],
    )
    assigned = c.rowcount or 0
    user_assigned = 0
    if user_id:
        c.execute(
            f"""
            INSERT INTO chat_assignments (numero, user_id, role_id, assigned_at)
            SELECT DISTINCT m.numero, %s, %s, NOW()
              FROM mensajes m
             WHERE {conditions}
            ON DUPLICATE KEY UPDATE
              user_id = VALUES(user_id),
              role_id = VALUES(role_id),
              assigned_at = VALUES(assigned_at)
            """,
            [user_id, role_id, *like_params],
        )
        user_assigned = c.rowcount or 0
    conn.commit()
    conn.close()

    return redirect(
        url_for(
            'roles.roles',
            bulk_assigned=assigned,
            bulk_role=role_name,
            bulk_keywords=", ".join(keywords),
            bulk_user=user_name,
            bulk_user_assigned=user_assigned,
        )
    )
