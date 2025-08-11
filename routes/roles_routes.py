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

    c.execute('SELECT id, nombre, keywords FROM roles ORDER BY nombre')
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

    return render_template('roles.html', roles=roles, asignaciones=asignaciones, usuarios=usuarios)


@roles_bp.route('/roles/create', methods=['POST'])
def crear_rol():
    if not _is_admin():
        return redirect(url_for('auth.login'))
    nombre = request.form['nombre']
    keywords = request.form.get('keywords', '')
    conn = get_connection()
    c = conn.cursor()
    c.execute('INSERT INTO roles (nombre, keywords) VALUES (%s, %s)', (nombre, keywords))
    conn.commit()
    conn.close()
    return redirect(url_for('roles.roles'))


@roles_bp.route('/roles/<int:rol_id>/edit', methods=['POST'])
def editar_rol(rol_id):
    if not _is_admin():
        return redirect(url_for('auth.login'))
    nombre = request.form['nombre']
    keywords = request.form.get('keywords', '')
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE roles SET nombre=%s, keywords=%s WHERE id=%s', (nombre, keywords, rol_id))
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
