from flask import Flask, request, jsonify, render_template, session, redirect, url_for, flash
import requests
import sqlite3
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import hashlib

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY')

META_TOKEN = os.getenv('META_TOKEN')
PHONE_NUMBER_ID = os.getenv('PHONE_NUMBER_ID')
VERIFY_TOKEN = os.getenv('VERIFY_TOKEN')

DB_PATH = 'database.db'
SESSION_TIMEOUT = 600  # 10 minutos
user_last_activity = {}
user_steps = {}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Tabla mensajes
    c.execute('''
        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT,
            mensaje TEXT,
            tipo TEXT,
            timestamp TEXT
        )
    ''')
    
    # Tabla mensajes procesados
    c.execute('''
        CREATE TABLE IF NOT EXISTS mensajes_procesados (
            mensaje_id TEXT PRIMARY KEY
        )
    ''')

    # Tabla de usuarios
    c.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            rol TEXT NOT NULL
        )
    ''')

    # Tabla de reglas de automatización
    c.execute('''
        CREATE TABLE IF NOT EXISTS reglas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step TEXT NOT NULL,
            input_text TEXT NOT NULL,
            respuesta TEXT NOT NULL,
            siguiente_step TEXT,
            tipo TEXT DEFAULT 'texto'
        )
    ''')

    # Crear usuario admin si no existe
    c.execute("SELECT * FROM usuarios WHERE username = 'admin'")
    if not c.fetchone():
        import hashlib
        password = 'admin123'
        hashed = hashlib.sha256(password.encode()).hexdigest()
        c.execute("INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)",
                  ('admin', hashed, 'admin'))

    conn.commit()
    conn.close()

init_db()

def enviar_mensaje(numero, mensaje, tipo='bot'):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_TOKEN}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": mensaje}
    }
    requests.post(url, headers=headers, json=data)
    guardar_mensaje(numero, mensaje, tipo)

def guardar_mensaje(numero, mensaje, tipo):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO mensajes (numero, mensaje, tipo, timestamp) VALUES (?, ?, ?, ?)",
              (numero, mensaje, tipo, str(datetime.now())))
    conn.commit()
    conn.close()

from flask import render_template, request, redirect, session, url_for

@app.route('/configuracion', methods=['GET', 'POST'])
def configuracion():
    if "user" not in session or session["rol"] != "admin":
        return redirect(url_for("login"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if request.method == 'POST':
        step = request.form['step']
        input_text = request.form['input_text']
        respuesta = request.form['respuesta']
        siguiente_step = request.form['siguiente_step']
        tipo = request.form['tipo']

        # Si ya existe una regla con ese step + input_text, actualizamos
        c.execute('''
            SELECT id FROM reglas WHERE step = ? AND input_text = ?
        ''', (step, input_text))
        existente = c.fetchone()

        if existente:
            c.execute('''
                UPDATE reglas
                SET respuesta = ?, siguiente_step = ?, tipo = ?
                WHERE id = ?
            ''', (respuesta, siguiente_step, tipo, existente[0]))
        else:
            c.execute('''
                INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo)
                VALUES (?, ?, ?, ?, ?)
            ''', (step, input_text, respuesta, siguiente_step, tipo))

        conn.commit()

    c.execute("SELECT * FROM reglas ORDER BY step, id")
    reglas = c.fetchall()
    conn.close()

    return render_template('configuracion.html', reglas=reglas)


@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge'), 200
        return 'Forbidden', 403

    data = request.get_json()

    if data.get('object'):
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                messages = value.get('messages')
                if messages:
                    message = messages[0]
                    mensaje_id = message.get('id')
                    from_number = message['from']
                    text = message['text']['body'].strip()

                    # Verificar si el mensaje ya fue procesado
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT 1 FROM mensajes_procesados WHERE mensaje_id = ?", (mensaje_id,))
                    if c.fetchone():
                        conn.close()
                        return jsonify({"status": "duplicate_ignored"})
                    c.execute("INSERT INTO mensajes_procesados (mensaje_id) VALUES (?)", (mensaje_id,))
                    conn.commit()
                    conn.close()

                    # Guardar el mensaje del cliente
                    guardar_mensaje(from_number, text, 'cliente')

                    # Verificar timeout de sesión
                    now = datetime.now()
                    last_time = user_last_activity.get(from_number)

                    if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                        msg = "Muchas gracias por comunicarte con nosotros. La sesión se dará por terminada ya que no recibimos respuesta. ¡Te esperamos nuevamente por aquí!"
                        enviar_mensaje(from_number, msg)
                        user_steps.pop(from_number, None)

                    user_last_activity[from_number] = now

                    # Obtener el paso actual del usuario
                    step = user_steps.get(from_number)

                    # 🟢 Si no hay paso (primera vez), mostrar bienvenida automáticamente
                    if not step:
                        step = 'menu_principal'
                        user_steps[from_number] = step
                        conn = sqlite3.connect(DB_PATH)
                        c = conn.cursor()
                        c.execute("SELECT respuesta, siguiente_step FROM reglas WHERE step = ? AND input_text = ?", (step, 'iniciar'))
                        regla = c.fetchone()
                        conn.close()

                        if regla:
                            enviar_mensaje(from_number, regla[0])
                            if regla[1]:
                                user_steps[from_number] = regla[1]
                        return jsonify({"status": "sent_welcome"})

                    # 🔄 Buscar regla correspondiente al paso y texto ingresado
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT respuesta, siguiente_step FROM reglas WHERE step = ? AND input_text = ?", (step, text))
                    regla = c.fetchone()
                    conn.close()

                    if regla:
                        enviar_mensaje(from_number, regla[0])
                        if regla[1]:
                            user_steps[from_number] = regla[1]
                    else:
                        enviar_mensaje(from_number, "Lo siento, no entendí tu respuesta. Por favor intenta nuevamente.")

    return jsonify({"status": "received"})

@app.route('/eliminar_regla/<int:regla_id>', methods=['POST'])
def eliminar_regla(regla_id):
    if "user" not in session or session["rol"] != "admin":
        return redirect(url_for("login"))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM reglas WHERE id = ?", (regla_id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for("configuracion"))


@app.route('/')
def index():
    if "user" not in session:
        return redirect(url_for("login"))

    conn = sqlite3.connect(DB_PATH)
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
    conn.close()
    return render_template('index.html', chats=chats)


@app.route('/get_chat/<numero>')
def get_chat(numero):
    if "user" not in session:
        return redirect(url_for("login"))
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT mensaje, tipo, timestamp FROM mensajes WHERE numero = ? ORDER BY timestamp", (numero,))
    mensajes = c.fetchall()
    conn.close()
    return jsonify({'mensajes': mensajes})

@app.route('/send_message', methods=['POST'])
def send_message():
    if "user" not in session:
        return redirect(url_for("login"))
    
    data = request.get_json()
    numero = data.get('numero')
    mensaje = data.get('mensaje')
    enviar_mensaje(numero, mensaje, tipo='asesor')  # <=== importante cambio aquí
    return jsonify({'status': 'success'})

@app.route('/get_chat_list')
def get_chat_list():
    if "user" not in session:
        return redirect(url_for("login"))
    
    conn = sqlite3.connect(DB_PATH)
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


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        hashed = hashlib.sha256(password.encode()).hexdigest()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM usuarios WHERE username = ? AND password = ?", (username, hashed))
        user = c.fetchone()
        conn.close()

        if user:
            session["user"] = user[1]
            session["rol"] = user[3]
            return redirect("/")
        else:
            error = "Usuario o contraseña incorrectos"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == '__main__':
    app.run(debug=True)
