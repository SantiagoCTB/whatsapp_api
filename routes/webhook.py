import os
import re
import logging
import threading
from flask import Blueprint, request, jsonify, url_for
from datetime import datetime
from difflib import SequenceMatcher
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
from services.job_queue import enqueue_transcription
from services.normalize_text import normalize_text

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN    = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT
DEFAULT_FALLBACK_TEXT = "No entendí tu respuesta, intenta de nuevo."

user_last_activity = {}
user_steps         = {}
# Mapa numero -> id de regla "en-hilo" pendiente de evaluar
pending_rules      = {}
# Mapa numero -> lista de textos recibidos para procesar tras un delay
message_buffer     = {}
pending_timers     = {}

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


def set_user_step(numero, step, estado='espera_usuario'):
    """Actualiza el paso en memoria y en la tabla chat_state."""
    user_steps[numero] = step
    update_chat_state(numero, step, estado)

os.makedirs(Config.MEDIA_ROOT, exist_ok=True)


@register_handler('barra_medida')
@register_handler('meson_recto_medida')
@register_handler('meson_l_medida')
def handle_medicion(numero, texto):
    step_actual = user_steps.get(numero, '').strip().lower()
    conn = get_connection(); c = conn.cursor()
    c.execute(
        """
        SELECT r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword, r.calculo, r.handler
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s AND r.input_text='*'
         GROUP BY r.id
        """,
        (step_actual,)
    )
    row = c.fetchone(); conn.close()
    if not row:
        return False
    resp, next_step, tipo_resp, media_urls, opts, rol_kw, calculo, handler_name = row
    media_list = media_urls.split('||') if media_urls else []
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
        if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
            enviar_mensaje(numero, resp.format(total=total), tipo_respuesta=tipo_resp, opciones=media_list[0])
            for extra in media_list[1:]:
                enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
        else:
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


def handle_text_message(numero, texto, fallback_text: str = DEFAULT_FALLBACK_TEXT):
    now           = datetime.now()
    last_time     = user_last_activity.get(numero)
    session_reset = False

    # Expiración de sesión
    if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
        user_steps.pop(numero, None)
        delete_chat_state(numero)
        session_reset = True
    user_last_activity[numero] = now

    # Comandos globales
    if handle_global_command(numero, texto):
        return

    # Paso inicial y bienvenida si es usuario nuevo
    is_new_user = numero not in user_steps
    stored_step = user_steps.get(numero, '')
    if not is_new_user:
        update_chat_state(numero, stored_step)
    step = stored_step.strip().lower() if stored_step else 'menu_principal'

    if is_new_user:
        conn = get_connection(); c = conn.cursor()
        c.execute(
            """
            SELECT r.id, r.respuesta, r.siguiente_step, r.tipo,
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   r.opciones, r.rol_keyword
              FROM reglas r
              LEFT JOIN regla_medias m ON r.id = m.regla_id
             WHERE r.step=%s AND r.input_text=%s
             GROUP BY r.id
            """,
            (step, 'iniciar')
        )
        row = c.fetchone(); conn.close()
        if row:
            _, resp, next_step, tipo_resp, media_urls, opts, rol_kw = row
            media_list = media_urls.split('||') if media_urls else []
            if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
                enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
                for extra in media_list[1:]:
                    enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
            else:
                enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)

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
            if next_step:
                trigger_auto_steps(numero)
                return

    if session_reset:
        return

    # Step actual
    step = user_steps.get(numero, '').strip().lower()

    # Handler específico del step
    handler = STEP_HANDLERS.get(step)
    if handler and handler(numero, texto):
        return

    texto_norm = normalize_text(texto or '')

    # Cargar reglas del step actual (incluye r.id e input_text)
    conn = get_connection(); c = conn.cursor()
    c.execute(
        """
        SELECT r.id, r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword, r.input_text
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s
         GROUP BY r.id
        """,
        (step,)
    )
    reglas = c.fetchall(); conn.close()

    # ===============  A) REGRA EN HILO CON '*'  ===============
    # Ver si hay una regla "en-hilo" pendiente para este número
    regla_hilo_id = pending_rules.get(numero)  # id de la regla que estamos esperando evaluar
    if regla_hilo_id is not None:
        r_activa = next((r for r in reglas if str(r[0]) == str(regla_hilo_id)), None)
        if r_activa:
            _id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, input_db = r_activa
            if (input_db or '').strip() == '*':
                media_list = media_urls.split('||') if media_urls else []
                if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
                    enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
                    for extra in media_list[1:]:
                        enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
                else:
                    enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)

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

                set_user_step(numero, (next_step or '').strip().lower())
                pending_rules.pop(numero, None)  # limpiar en-hilo
                if next_step:
                    trigger_auto_steps(numero)
                return
        # Si la activa no es '*', seguimos con la lógica normal

    # ===============  B) MATCH EXACTO (sin '*')  ===============
    row = None
    for _id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, input_db in reglas:
        if (input_db or '').strip() == '*':
            continue  # el * no participa en matching; se usa solo en A) o D)
        triggers = [normalize_text(t.strip()) for t in (input_db or '').split(',') if t.strip()]
        if not triggers:
            continue

        matched = False
        for trigger in triggers:
            if not trigger:
                continue
            if ' ' in trigger:
                if trigger in texto_norm:
                    matched = True
                    break
            else:
                if re.search(rf"\b{re.escape(trigger)}\b", texto_norm):
                    matched = True
                    break

        if matched:
            media_list = media_urls.split('||') if media_urls else []
            row = (_id, resp, next_step, tipo_resp, media_list, opts, rol_kw)
            break

    # ===============  C) MATCH "FUZZY" (sin '*')  ===============
    if not row:
        words = texto_norm.split()
        for _id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, input_db in reglas:
            if (input_db or '').strip() == '*':
                continue
            triggers = [normalize_text(t.strip()) for t in (input_db or '').split(',') if t.strip()]
            if not triggers:
                continue
            found = False
            for trigger in triggers:
                for word in words:
                    if SequenceMatcher(None, word, trigger).ratio() >= 0.8:
                        found = True
                        break
                if found:
                    break
            if found:
                media_list = media_urls.split('||') if media_urls else []
                row = (_id, resp, next_step, tipo_resp, media_list, opts, rol_kw)
                break

    # ===============  D) CATCH-ALL CON '*' (solo si hay uno)  ===============
    if not row:
        wildcard_reglas = [r for r in reglas if (r[7] or '').strip() == '*']
        # Si hay exactamente UNA regla con '*', úsala como catch-all del step
        if len(wildcard_reglas) == 1:
            _id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, _ = wildcard_reglas[0]
            media_list = media_urls.split('||') if media_urls else []
            if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
                enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
                for extra in media_list[1:]:
                    enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
            else:
                enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)

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

            set_user_step(numero, (next_step or '').strip().lower())
            pending_rules.pop(numero, None)  # por si venía de una regla en-hilo
            if next_step:
                trigger_auto_steps(numero)
            return

    # ===============  E) RESPONDER SI HUBO MATCH (B/C)  ===============
    if row:
        _id, resp, next_step, tipo_resp, media_list, opts, rol_kw = row
        if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
            enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
            for extra in media_list[1:]:
                enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
        else:
            enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)

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

        set_user_step(numero, (next_step or '').strip().lower())
        pending_rules.pop(numero, None)
        if next_step:
            trigger_auto_steps(numero)
        return

    # ===============  F) FALLBACK  ===============
    enviar_mensaje(numero, fallback_text)
    guardar_mensaje(numero, fallback_text, "bot")
    # (Asegúrate de que update_chat_state acepte estos parámetros)
    update_chat_state(numero, step, 'sin_regla')

def set_en_hilo(numero, regla_id):
    """Registra que el número tiene una regla en-hilo pendiente."""
    pending_rules[numero] = regla_id


def trigger_auto_steps(numero):
    """Busca y ejecuta en cadena las reglas automáticas ('*') del step actual."""
    step = user_steps.get(numero, '').strip().lower()
    if not step:
        return

    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT id FROM reglas WHERE step=%s AND input_text='*'", (step,))
    row = c.fetchone(); conn.close()
    if row:
        regla_id = row[0]
        logging.info("Transición automática para %s: step '%s' con regla %s", numero, step, regla_id)
        set_en_hilo(numero, regla_id)


def process_buffered_messages(numero):
    textos = message_buffer.get(numero)
    if not textos:
        return
    combined = " ".join(textos)
    normalized = normalize_text(combined)
    handle_text_message(numero, normalized)
    message_buffer.pop(numero, None)
    timer = pending_timers.pop(numero, None)
    if timer:
        timer.cancel()

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
            for msg in msgs:
                msg_type    = msg.get('type')
                from_number = msg.get('from')
                wa_id       = msg.get('id')
                reply_to_id = msg.get('context', {}).get('id')

                if from_number not in user_steps:
                    row = get_chat_state(from_number)
                    if row:
                        step_db, last_act = row
                        user_steps[from_number] = step_db or ''
                        if last_act:
                            user_last_activity[from_number] = last_act

                # evitar duplicados
                conn = get_connection(); c = conn.cursor()
                c.execute("SELECT 1 FROM mensajes_procesados WHERE mensaje_id = %s", (wa_id,))
                if c.fetchone():
                    conn.close()
                    continue
                c.execute("INSERT INTO mensajes_procesados (mensaje_id) VALUES (%s)", (wa_id,))
                conn.commit(); conn.close()

                if msg.get("referral"):
                    ref = msg["referral"]
                    guardar_mensaje(
                        from_number,
                        "",
                        "referral",
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        link_url=ref.get("source_url"),
                        link_title=ref.get("headline"),
                        link_body=ref.get("body"),
                        link_thumb=ref.get("thumbnail_url"),
                    )
                    continue

                # AUDIO
                if msg_type == 'audio':
                    media_id   = msg['audio']['id']
                    mime_raw   = msg['audio'].get('mime_type', 'audio/ogg')
                    mime_clean = mime_raw.split(';')[0].strip()
                    ext        = mime_clean.split('/')[-1]

                    audio_bytes = download_audio(media_id)
                    filename = f"{media_id}.{ext}"
                    path = os.path.join(Config.MEDIA_ROOT, filename)
                    with open(path, 'wb') as f:
                        f.write(audio_bytes)

                    public_url = url_for('static', filename=f'uploads/{filename}', _external=True)

                    db_id = guardar_mensaje(
                        from_number,
                        "",
                        'audio',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        media_id=media_id,
                        media_url=public_url,
                        mime_type=mime_clean,
                    )

                    queued = enqueue_transcription(
                        path,
                        from_number,
                        media_id,
                        mime_clean,
                        public_url,
                        db_id,
                    )
                    if queued:
                        logging.info("Audio encolado para transcripción: %s", media_id)
                    else:
                        logging.warning("No se pudo encolar audio %s para transcripción", media_id)
                    continue

                if msg_type == 'video':
                    media_id   = msg['video']['id']
                    mime_raw   = msg['video'].get('mime_type', 'video/mp4')
                    mime_clean = mime_raw.split(';')[0].strip()
                    ext        = mime_clean.split('/')[-1]

                    # 1) Descarga bytes y guardar en static/uploads
                    media_bytes = download_audio(media_id)
                    filename    = f"{media_id}.{ext}"
                    path        = os.path.join(Config.MEDIA_ROOT, filename)
                    with open(path, 'wb') as f:
                        f.write(media_bytes)

                    # 2) URL pública
                    public_url = url_for('static', filename=f'uploads/{filename}', _external=True)

                    # 3) Guardar en BD
                    guardar_mensaje(
                        from_number,
                        "",               # sin texto
                        'video',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        media_id=media_id,
                        media_url=public_url,
                        mime_type=mime_clean
                    )

                    # 4) Registro interno
                    logging.info("Video recibido: %s", media_id)
                    continue

                # IMAGEN
                if msg_type == 'image':
                    media_id  = msg['image']['id']
                    media_url = get_media_url(media_id)
                    guardar_mensaje(
                        from_number,
                        "",
                        'cliente_image',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        media_id=media_id,
                        media_url=media_url
                    )
                    logging.info("Imagen recibida: %s", media_id)
                    continue

                # TEXTO / INTERACTIVO
                if 'text' in msg:
                    text = msg['text']['body'].strip()
                elif 'interactive' in msg:
                    text = (
                        msg['interactive'].get('list_reply', {}).get('title') or
                        msg['interactive'].get('button_reply', {}).get('title') or ''
                    ).strip()
                else:
                    continue

                normalized_text = normalize_text(text)

                guardar_mensaje(
                    from_number,
                    text,
                    'cliente',
                    wa_id=wa_id,
                    reply_to_wa_id=reply_to_id,
                )
                message_buffer.setdefault(from_number, []).append(normalized_text)
                if from_number in pending_timers:
                    pending_timers[from_number].cancel()
                timer = threading.Timer(10, process_buffered_messages, args=(from_number,))
                pending_timers[from_number] = timer
                timer.start()
                return jsonify({'status': 'buffered'}), 200
    return jsonify({'status':'received'}), 200
