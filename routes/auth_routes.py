from flask import Blueprint, render_template, request, redirect, session, url_for
import hashlib
from services.db import get_connection, get_roles_by_user

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = (request.form.get('password') or "").strip()
        hashed = hashlib.sha256(password.encode()).hexdigest()

        conn = get_connection()
        try:
            c = conn.cursor()
            # Estándar: usuarios SIN columna 'rol' aquí; roles van por tabla relacional
            c.execute(
                'SELECT id, username, password FROM usuarios WHERE username = %s AND password = %s',
                (username, hashed)
            )
            user = c.fetchone()

            if user:
                # user -> (id, username, password)
                session['user'] = user[1]

                # Roles centralizados
                roles = get_roles_by_user(user[0]) or []
                session['roles'] = roles

                # Compatibilidad con código existente
                session['rol'] = roles[0] if roles else None

                return redirect(url_for('chat.index'))
            else:
                error = 'Usuario o contraseña incorrectos'
        finally:
            conn.close()

    return render_template('login.html', error=error)

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
