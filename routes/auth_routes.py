from flask import Blueprint, render_template, request, redirect, session, url_for
from werkzeug.security import check_password_hash
from services.db import get_connection

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        conn = get_connection()
        c = conn.cursor()
        c.execute(
            'SELECT id, username, password, rol FROM usuarios WHERE username = %s',
            (username,)
        )
        user = c.fetchone()
        conn.close()

        if user and check_password_hash(user[2], password):
            # user tuple: (id, username, password, rol)
            session['user'] = user[1]
            session['rol'] = user[3]
            return redirect(url_for('chat.index'))  # redirige a la ruta principal
        else:
            error = 'Usuario o contraseña incorrectos'

    return render_template('login.html', error=error)

@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))
