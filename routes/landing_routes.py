import os

from flask import Blueprint, abort, current_app
from werkzeug.utils import safe_join

from config import Config

landing_bp = Blueprint("landing", __name__)

LANDING_DIR = os.path.join(Config.BASEDIR, "landing")


def _normalize_filename(filename: str) -> str:
    """Ensure filenames always point to an HTML document."""

    if not filename:
        return "index.html"

    if filename.endswith("/"):
        filename = os.path.join(filename, "index.html")

    if not filename.lower().endswith(".html"):
        filename = f"{filename}.html"

    return filename


def _serve_landing_page(filename: str):
    """Serve a landing page with explicit logging and HTML content type."""

    normalized_filename = _normalize_filename(filename)
    safe_path = safe_join(LANDING_DIR, normalized_filename)

    if not safe_path or not os.path.isfile(safe_path):
        current_app.logger.error("Landing page not found: %s", safe_path)
        abort(404)

    with open(safe_path, "r", encoding="utf-8") as fp:
        html_content = fp.read()

    response = current_app.response_class(
        html_content, status=200, content_type="text/html; charset=utf-8"
    )
    current_app.logger.info(
        "Landing page served",
        extra={"path": safe_path, "content_type": response.mimetype},
    )
    return response


@landing_bp.route("/privacidad", strict_slashes=False)
def privacidad():
    return _serve_landing_page("privacidad.html")


@landing_bp.route("/aviso", strict_slashes=False)
def aviso():
    return _serve_landing_page("aviso.html")


@landing_bp.route("/terminos", strict_slashes=False)
def terminos():
    return _serve_landing_page("terminos.html")


@landing_bp.route("/landing", defaults={"filename": "index"}, strict_slashes=False)
@landing_bp.route("/landing/<path:filename>", strict_slashes=False)
def landing(filename: str):
    """Serve any landing or sub-landing HTML stored under the landing directory."""

    return _serve_landing_page(filename)
