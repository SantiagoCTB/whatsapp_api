import os
from flask import Blueprint, request, jsonify, url_for
from datetime import datetime
from config import Config
from services.db import get_connection, guardar_mensaje
from services.whatsapp_api import download_audio, get_media_url, enviar_mensaje

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN    = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT

user_last_activity = {}
user_steps         = {}

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
            msgs = change.get('value', {}).get('messages', []) or []
            if not msgs:
                continue

            msg         = msgs[0]
            msg_type    = msg.get('type')
            from_number = msg.get('from')
            mensaje_id  = msg.get('id')

            # evitar duplicados
            conn = get_connection(); c = conn.cursor()
            c.execute("SELECT 1 FROM mensajes_procesados WHERE mensaje_id = %s", (mensaje_id,))
            if c.fetchone():
                conn.close()
                return jsonify({'status':'duplicate'}), 200
            c.execute("INSERT INTO mensajes_procesados (mensaje_id) VALUES (%s)", (mensaje_id,))
            conn.commit(); conn.close()

            # AUDIO
            if msg_type == 'audio':
                media_id   = msg['audio']['id']
                mime_raw   = msg['audio'].get('mime_type', 'audio/ogg')
                mime_clean = mime_raw.split(';')[0].strip()
                ext        = mime_clean.split('/')[-1]

                audio_bytes = download_audio(media_id)
                filename    = f"{media_id}.{ext}"
                path        = os.path.join(Config.UPLOAD_FOLDER, filename)
                with open(path, 'wb') as f:
                    f.write(audio_bytes)

                public_url = url_for('static', filename=f'uploads/{filename}', _external=True)

                guardar_mensaje(
                    from_number,
                    "",
                    'audio',
                    media_id=media_id,
                    media_url=public_url,
                    mime_type=mime_clean
                )

                enviar_mensaje(from_number, "Audio recibido correctamente.", tipo='bot')
                continue

            if msg_type == 'video':
                media_id   = msg['video']['id']
                mime_raw   = msg['video'].get('mime_type', 'video/mp4')
                mime_clean = mime_raw.split(';')[0].strip()
                ext        = mime_clean.split('/')[-1]

                # 1) Descarga bytes y guardar en static/uploads
                media_bytes = download_audio(media_id)
                filename    = f"{media_id}.{ext}"
                path        = os.path.join(Config.UPLOAD_FOLDER, filename)
                with open(path, 'wb') as f:
                    f.write(media_bytes)

                # 2) URL pública
                public_url = url_for('static', filename=f'uploads/{filename}', _external=True)

                # 3) Guardar en BD
                guardar_mensaje(
                    from_number,
                    "",               # sin texto
                    'video',
                    media_id=media_id,
                    media_url=public_url,
                    mime_type=mime_clean
                )
                
                # 4) Confirmación al cliente
                enviar_mensaje(from_number, "Video recibido correctamente.", tipo='bot')
                continue

            # IMAGEN
            if msg_type == 'image':
                media_id  = msg['image']['id']
                media_url = get_media_url(media_id)
                guardar_mensaje(
                    from_number,
                    "",
                    'cliente_image',
                    media_id=media_id,
                    media_url=media_url
                )
                enviar_mensaje(from_number, "Imagen recibida correctamente.", tipo='bot')
                continue

            # TEXTO / INTERACTIVO
            if 'text' in msg:
                text = msg['text']['body'].strip().lower()
            elif 'interactive' in msg:
                text = (
                    msg['interactive'].get('list_reply', {}).get('title') or
                    msg['interactive'].get('button_reply', {}).get('title') or ''
                ).strip().lower()
            else:
                continue

            guardar_mensaje(from_number, text, 'cliente')

            now       = datetime.now()
            last_time = user_last_activity.get(from_number)
            if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
                enviar_mensaje(
                    from_number,
                    "Muchas gracias por comunicarte. La sesión terminó por inactividad."
                )
                user_steps.pop(from_number, None)
            user_last_activity[from_number] = now

            if text in ['reiniciar', 'volver al inicio', 'inicio', 'menú', 'menu', 'ayuda']:
                user_steps[from_number] = 'menu_principal'
                enviar_mensaje(from_number, "Perfecto, volvamos a empezar.")

                conn = get_connection(); c = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones, rol_keyword "
                    "FROM reglas WHERE step=%s AND input_text=%s",
                    ('menu_principal','iniciar')
                )
                row = c.fetchone(); conn.close()
                if row:
                    resp, next_step, tipo_resp, opts, rol_kw = row
                    enviar_mensaje(from_number, resp, tipo_respuesta=tipo_resp, opciones=opts)
                    if rol_kw:
                        conn2 = get_connection(); c2 = conn2.cursor()
                        c2.execute(
                            "INSERT IGNORE INTO chat_roles (numero, rol_keyword) VALUES (%s, %s)",
                            (from_number, rol_kw)
                        )
                        conn2.commit(); conn2.close()
                    if next_step:
                        user_steps[from_number] = next_step
                return jsonify({'status':'reiniciado'}), 200

            step = user_steps.get(from_number)
            if not step:
                step = 'menu_principal'
                user_steps[from_number] = step
                conn = get_connection(); c = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones, rol_keyword "
                    "FROM reglas WHERE step=%s AND input_text=%s",
                    (step,'iniciar')
                )
                row = c.fetchone(); conn.close()
                if row:
                    resp, next_step, _, _, rol_kw = row
                    enviar_mensaje(from_number, resp)
                    if rol_kw:
                        conn2 = get_connection(); c2 = conn2.cursor()
                        c2.execute(
                            "INSERT IGNORE INTO chat_roles (numero, rol_keyword) VALUES (%s, %s)",
                            (from_number, rol_kw)
                        )
                        conn2.commit(); conn2.close()
                    if next_step:
                        user_steps[from_number] = next_step
                return jsonify({'status':'sent_welcome'}), 200

            try:
                if step == 'barra_medida':
                    medida = int(text)
                    total  = medida * 1700
                    enviar_mensaje(
                        from_number,
                        f"Valor estimado: {total:,} $ Pesos.\nENVÍA 2 para asesor."
                    )
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status':'barra_ok'}), 200

                if step == 'meson_recto_medida':
                    medida = int(text)
                    total  = (medida + 100) * 1700
                    enviar_mensaje(
                        from_number,
                        f"Valor estimado: {total:,} $ Pesos.\nENVÍA 2 para asesor."
                    )
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status':'recto_ok'}), 200

                if step == 'meson_l_medida':
                    p1, p2 = map(int, text.replace(" ","").split("x"))
                    total  = (p1 + p2 + 40) * 1700
                    enviar_mensaje(
                        from_number,
                        f"Valor estimado: {total:,} $ Pesos.\nENVÍA 2 para asesor."
                    )
                    user_steps[from_number] = 'esperando_confirmacion'
                    return jsonify({'status':'l_ok'}), 200
            except:
                enviar_mensaje(from_number, "Por favor ingresa la medida correcta.")
                return jsonify({'status':'invalid_measure'}), 200

            conn = get_connection(); c = conn.cursor()
            c.execute(
                "SELECT respuesta, siguiente_step, tipo, opciones, rol_keyword "
                "FROM reglas WHERE step=%s AND input_text=%s",
                (step, text)
            )
            row = c.fetchone(); conn.close()
            if row:
                resp, next_step, tipo_resp, opts, rol_kw = row
                enviar_mensaje(from_number, resp, tipo_respuesta=tipo_resp, opciones=opts)
                if rol_kw:
                    conn2 = get_connection(); c2 = conn2.cursor()
                    c2.execute(
                        "INSERT IGNORE INTO chat_roles (numero, rol_keyword) VALUES (%s, %s)",
                        (from_number, rol_kw)
                    )
                    conn2.commit(); conn2.close()
                if next_step:
                    user_steps[from_number] = next_step
            else:
                enviar_mensaje(from_number, "No entendí tu respuesta, intenta de nuevo.")
    return jsonify({'status':'received'}), 200
