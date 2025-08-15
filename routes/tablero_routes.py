from flask import Blueprint, render_template, session, redirect, url_for, current_app


tablero_bp = Blueprint('tablero', __name__)


@tablero_bp.route('/tablero')
def tablero():
    """Renderiza la página del tablero con gráficos de Streamlit."""
    if "user" not in session:
        return redirect(url_for('auth.login'))
    streamlit_url = current_app.config["STREAMLIT_URL"]
    return render_template('tablero.html', streamlit_url=streamlit_url)
