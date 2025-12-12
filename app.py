# app.py
from flask import Flask
from dotenv import load_dotenv
import os
import logging
import sys

load_dotenv()

from config import Config

from services.db import init_db
from routes.auth_routes import auth_bp
from routes.chat_routes import chat_bp
from routes.configuracion import config_bp
from routes.roles_routes import roles_bp
from routes.users_routes import users_bp
from routes.webhook import webhook_bp
from routes.tablero_routes import tablero_bp
from routes.export_routes import export_bp
from routes.backup_routes import backup_bp
from services.backup import start_daily_backup_scheduler

def _ensure_media_root():
    """Create the directory where user uploads are stored."""
    media_root = Config.MEDIA_ROOT
    try:
        os.makedirs(media_root, exist_ok=True)
        logging.getLogger(__name__).info("MEDIA_ROOT inicializado en %s", media_root)
    except OSError as exc:
        raise RuntimeError(
            f"No se pudo preparar el directorio de medios en '{media_root}'."
        ) from exc

    static_uploads = os.path.join(Config.BASEDIR, "static", "uploads")
    if os.path.abspath(static_uploads) != media_root:
        logging.getLogger(__name__).warning(
            "MEDIA_ROOT (%s) es distinto del directorio estándar de static/uploads. "
            "Asegúrate de exponerlo correctamente en tu servidor.",
            media_root,
        )


def create_app():
    _ensure_media_root()
    app = Flask(
        __name__,
        static_folder=os.path.join(Config.BASEDIR, "static"),
        template_folder=os.path.join(Config.BASEDIR, "templates"),
    )
    # Si usas clase de config:
    app.config.from_object(Config)

    if not app.debug:
        log_format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
        logging.basicConfig(
            level=logging.INFO,
            format=log_format,
            handlers=[
                logging.FileHandler('app.log'),
                logging.StreamHandler(sys.stdout)
            ]
        )

    # Registra blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(roles_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(tablero_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(backup_bp)

    # Inicializa BD por defecto para evitar errores en entornos nuevos.
    # Puede deshabilitarse con INIT_DB_ON_START=0 si se prefiere controlar
    # la migración manualmente.
    if os.getenv("INIT_DB_ON_START", "1") != "0":
        with app.app_context():
            init_db()

    start_daily_backup_scheduler()

    return app

# Objeto WSGI para Gunicorn
app = create_app()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
