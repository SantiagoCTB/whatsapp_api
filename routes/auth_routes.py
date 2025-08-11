from flask import Blueprint, render_template, request, redirect, session, url_for
import hashlib
from services.db import get_connection


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

        if user:
            c.execute(
                'SELECT role_name FROM user_roles WHERE user_id = %s',
                (user[0],)
            )
            roles = [r[0] for r in c.fetchall()]
            conn.close()
            session['user'] = user[1]
            session['roles'] = roles
            return redirect(url_for('chat.index'))
        else:
            conn.close()
            error = 'Usuario o contraseña incorrectos'

    return render_template('login.html', error=error)


@auth_bp.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('roles', None)
    return redirect(url_for('auth.login'))

