from flask import (
    Blueprint,
    request,
    redirect,
    session,
    url_for,
    send_from_directory,
    current_app,
)
import os
import hashlib
from werkzeug.security import check_password_hash
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

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
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
                session['user'] = user[1]

                # Roles centralizados
                roles = get_roles_by_user(user[0]) or []
                session['roles'] = roles
                session['rol'] = roles[0] if roles else None  # compatibilidad

                return redirect(url_for('chat.index'))
            else:
                # En caso de error, regresar a la interfaz de login
                return redirect(url_for('auth.login'))
        finally:
            conn.close()

    # Para GET, sirve la aplicación de React
    dist_dir = os.path.join(current_app.root_path, 'frontend', 'dist')
    return send_from_directory(dist_dir, 'index.html')

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
