from flask import Blueprint, render_template, request, redirect, session, url_for
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
    error = None
    if request.method == 'POST':
        # Selección de rol posterior al login
        if 'rol' in request.form and not request.form.get('username'):
            selected_role = request.form.get('rol')
            roles = session.get('roles', [])
            if selected_role in roles:
                session['rol'] = selected_role
                return redirect(url_for('chat.index'))
            error = 'Rol inválido'
            return render_template('select_role.html', roles=roles, error=error)

        username = (request.form.get('username') or "").strip()
        password = (request.form.get('password') or "")

        conn = get_connection()
        try:
            c = conn.cursor()
            # Trae solo por username; la verificación del hash se hace en app
            c.execute(
                'SELECT id, username, password FROM usuarios WHERE username = %s',
                (username,),
            )
            user = c.fetchone()

            if user and _verify_password(user[2], password):
                roles = get_roles_by_user(user[0]) or []
                if not roles:
                    error = 'Usuario sin rol asignado'
                else:
                    session['user'] = user[1]
                    session['roles'] = roles
                    if len(roles) == 1:
                        session['rol'] = roles[0]
                        return redirect(url_for('chat.index'))
                    return render_template('select_role.html', roles=roles)
            else:
                error = 'Usuario o contraseña incorrectos'
        finally:
            conn.close()

    return render_template('login.html', error=error)

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
