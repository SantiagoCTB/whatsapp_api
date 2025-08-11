from flask import Blueprint, render_template, request, redirect, session, url_for
import hashlib
from services.db import get_connection, get_roles_by_user

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        hashed = hashlib.sha256(password.encode()).hexdigest()

        conn = get_connection()
        c = conn.cursor()
        c.execute(
            'SELECT id, username, password FROM usuarios WHERE username = %s AND password = %s',
            (username, hashed)
        )
        user = c.fetchone()
        conn.close()

        if user:
            # user tuple: (id, username, password)
            session['user'] = user[1]
            roles = get_roles_by_user(user[0])
            session['roles'] = roles
            # compatibilidad con código existente
            session['rol'] = roles[0] if roles else None
            return redirect(url_for('chat.index'))  # redirige a la ruta principal
        else:
            error = 'Usuario o contraseña incorrectos'

    return render_template('login.html', error=error)

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))

