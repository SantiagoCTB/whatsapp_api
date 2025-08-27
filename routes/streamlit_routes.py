import os

from flask import Blueprint, render_template_string

from config import Config

streamlit_bp = Blueprint('streamlit', __name__, url_prefix='/streamlit')

@streamlit_bp.route('/')
def dashboard():
    """Render an iframe embedding the Streamlit dashboard."""
    iframe_url = os.getenv("STREAMLIT_URL", Config.STREAMLIT_URL)
    iframe_html = (
        f"<iframe src='{iframe_url}' style='width:100%; height:100vh; border:none;'></iframe>"
    )
    return render_template_string(iframe_html)
