from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
import sqlite3
from config import Config
from services.whatsapp_api import enviar_mensaje

chat_bp = Blueprint('chat', __name__)

@chat_bp.route('/')
def index():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT numero FROM mensajes")
    numeros = [row[0] for row in c.fetchall()]
    chats = []
    for numero in numeros:
        c.execute("SELECT mensaje FROM mensajes WHERE numero = ? ORDER BY timestamp DESC LIMIT 1", (numero,))
        ultimo = c.fetchone()
        requiere_asesor = False
        if ultimo and "asesor" in ultimo[0].lower():
            requiere_asesor = True
        chats.append((numero, requiere_asesor))

    # Leer botones
    c.execute("SELECT id, mensaje FROM botones ORDER BY id")
    botones = c.fetchall()

    conn.close()
    return render_template('index.html', chats=chats, botones=botones)

@chat_bp.route('/get_chat/<numero>')
def get_chat(numero):
    if "user" not in session:
        return redirect(url_for("auth.login"))
    
    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT mensaje, tipo, timestamp FROM mensajes WHERE numero = ? ORDER BY timestamp", (numero,))
    mensajes = c.fetchall()
    conn.close()
    return jsonify({'mensajes': mensajes})

@chat_bp.route('/send_message', methods=['POST'])
def send_message():
    if "user" not in session:
        return redirect(url_for("auth.login"))
    
    data = request.get_json()
    numero = data.get('numero')
    mensaje = data.get('mensaje')
    enviar_mensaje(numero, mensaje, tipo='asesor')  # <=== importante cambio aquí
    return jsonify({'status': 'success'})

@chat_bp.route('/get_chat_list')
def get_chat_list():
    if "user" not in session:
        return redirect(url_for("auth.login"))

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT numero FROM mensajes")
    numeros = [row[0] for row in c.fetchall()]

    chats = []
    for numero in numeros:
        c.execute("SELECT mensaje FROM mensajes WHERE numero = ? ORDER BY timestamp DESC LIMIT 1", (numero,))
        ultimo = c.fetchone()

        c.execute("SELECT nombre FROM alias WHERE numero = ?", (numero,))
        alias = c.fetchone()
        alias_nombre = alias[0] if alias else None

        requiere_asesor = False
        if ultimo and "asesor" in ultimo[0].lower():
            requiere_asesor = True
        chats.append({"numero": numero, "asesor": requiere_asesor, "alias": alias_nombre})

    conn.close()
    return jsonify(chats)

@chat_bp.route('/set_alias', methods=['POST'])
def set_alias():
    if "user" not in session:
        return jsonify({"error": "No autorizado"}), 401

    data = request.get_json()
    numero = data.get('numero')
    nombre = data.get('nombre')

    conn = sqlite3.connect(Config.DB_PATH)
    c = conn.cursor()
    c.execute("REPLACE INTO alias (numero, nombre) VALUES (?, ?)", (numero, nombre))
    conn.commit()
    conn.close()

    return jsonify({"status": "ok"})