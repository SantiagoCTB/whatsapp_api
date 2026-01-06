import os

from flask import Blueprint, send_from_directory

from config import Config

landing_bp = Blueprint("landing", __name__)

LANDING_DIR = os.path.join(Config.BASEDIR, "landing")


@landing_bp.route("/privacidad")
def privacidad():
    return send_from_directory(LANDING_DIR, "privacidad.html", mimetype="text/html")


@landing_bp.route("/aviso")
def aviso():
    return send_from_directory(LANDING_DIR, "aviso.html", mimetype="text/html")


@landing_bp.route("/terminos")
def terminos():
    return send_from_directory(LANDING_DIR, "terminos.html", mimetype="text/html")
