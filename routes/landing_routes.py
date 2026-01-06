import os

from flask import Blueprint, abort, current_app, send_file

from config import Config

landing_bp = Blueprint("landing", __name__)

LANDING_DIR = os.path.join(Config.BASEDIR, "landing")


def _serve_landing_page(filename: str):
    """Serve a landing page with explicit logging and HTML content type."""

    path = os.path.join(LANDING_DIR, filename)
    if not os.path.isfile(path):
        current_app.logger.error("Landing page not found: %s", path)
        abort(404)

    response = send_file(path, mimetype="text/html; charset=utf-8")
    current_app.logger.info(
        "Landing page served", extra={"path": path, "content_type": response.mimetype}
    )
    return response


@landing_bp.route("/privacidad")
def privacidad():
    return _serve_landing_page("privacidad.html")


@landing_bp.route("/aviso")
def aviso():
    return _serve_landing_page("aviso.html")


@landing_bp.route("/terminos")
def terminos():
    return _serve_landing_page("terminos.html")
