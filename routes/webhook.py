import os
from flask import Blueprint, request, jsonify, url_for
from datetime import datetime
from config import Config
from services.db import get_connection, guardar_mensaje
from services.whatsapp_api import download_audio, enviar_mensaje

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN    = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT

user_last_activity = {}
user_steps         = {}

# Asegurar que static/uploads exista
os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

@webhook_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        token     = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if token == VERIFY_TOKEN:
            return challenge, 200
        return 'Forbidden', 403

    data = request.get_json() or {}
    if not data.get('object'):
        return jsonify({'status': 'no_object'}), 400

    for entry in data.get('entry', []):
        for change in entry.get('changes', []):
            messages = change.get('value', {}).get('messages', []) or []
            if not messages:
                continue

            msg         = messages[0]
            msg_type    = msg.get('type')
            from_number = msg.get('from')
            mensaje_id  = msg.get('id')

            # Evitar duplicados
            conn = get_connection()
            c    = conn.cursor()
            c.execute(
                "SELECT 1 FROM mensajes_procesados WHERE mensaje_id = %s",
                (mensaje_id,)
            )
            if c.fetchone():
                conn.close()
                return jsonify({'status': 'duplicate'}), 200
            c.execute(
                "INSERT INTO mensajes_procesados (mensaje_id) VALUES (%s)",
                (mensaje_id,)
            )
            conn.commit()
            conn.close()

            # ─── AUDIO ─────────────────────────────────────────────────────────
            if msg_type == 'audio':
                media_id = msg['audio']['id']
                mime     = msg['audio'].get('mime_type', 'audio/ogg')
                ext      = mime.split('/')[-1]

                # Descargar y guardar en static/uploads
                audio_bytes = download_audio(media_id)
                filename    = f"{media_id}.{ext}"
                path        = os.path.join(Config.UPLOAD_FOLDER, filename)
                with open(path, 'wb') as f:
                    f.write(audio_bytes)

                # URL pública para el audio
                public_url = url_for(
                    'static',
                    filename=f'uploads/{filename}',
                    _external=True
                )

                # Guardar en la base de datos
                guardar_mensaje(
                    from_number,
                    "",              # sin texto
                    'audio',
                    media_id=media_id,
                    media_url=public_url,
                    mime_type=mime
                )

                # Confirmación al cliente
                enviar_mensaje(
                    from_number,
                    "Audio recibido correctamente.",
                    tipo='bot'
                )
                continue

            # ─── TEXTO E INTERACTIVOS ─────────────────────────────────────────
            if 'text' in msg:
                text = msg['text']['body'].strip().lower()
            elif 'interactive' in msg:
                text = (
                    msg['interactive'].get('list_reply', {}).get('title') or
                    msg['interactive'].get('button_reply', {}).get('title') or ''
                ).strip().lower()
            else:
                continue

            # Guardar texto del cliente
            guardar_mensaje(from_number, text, 'cliente')

            # Manejo de inactividad
            now       = datetime.now()
            last_time = user_last_activity.get(from_number)
            if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                enviar_mensaje(
                    from_number,
                    "Muchas gracias por comunicarte con nosotros. "
                    "La sesión se dará por terminada por inactividad. ¡Te esperamos nuevamente!"
                )
                user_steps.pop(from_number, None)
            user_last_activity[from_number] = now

            # Palabras clave para reiniciar
            if text in ['reiniciar', 'volver al inicio', 'inicio', 'menú', 'menu', 'ayuda']:
                user_steps[from_number] = 'menu_principal'
                enviar_mensaje(from_number, "Perfecto, volvamos a empezar.")

                conn = get_connection()
                c    = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones "
                    "FROM reglas WHERE step = %s AND input_text = %s",
                    ('menu_principal', 'iniciar')
                )
                bienvenida = c.fetchone()
                conn.close()

                if bienvenida:
                    texto_respuesta, siguiente, tipo_respuesta, opciones = bienvenida
                    enviar_mensaje(
                        from_number,
                        texto_respuesta,
                        tipo_respuesta=tipo_respuesta,
                        opciones=opciones
                    )
                    if siguiente:
                        user_steps[from_number] = siguiente
                return jsonify({'status': 'reiniciado'}), 200

            # Paso actual
            step = user_steps.get(from_number)
            if not step:
                step = 'menu_principal'
                user_steps[from_number] = step

                conn = get_connection()
                c    = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones "
                    "FROM reglas WHERE step = %s AND input_text = %s",
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

            # Lógica de medidas para cotización
            try:
                if step == 'barra_medida':
                    medida = int(text)
                    total  = medida * 1700
                    enviar_mensaje(
                        from_number,
                        f"El valor estimado para tu barra de largo {medida} cm es: {total:,} $ Pesos.\n"
                        "Si deseas comunicarte con un asesor, ENVÍA 2."
                    )
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status': 'barra_ok'}), 200

                if step == 'meson_recto_medida':
                    medida = int(text)
                    total  = (medida + 100) * 1700
                    enviar_mensaje(
                        from_number,
                        f"El valor estimado para tu mesón recto es: {total:,} $ Pesos.\n"
                        "Si deseas comunicarte con un asesor, ENVÍA 2."
                    )
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status': 'recto_ok'}), 200

                if step == 'meson_l_medida':
                    partes = text.replace(" ", "").split("x")
                    if len(partes) == 2:
                        p1, p2 = map(int, partes)
                        total  = (p1 + p2 + 40) * 1700
                        enviar_mensaje(
                            from_number,
                            f"El valor estimado para tu mesón en L es: {total:,} $ Pesos.\n"
                            "Si deseas comunicarte con un asesor, ENVÍA 2."
                        )
                        user_steps[from_number] = 'esperando_confirmacion'
                        return jsonify({'status': 'l_ok'}), 200
                    raise ValueError("Formato inválido")

            except Exception:
                enviar_mensaje(
                    from_number,
                    "Por favor ingresa la medida correctamente. Ej: 150 o 200 x 150"
                )
                return jsonify({'status': 'invalid_measure'}), 200

            # Reglas dinámicas
            conn = get_connection()
            c    = conn.cursor()
            c.execute(
                "SELECT respuesta, siguiente_step, tipo, opciones "
                "FROM reglas WHERE step = %s AND input_text = %s",
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
                enviar_mensaje(
                    from_number,
                    "Lo siento, no entendí tu respuesta. Por favor intenta nuevamente."
                )

    return jsonify({'status': 'received'}), 200
