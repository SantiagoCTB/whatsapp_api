from flask import Blueprint, render_template, request, redirect, session, url_for
import hashlib
import re
from typing import Optional
from werkzeug.security import check_password_hash, generate_password_hash
from services.db import get_connection, get_roles_by_user

auth_bp = Blueprint('auth', __name__)

def _verify_password(stored_hash: str, plain: str) -> bool:
    """
    Soporta hashes nuevos de Werkzeug (pbkdf2:...) y legacy sha256 hex.
    """
    if not stored_hash:
        return False
    # Werkz: empieza con "pbkdf2:" o "scrypt:" etc.
    if stored_hash.startswith(("pbkdf2:", "scrypt:", "argon2:")):
        return check_password_hash(stored_hash, plain)
    # Legacy: sha256 hexdigest sin sal
    legacy = hashlib.sha256((plain or "").encode()).hexdigest()
    return stored_hash == legacy


def _password_strength_error(password: str) -> Optional[str]:
    """Valida reglas básicas de complejidad de contraseña."""

    if not password or len(password) < 8:
        return "La nueva contraseña debe tener al menos 8 caracteres."
    if re.search(r"\s", password):
        return "La nueva contraseña no puede contener espacios."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "La nueva contraseña debe incluir letras y números."
    return None

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    success_message = session.pop('success_message', None)
    change_error = session.pop('change_error', None)
    show_change = session.pop('show_change', False)
    if request.method == 'POST':
        username = (request.form.get('username') or "").strip()
        password = (request.form.get('password') or "")

        conn = get_connection()
        try:
            c = conn.cursor()
            # Trae solo por username; la verificación del hash se hace en app
            c.execute(
                'SELECT id, username, password FROM usuarios WHERE username = %s',
                (username,)
            )
            user = c.fetchone()

            if user and _verify_password(user[2], password):
                # user -> (id, username, password)
                session.permanent = False  # Expira cuando se cierre el navegador
                session['user'] = user[1]

                # Roles centralizados
                roles = get_roles_by_user(user[0]) or []
                session['roles'] = roles
                session['rol'] = roles[0] if roles else None  # compatibilidad

                return redirect(url_for('chat.index'))
            else:
                error = 'Usuario o contraseña incorrectos'
        finally:
            conn.close()

    return render_template(
        'login.html',
        error=error,
        success=success_message,
        change_error=change_error,
        show_change=show_change,
    )

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@auth_bp.route('/password-change', methods=['POST'])
def password_change():
    username = (request.form.get('username') or "").strip()
    current_password = request.form.get('current_password') or ""
    new_password = request.form.get('new_password') or ""
    confirm_password = request.form.get('confirm_password') or ""

    error: Optional[str] = None

    if not username or not current_password or not new_password or not confirm_password:
        error = "Todos los campos son obligatorios."
    elif new_password != confirm_password:
        error = "La confirmación de la nueva contraseña no coincide."
    elif new_password == current_password:
        error = "La nueva contraseña debe ser diferente a la actual."
    else:
        strength_error = _password_strength_error(new_password)
        if strength_error:
            error = strength_error

    if error:
        session['change_error'] = error
        session['show_change'] = True
        return redirect(url_for('auth.login'))

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('SELECT id, password FROM usuarios WHERE username = %s', (username,))
        row = c.fetchone()

        if not row or not _verify_password(row[1], current_password):
            error = "Usuario o contraseña actual incorrectos."
        else:
            hashed = generate_password_hash(new_password)
            c.execute('UPDATE usuarios SET password = %s WHERE id = %s', (hashed, row[0]))
            conn.commit()
    finally:
        conn.close()

    if error:
        session['change_error'] = error
        session['show_change'] = True
    else:
        session['success_message'] = (
            "Contraseña actualizada correctamente. Ahora puedes iniciar sesión con tu nueva contraseña."
        )

    return redirect(url_for('auth.login'))
