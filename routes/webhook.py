from flask import Blueprint, request, jsonify
from config import Config
from services.db import guardar_mensaje
from services.whatsapp_api import enviar_mensaje
import sqlite3

webhook_bp = Blueprint('webhook', __name__)

user_last_activity = {}
user_steps = {}

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
                    text = message['text']['body'].strip().lower()

                    # Verificar duplicados
                    conn = sqlite3.connect(Config.DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT 1 FROM mensajes_procesados WHERE mensaje_id = ?", (mensaje_id,))
                    if c.fetchone():
                        conn.close()
                        return jsonify({"status": "duplicate_ignored"})
                    c.execute("INSERT INTO mensajes_procesados (mensaje_id) VALUES (?)", (mensaje_id,))
                    conn.commit()
                    conn.close()

                    guardar_mensaje(from_number, text, 'cliente')

                    # Timeout
                    now = datetime.now()
                    last_time = user_last_activity.get(from_number)
                    if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                        enviar_mensaje(from_number, "Muchas gracias por comunicarte con nosotros. La sesión se dará por terminada por inactividad. ¡Te esperamos nuevamente por aquí!")
                        user_steps.pop(from_number, None)
                    user_last_activity[from_number] = now

                    # Palabras clave para reiniciar
                    if text in ['reiniciar', 'volver al inicio', 'inicio', 'menú', 'menu', 'ayuda']:
                        user_steps.pop(from_number, None)
                        user_steps[from_number] = 'menu_principal'
                        enviar_mensaje(from_number, "Perfecto, volvamos a empezar.")

                        conn = sqlite3.connect(Config.DB_PATH)
                        c = conn.cursor()
                        c.execute("SELECT respuesta, siguiente_step FROM reglas WHERE step = 'menu_principal' AND input_text = 'iniciar'")
                        bienvenida = c.fetchone()
                        conn.close()

                        if bienvenida:
                            enviar_mensaje(from_number, bienvenida[0])
                            if bienvenida[1]:
                                user_steps[from_number] = bienvenida[1]
                        return jsonify({"status": "reiniciado"})

                    # Paso actual
                    step = user_steps.get(from_number)

                    # Si no hay paso, iniciar con mensaje de bienvenida
                    if not step:
                        step = 'menu_principal'
                        user_steps[from_number] = step

                        conn = sqlite3.connect(Config.DB_PATH)
                        c = conn.cursor()
                        c.execute("SELECT respuesta, siguiente_step FROM reglas WHERE step = ? AND input_text = ?", (step, 'iniciar'))
                        bienvenida = c.fetchone()
                        conn.close()

                        if bienvenida:
                            enviar_mensaje(from_number, bienvenida[0])
                            if bienvenida[1]:
                                user_steps[from_number] = bienvenida[1]  # ✅ se guarda el paso correcto
                        return jsonify({"status": "sent_welcome"})

                    # Lógica de cotización con medidas
                    try:
                        if step == 'barra_medida':
                            medida = int(text)
                            total = medida * 1700
                            respuesta = f"El valor estimado para tu barra de largo {medida} cm es: {total:,} $ Pesos.\nSi deseas comunicarte con un asesor, ENVÍA 2."
                            enviar_mensaje(from_number, respuesta)
                            user_steps[from_number] = 'esperando_confirmacion'
                            return jsonify({"status": "barra_ok"})

                        elif step == 'meson_recto_medida':
                            medida = int(text)
                            total = (medida + 100) * 1700
                            respuesta = f"El valor estimado para tu mesón recto es: {total:,} $ Pesos.\nSi deseas comunicarte con un asesor, ENVÍA 2."
                            enviar_mensaje(from_number, respuesta)
                            user_steps[from_number] = 'esperando_confirmacion'
                            return jsonify({"status": "recto_ok"})

                        elif step == 'meson_l_medida':
                            partes = text.replace(" ", "").split("x")
                            if len(partes) == 2:
                                parte1, parte2 = map(int, partes)
                                total = (parte1 + parte2 + 40) * 1700
                                respuesta = f"El valor estimado para tu mesón en L es: {total:,} $ Pesos.\nSi deseas comunicarte con un asesor, ENVÍA 2."
                                enviar_mensaje(from_number, respuesta)
                                user_steps[from_number] = 'esperando_confirmacion'
                                return jsonify({"status": "l_ok"})
                            else:
                                raise ValueError("Formato inválido")

                    except Exception as e:
                        enviar_mensaje(from_number, "Por favor ingresa la medida correctamente. Ej: 150 o 200 x 150")
                        return jsonify({"status": "invalid_measure"})

                    # Consultar reglas desde la base
                    conn = sqlite3.connect(Config.DB_PATH)
                    c = conn.cursor()
                    c.execute("SELECT respuesta, siguiente_step, tipo FROM reglas WHERE step = ? AND input_text = ?", (step, text))
                    regla = c.fetchone()
                    conn.close()

                    if regla:
                        respuesta, siguiente, tipo = regla
                        enviar_mensaje(from_number, respuesta)
                        if siguiente:
                            user_steps[from_number] = siguiente
                    else:
                        enviar_mensaje(from_number, "Lo siento, no entendí tu respuesta. Por favor intenta nuevamente.")

    return jsonify({"status": "received"})

