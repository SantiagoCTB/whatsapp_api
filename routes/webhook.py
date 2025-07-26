from flask import Blueprint, request, jsonify
from config import Config
from services.db import get_connection, guardar_mensaje
from services.whatsapp_api import enviar_mensaje, get_media_url
from datetime import datetime

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT


# Para tracking de sesiones
user_last_activity = {}
user_steps = {}

@webhook_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Verificación de webhook
        if request.args.get('hub.verify_token') == VERIFY_TOKEN:
            return request.args.get('hub.challenge'), 200
        return 'Forbidden', 403

    data = request.get_json()
    if not data.get('object'):
        return jsonify({'status': 'no_object'}), 400

    for entry in data.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            messages = value.get('messages')
            if not messages:
                continue

            message = messages[0]
            mensaje_id = message.get('id')
            from_number = message.get('from')

            # Extraer texto
            if 'text' in message:
                text = message['text']['body'].strip().lower()
            elif 'interactive' in message:
                text = (
                    message['interactive'].get('list_reply', {}).get('title') or
                    message['interactive'].get('button_reply', {}).get('title') or ''
                ).strip().lower()
            
            elif 'image' in message:
                media_id  = message['image']['id']
                media_url = get_media_url(media_id)
                # guardamos en MySQL con tipo cliente_image
                guardar_mensaje(from_number, None, 'cliente_image', media_id, media_url)
                # opcional: envía una confirmación al cliente
                enviar_mensaje(from_number, "Imagen recibida correctamente.", tipo='bot')
                continue
            else:
                return jsonify({'status': 'unsupported_message_type'}), 200

            # Verificar duplicados
            conn = get_connection()
            c = conn.cursor()
            c.execute(
                "SELECT 1 FROM mensajes_procesados WHERE mensaje_id = %s",
                (mensaje_id,)
            )
            if c.fetchone():
                conn.close()
                return jsonify({'status': 'duplicate_ignored'}), 200
            c.execute(
                "INSERT INTO mensajes_procesados (mensaje_id) VALUES (%s)",
                (mensaje_id,)
            )
            conn.commit()
            conn.close()

            # Guardar mensaje de cliente
            guardar_mensaje(from_number, text, 'cliente')

            # Manejo de timeout
            now = datetime.now()
            last_time = user_last_activity.get(from_number)
            if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                enviar_mensaje(
                    from_number,
                    "Muchas gracias por comunicarte con nosotros. La sesión se dará por terminada por inactividad. ¡Te esperamos nuevamente por aquí!"
                )
                user_steps.pop(from_number, None)
            user_last_activity[from_number] = now

            # Palabras clave para reiniciar
            if text in ['reiniciar', 'volver al inicio', 'inicio', 'menú', 'menu', 'ayuda']:
                user_steps[from_number] = 'menu_principal'
                enviar_mensaje(from_number, "Perfecto, volvamos a empezar.")

                conn = get_connection()
                c = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones FROM reglas "
                    "WHERE step = %s AND input_text = %s",
                    ('menu_principal', 'iniciar')
                )
                bienvenida = c.fetchone()
                conn.close()

                if bienvenida:
                    texto_respuesta, siguiente, tipo_respuesta, opciones = bienvenida
                    enviar_mensaje(
                        from_number,
                        texto_respuesta,
                        tipo= 'bot',
                        tipo_respuesta=tipo_respuesta,
                        opciones=opciones
                    )
                    if siguiente:
                        user_steps[from_number] = siguiente
                return jsonify({'status': 'reiniciado'}), 200

            # Obtener paso actual
            step = user_steps.get(from_number)

            # Si no hay paso, enviar bienvenida inicial
            if not step:
                step = 'menu_principal'
                user_steps[from_number] = step

                conn = get_connection()
                c = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones FROM reglas "
                    "WHERE step = %s AND input_text = %s",
                    (step, 'iniciar')
                )
                bienvenida = c.fetchone()
                conn.close()

                if bienvenida:
                    respuesta, siguiente, *_ = bienvenida
                    enviar_mensaje(from_number, respuesta)
                    if siguiente:
                        user_steps[from_number] = siguiente
                return jsonify({'status': 'sent_welcome'}), 200

            # Lógica de medidas (cotización)
            try:
                if step == 'barra_medida':
                    medida = int(text)
                    total = medida * 1700
                    respuesta = (
                        f"El valor estimado para tu barra de largo {medida} cm es: {total:,} $ Pesos."
                        "\nSi deseas comunicarte con un asesor, ENVÍA 2."
                    )
                    enviar_mensaje(from_number, respuesta)
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status': 'barra_ok'}), 200

                if step == 'meson_recto_medida':
                    medida = int(text)
                    total = (medida + 100) * 1700
                    respuesta = (
                        f"El valor estimado para tu mesón recto es: {total:,} $ Pesos."
                        "\nSi deseas comunicarte con un asesor, ENVÍA 2."
                    )
                    enviar_mensaje(from_number, respuesta)
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status': 'recto_ok'}), 200

                if step == 'meson_l_medida':
                    partes = text.replace(" ", "").split("x")
                    if len(partes) == 2:
                        p1, p2 = map(int, partes)
                        total = (p1 + p2 + 40) * 1700
                        respuesta = (
                            f"El valor estimado para tu mesón en L es: {total:,} $ Pesos."
                            "\nSi deseas comunicarte con un asesor, ENVÍA 2."
                        )
                        enviar_mensaje(from_number, respuesta)
                        user_steps[from_number] = 'esperando_confirmacion'
                        return jsonify({'status': 'l_ok'}), 200
                    raise ValueError("Formato inválido")

            except Exception:
                enviar_mensaje(from_number, "Por favor ingresa la medida correctamente. Ej: 150 o 200 x 150")
                return jsonify({'status': 'invalid_measure'}), 200

            # Consultar reglas dinámicas
            conn = get_connection()
            c = conn.cursor()
            c.execute(
                "SELECT respuesta, siguiente_step, tipo, opciones FROM reglas "
                "WHERE step = %s AND input_text = %s",
                (step, text)
            )
            regla = c.fetchone()
            conn.close()

            if regla:
                respuesta, siguiente, tipo_respuesta, opciones_raw = regla
                enviar_mensaje(
                    from_number,
                    respuesta,
                    tipo_respuesta=tipo_respuesta,
                    opciones=opciones_raw
                )
                if siguiente:
                    user_steps[from_number] = siguiente
            else:
                enviar_mensaje(from_number, "Lo siento, no entendí tu respuesta. Por favor intenta nuevamente.")

    return jsonify({'status': 'received'}), 200
