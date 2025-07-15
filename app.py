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

                    # Evitar duplicados
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT 1 FROM mensajes_procesados WHERE mensaje_id = ?", (mensaje_id,))
                    if c.fetchone():
                        conn.close()
                        return jsonify({"status": "duplicate_ignored"})
                    c.execute("INSERT INTO mensajes_procesados (mensaje_id) VALUES (?)", (mensaje_id,))
                    conn.commit()
                    conn.close()

                    guardar_mensaje(from_number, text, 'cliente')

                    now = datetime.now()
                    last_time = user_last_activity.get(from_number)

                    if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                        msg = "Muchas gracias por comunicarte con nosotros, la sesión se dará por terminada ya que no recibimos respuesta, te esperamos nuevamente por aquí !"
                        enviar_mensaje(from_number, msg)
                        user_steps.pop(from_number, None)

                    user_last_activity[from_number] = now
                    step = user_steps.get(from_number, 'menu_principal')

                    if step == 'menu_principal':
                        menu = (
                            "Hola! Es un gusto para nosotros en Aceros Tecnimedellín atenderte! Somos especialistas en TODO en ACERO.\n\n"
                            "Para atenderte mira nuestras opciones y ÚNICAMENTE manda el NÚMERO de la opción que estás interesado/a:\n\n"
                            "1. Hacer una cotización\n"
                            "2. Averiguar por el estado de mi pedido\n"
                            "3. Quiero ver el catálogo\n"
                            "4. Comunicarme con un asesor"
                        )
                        enviar_mensaje(from_number, menu)
                        user_steps[from_number] = 'esperando_opcion_principal'

                    elif step == 'esperando_opcion_principal':
                        if text == '1':
                            submenu = (
                                "¿Qué tipo de producto deseas cotizar?\n\n"
                                "1. Barra recta sin lavaplatos\n"
                                "2. Mesón recto con lavaplatos\n"
                                "3. Mesón en L con lavaplatos\n"
                                "4. Comunicarme con un asesor"
                            )
                            enviar_mensaje(from_number, submenu)
                            user_steps[from_number] = 'cotizacion_tipo'

                        elif text == '4':
                            msg = "Un asesor te contactará pronto."
                            enviar_mensaje(from_number, msg)
                            user_steps[from_number] = 'menu_principal'

                        else:
                            msg = "Por favor responde con una opción válida del 1 al 4."
                            enviar_mensaje(from_number, msg)

                    elif step == 'cotizacion_tipo':
                        if text == '1':
                            enviar_mensaje(from_number, "Ingrese la medida de la BARRA en CENTÍMETROS, Ejemplo: 150")
                            user_steps[from_number] = 'barra_medida'

                        elif text == '2':
                            enviar_mensaje(from_number, "Ingrese la medida del mesón recto con lavaplatos en CENTÍMETROS, Ejemplo: 150")
                            user_steps[from_number] = 'meson_recto_medida'

                        elif text == '3':
                            enviar_mensaje(from_number, "Ingrese las medidas del mesón en L (ejemplo: 200 x 150).")
                            # enviar_mensaje(from_number, "[Imagen ilustrativa]")
                            user_steps[from_number] = 'meson_l_medida'

                        elif text == '4':
                            enviar_mensaje(from_number, "Te conectaremos con un asesor.")
                            user_steps[from_number] = 'menu_principal'
                            return jsonify({"status": "waiting_for_asesor"})

                        else:
                            enviar_mensaje(from_number, "Opción no válida. Responde con 1 a 4.")

                    elif step == 'barra_medida':
                        try:
                            medida = int(text)
                            total = medida * 1700
                            respuesta = f"El valor estimado para tu barra de largo {medida} cm es: {total:,} $ Pesos.\nSi quieres hacer la orden y comunicarte con un asesor, ENVÍA 2."
                            enviar_mensaje(from_number, respuesta)
                            user_steps[from_number] = 'esperando_confirmacion'
                        except:
                            enviar_mensaje(from_number, "Por favor, ingresa solo la medida en números. Ejemplo: 150")

                    elif step == 'meson_recto_medida':
                        try:
                            medida = int(text)
                            total = (medida + 100) * 1700
                            respuesta = f"El valor estimado para tu mesón recto es: {total:,} $ Pesos.\nSi quieres hacer la orden y comunicarte con un asesor, ENVÍA 2."
                            enviar_mensaje(from_number, respuesta)
                            user_steps[from_number] = 'esperando_confirmacion'
                        except:
                            enviar_mensaje(from_number, "Por favor, ingresa solo la medida en números. Ejemplo: 150")

                    elif step == 'meson_l_medida':
                        try:
                            parte1, parte2 = map(int, text.lower().replace(" ", "").split("x"))
                            total = (parte1 + parte2 + 40) * 1700
                            respuesta = f"El valor estimado para tu mesón en L es: {total:,} $ Pesos.\nSi quieres hacer la orden y comunicarte con un asesor, ENVÍA 2."
                            enviar_mensaje(from_number, respuesta)
                            user_steps[from_number] = 'esperando_confirmacion'
                        except:
                            enviar_mensaje(from_number, "Por favor ingresa el formato correctamente: 200 x 150")

                    elif step == 'esperando_confirmacion':
                        if text == '2':
                            enviar_mensaje(from_number, "Un asesor te contactará pronto para finalizar tu pedido.")
                            user_steps[from_number] = 'menu_principal'
                        else:
                            enviar_mensaje(from_number, "Si deseas comunicarte con un asesor para tu pedido, ENVÍA 2.")

    return jsonify({"status": "received"})

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
