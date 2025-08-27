from flask import Blueprint, render_template_string

streamlit_bp = Blueprint('streamlit', __name__, url_prefix='/streamlit')

@streamlit_bp.route('/')
def dashboard():
    """Render an iframe embedding the Streamlit dashboard."""
    iframe_html = (
        "<iframe src='http://localhost:8501' style='width:100%; height:100vh; border:none;'></iframe>"
    )
    return render_template_string(iframe_html)
