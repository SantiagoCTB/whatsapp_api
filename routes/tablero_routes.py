from flask import Blueprint, render_template, session, redirect, url_for


tablero_bp = Blueprint('tablero', __name__)


@tablero_bp.route('/tablero')
def tablero():
    """Renderiza la página del tablero con gráficos de Streamlit."""
    if "user" not in session:
        return redirect(url_for('auth.login'))
    return render_template('tablero.html')
