from flask import Flask
from dotenv import load_dotenv
import os

from services.db import init_db
from routes.auth_routes import auth_bp
from routes.chat_routes import chat_bp
from routes.configuracion import config_bp
from routes.webhook import webhook_bp

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')

# Registro de Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(chat_bp)
app.register_blueprint(config_bp)
app.register_blueprint(webhook_bp)

init_db()

if __name__ == '__main__':
    app.run(debug=True)
