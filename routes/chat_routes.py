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
    enviar_mensaje(numero, mensaje, tipo='asesor')
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
        requiere_asesor = False
        if ultimo and "asesor" in ultimo[0].lower():
            requiere_asesor = True
        chats.append({'numero': numero, 'asesor': requiere_asesor})

    conn.close()
    return jsonify(chats)
