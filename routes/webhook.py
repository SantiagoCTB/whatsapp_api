import os
from flask import Blueprint, request, jsonify, send_file, url_for, abort
from config import Config
from services.db import get_connection, guardar_mensaje
from services.whatsapp_api import (
    enviar_mensaje,
    get_media_url,
    subir_media,
    download_audio
)
from datetime import datetime

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT
MEDIA_FOLDER = Config.MEDIA_FOLDER
os.makedirs(MEDIA_FOLDER, exist_ok=True)

# Tracking sessions
user_last_activity = {}
user_steps = {}

@webhook_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token == VERIFY_TOKEN:
            return challenge, 200
        return 'Forbidden', 403

    data = request.get_json() or {}
    if not data.get('object'):
        return jsonify({'status': 'no_object'}), 400

    for entry in data.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value', {})
            messages = value.get('messages', []) or []
            if not messages:
                continue

            msg = messages[0]
            msg_type = msg.get('type')
            mensaje_id = msg.get('id')
            from_number = msg.get('from')

            # Text message
            if msg_type == 'text':
                text = msg['text']['body'].strip().lower()

            # Interactive: list or button
            elif msg_type == 'interactive':
                list_reply = msg['interactive'].get('list_reply', {})
                button_reply = msg['interactive'].get('button_reply', {})
                text = (list_reply.get('title') or button_reply.get('title') or '').strip().lower()

            # Image
            elif msg_type == 'image':
                media_id = msg['image']['id']
                media_url = get_media_url(media_id)
                guardar_mensaje(from_number, mensaje_id, 'cliente_image', media_id=media_id, media_url=media_url)
                enviar_mensaje(from_number, "Imagen recibida correctamente.", tipo='bot')
                continue

            # Audio
            elif msg_type == 'audio':
                audio_id = msg['audio']['id']
                mime     = msg['audio'].get('mime_type', 'audio/ogg')

                # 1) Descargar y guardar localmente
                audio_bytes = download_audio(audio_id)
                ext         = mime.split('/')[-1]
                filename    = f"{audio_id}.{ext}"
                path        = os.path.join(MEDIA_FOLDER, filename)
                with open(path, 'wb') as f:
                    f.write(audio_bytes)

                # 2) Generar URL pública para servir el audio
                public_url = url_for('webhook.serve_audio', media_id=audio_id, _external=True)

                # 3) Persistir en la base de datos con media_url y mime_type
                guardar_mensaje(
                    from_number,
                    "",           # texto vacío
                    'audio',      # tipo
                    media_id=audio_id,
                    media_url=public_url,
                    mime_type=mime
                )

                # 4) Confirmación al cliente
                enviar_mensaje(from_number, "Audio recibido correctamente.", tipo='bot')
                continue

            else:
                return jsonify({'status': 'unsupported_message_type'}), 200

            # Duplicate check
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

            # Save client text message
            guardar_mensaje(from_number, text, 'cliente')

            # Session timeout handling
            now = datetime.now()
            last_time = user_last_activity.get(from_number)
            if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                enviar_mensaje(
                    from_number,
                    "Muchas gracias por comunicarte con nosotros. La sesión se dará por terminada por inactividad. ¡Te esperamos nuevamente por aquí!"
                )
                user_steps.pop(from_number, None)
            user_last_activity[from_number] = now

            # Restart keywords
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
                        tipo='bot',
                        tipo_respuesta=tipo_respuesta,
                        opciones=opciones
                    )
                    if siguiente:
                        user_steps[from_number] = siguiente
                return jsonify({'status': 'reiniciado'}), 200

            # Determine current step
            step = user_steps.get(from_number)

            # Send initial welcome if no step
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

            # Business logic for measurements
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

            except ValueError:
                enviar_mensaje(from_number, "Por favor ingresa la medida correctamente. Ej: 150 o 200 x 150")
                return jsonify({'status': 'invalid_measure'}), 200

            # Dynamic rules lookup
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


@webhook_bp.route('/media/audio/<media_id>')
def serve_audio(media_id):
    """
    Devuelve el archivo de audio correspondiente al media_id,
    probando varias extensiones y retornando un 404 si no existe.
    """
    for ext in ('ogg', 'mp3', 'wav', 'm4a', 'bin'):
        path = os.path.join(MEDIA_FOLDER, f"{media_id}.{ext}")
        if os.path.isfile(path):
            mime = f"audio/{ext}" if ext != 'bin' else 'application/octet-stream'
            return send_file(path, mimetype=mime)
    # Si no encontramos nada, 404
    abort(404)