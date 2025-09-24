# asgi.py
from app import app as flask_app  # <-- si tu Flask está en app.py y la instancia se llama 'app'
from asgiref.wsgi import WsgiToAsgi

asgi_app = WsgiToAsgi(flask_app)
