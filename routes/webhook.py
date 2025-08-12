import os
from flask import Blueprint, request, jsonify, url_for
from datetime import datetime
from config import Config
from services.db import (
    get_connection,
    guardar_mensaje,
    get_chat_state,
    update_chat_state,
    delete_chat_state,
)
from services.whatsapp_api import download_audio, get_media_url, enviar_mensaje
from services.global_commands import handle_global_command

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN    = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT

user_last_activity = {}
user_steps         = {}

STEP_HANDLERS = {}
EXTERNAL_HANDLERS = {}


def register_handler(step):
    def decorator(func):
        STEP_HANDLERS[step] = func
        return func
    return decorator


def register_external(name):
    def decorator(func):
        EXTERNAL_HANDLERS[name] = func
        return func
    return decorator


def set_user_step(numero, step):
    """Actualiza el paso en memoria y en la tabla chat_state."""
    user_steps[numero] = step
    update_chat_state(numero, step)

os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)


@register_handler('barra_medida')
@register_handler('meson_recto_medida')
@register_handler('meson_l_medida')
def handle_medicion(numero, texto):
    step_actual = user_steps.get(numero, '').strip().lower()
    conn = get_connection(); c = conn.cursor()
    c.execute(
        "SELECT respuesta, siguiente_step, tipo, opciones, rol_keyword, calculo, handler "
        "FROM reglas WHERE step=%s AND input_text='*'",
        (step_actual,)
    )
    row = c.fetchone(); conn.close()
    if not row:
        return False
    resp, next_step, tipo_resp, opts, rol_kw, calculo, handler_name = row
    try:
        if handler_name:
            func = EXTERNAL_HANDLERS.get(handler_name)
            if not func:
                raise ValueError('handler no encontrado')
            total = func(texto)
        else:
            contexto = {}
            if calculo and 'p1' in calculo and 'p2' in calculo:
                p1, p2 = map(int, texto.replace(' ', '').split('x'))
                contexto.update({'p1': p1, 'p2': p2})
            else:
                contexto['medida'] = int(texto)
            total = eval(calculo, {}, contexto) if calculo else 0
        enviar_mensaje(numero, resp.format(total=total), tipo_respuesta=tipo_resp, opciones=opts)
        if rol_kw:
            conn2 = get_connection(); c2 = conn2.cursor()
            c2.execute("SELECT id FROM roles WHERE keyword=%s", (rol_kw,))
            role = c2.fetchone()
            if role:
                c2.execute(
                    "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                    (numero, role[0])
                )
                conn2.commit()
            conn2.close()
        set_user_step(numero, next_step.strip().lower() if next_step else '')
    except Exception:
        enviar_mensaje(numero, "Por favor ingresa la medida correcta.")
    return True

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

            if from_number not in user_steps:
                row = get_chat_state(from_number)
                if row:
                    step_db, last_act = row
                    user_steps[from_number] = step_db or ''
                    if last_act:
                        user_last_activity[from_number] = last_act

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
                delete_chat_state(from_number)
            user_last_activity[from_number] = now

            if handle_global_command(from_number, text):
                return jsonify({'status': 'handled_global'}), 200

            is_new_user = from_number not in user_steps
            stored_step = user_steps.get(from_number, '')
            if not is_new_user:
                update_chat_state(from_number, stored_step)
            step = stored_step.strip().lower() if stored_step else 'menu_principal'
            if is_new_user:
                conn = get_connection(); c = conn.cursor()
                c.execute(
                    "SELECT respuesta, siguiente_step, tipo, opciones, rol_keyword "
                    "FROM reglas WHERE step=%s AND input_text=%s",
                    (step,'iniciar')
                )
                row = c.fetchone(); conn.close()
                if row:
                    resp, next_step, tipo_resp, opts, rol_kw = row
                    enviar_mensaje(from_number, resp, tipo_respuesta=tipo_resp, opciones=opts)
                    if rol_kw:
                        conn2 = get_connection(); c2 = conn2.cursor()
                        c2.execute("SELECT id FROM roles WHERE keyword=%s", (rol_kw,))
                        role = c2.fetchone()
                        if role:
                            c2.execute(
                                "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                                (from_number, role[0])
                            )
                            conn2.commit()
                        conn2.close()
                    set_user_step(from_number, next_step.strip().lower() if next_step else '')
            step = user_steps.get(from_number, '').strip().lower()
            text = text.strip().lower()

            handler = STEP_HANDLERS.get(step)
            if handler and handler(from_number, text):
                return jsonify({'status':'handled'}), 200
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
                    c2.execute("SELECT id FROM roles WHERE keyword=%s", (rol_kw,))
                    role = c2.fetchone()
                    if role:
                        c2.execute(
                            "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                            (from_number, role[0])
                        )
                        conn2.commit()
                    conn2.close()
                set_user_step(from_number, next_step.strip().lower() if next_step else '')
            else:
                enviar_mensaje(from_number, "No entendí tu respuesta, intenta de nuevo.")
    return jsonify({'status':'received'}), 200
