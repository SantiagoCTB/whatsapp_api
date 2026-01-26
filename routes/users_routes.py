import importlib.util

from flask import Blueprint, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

if importlib.util.find_spec("mysql.connector"):
    from mysql.connector.errors import IntegrityError
else:  # pragma: no cover - fallback cuando falta el conector
    class IntegrityError(Exception):
        pass

from routes.auth_routes import _password_strength_error
from services.db import get_connection


users_bp = Blueprint('usuarios', __name__)


def _is_admin() -> bool:
    return 'admin' in (session.get('roles') or [])


@users_bp.route('/usuarios', methods=['GET', 'POST'])
def manage_users():
    if not _is_admin():
        return redirect(url_for('auth.login'))

    success_message = session.pop('user_success', None)
    delete_error = session.pop('user_error', None)
    error = None
    form_data = {}

    conn = get_connection()
    try:
        cursor = conn.cursor()

        if request.method == 'POST':
            username = (request.form.get('username') or '').strip()
            password = request.form.get('password') or ''
            confirm_password = request.form.get('confirm_password') or ''
            role_id = request.form.get('role_id') or ''

            form_data = {
                'username': username,
                'role_id': role_id,
            }

            if not username or not password or not confirm_password or not role_id:
                error = 'Todos los campos son obligatorios.'
            elif password != confirm_password:
                error = 'Las contraseñas no coinciden.'
            else:
                strength_error = _password_strength_error(password)
                if strength_error:
                    error = strength_error

            if not error:
                hashed_password = generate_password_hash(password)
                try:
                    cursor.execute(
                        'INSERT INTO usuarios (username, password) VALUES (%s, %s)',
                        (username, hashed_password),
                    )
                    user_id = cursor.lastrowid
                    cursor.execute(
                        'INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)',
                        (user_id, role_id),
                    )
                    conn.commit()
                    session['user_success'] = f"Usuario '{username}' creado correctamente."
                    return redirect(url_for('usuarios.manage_users'))
                except IntegrityError:
                    conn.rollback()
                    error = 'El nombre de usuario ya existe.'

        cursor.execute('SELECT id, name FROM roles ORDER BY name')
        roles = cursor.fetchall()

        cursor.execute(
            '''
            SELECT u.id, u.username,
                   COALESCE(GROUP_CONCAT(r.name ORDER BY r.name SEPARATOR ', '), '')
              FROM usuarios u
         LEFT JOIN user_roles ur ON u.id = ur.user_id
         LEFT JOIN roles r ON ur.role_id = r.id
          GROUP BY u.id, u.username
          ORDER BY u.username
            '''
        )
        usuarios = cursor.fetchall()
    finally:
        conn.close()

    if not error and delete_error:
        error = delete_error

    return render_template(
        'usuarios.html',
        roles=roles,
        usuarios=usuarios,
        error=error,
        success=success_message,
        form_data=form_data,
    )


@users_bp.route('/usuarios/<int:user_id>/delete', methods=['POST'])
def delete_user(user_id: int):
    if not _is_admin():
        return redirect(url_for('auth.login'))

    current_username = session.get('user')
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT id, username FROM usuarios WHERE id = %s', (user_id,))
        row = cursor.fetchone()
        if not row:
            session['user_error'] = 'El usuario seleccionado no existe.'
        elif row[1] == current_username:
            session['user_error'] = 'No puedes eliminar tu propio usuario.'
        else:
            cursor.execute('DELETE FROM usuarios WHERE id = %s', (user_id,))
            conn.commit()
            session['user_success'] = f"Usuario '{row[1]}' eliminado correctamente."
    finally:
        conn.close()

    return redirect(url_for('usuarios.manage_users'))
