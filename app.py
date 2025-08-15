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

# Carga .env y crea la app
load_dotenv()
os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = os.getenv('SECRET_KEY')

# --- Inicializa la base de datos inmediatamente, al importar app.py ---
init_db()

# Registro de Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(config_bp)
app.register_blueprint(roles_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(tablero_bp)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
