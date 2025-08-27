# app.py
from flask import Flask
from dotenv import load_dotenv
import os
from config import Config

from services.db import init_db
from routes.auth_routes import auth_bp
from routes.chat_routes import chat_bp
from routes.configuracion import config_bp
from routes.roles_routes import roles_bp
from routes.webhook import webhook_bp
from routes.tablero_routes import tablero_bp

load_dotenv()

def create_app():
    app = Flask(__name__)
    # Si usas clase de config:
    app.config.from_object(Config)

    # Registra blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(roles_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(tablero_bp)

    # Inicializa BD solo si se pide explícitamente y dentro del app_context
    if os.getenv("INIT_DB_ON_START", "0") == "1":
        with app.app_context():
            init_db()

    return app

# Objeto WSGI para Gunicorn
app = create_app()

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
