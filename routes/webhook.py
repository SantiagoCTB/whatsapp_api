import os
import logging
import threading
import json
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
from services.job_queue import enqueue_transcription
from services.normalize_text import normalize_text
from services.global_commands import handle_global_command

webhook_bp = Blueprint('webhook', __name__)

VERIFY_TOKEN    = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT
DEFAULT_FALLBACK_TEXT = "No entendí tu respuesta, intenta de nuevo."

# Mapa numero -> lista de textos recibidos para procesar tras un delay
message_buffer     = {}
pending_timers     = {}
cache_lock         = threading.Lock()

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
    """Actualiza el paso en la tabla chat_state."""
    update_chat_state(numero, step, estado)


def get_current_step(numero):
    row = get_chat_state(numero)
    return (row[0] or '').strip().lower() if row else ''

os.makedirs(Config.MEDIA_ROOT, exist_ok=True)


def _get_step_from_options(opciones_json, option_id):
    try:
        data = json.loads(opciones_json or '')
    except Exception:
        return None
    if isinstance(data, list):
        # Puede ser lista de secciones o botones
        if data and isinstance(data[0], dict) and data[0].get('reply'):
            for b in data:
                if b.get('reply', {}).get('id') == option_id:
                    nxt = b.get('step') or b.get('next_step')
                    return (nxt or '').strip().lower() or None
        sections = data
    elif isinstance(data, dict):
        sections = data.get('sections', [])
    else:
        sections = []
    for sec in sections:
        for row in sec.get('rows', []):
            if row.get('id') == option_id:
                nxt = row.get('step') or row.get('next_step')
                return (nxt or '').strip().lower() or None
    return None


def handle_option_reply(numero, option_id):
    if not option_id:
        return False
    step = get_current_step(numero)
    if not step:
        return False
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT opciones FROM reglas WHERE step=%s", (step,))
    rows = c.fetchall(); conn.close()
    for (opcs,) in rows:
        nxt = _get_step_from_options(opcs or '', option_id)
        if nxt:
            advance_steps(numero, nxt)
            return True
    return False


def dispatch_rule(numero, regla, step=None):
    """Envía la respuesta definida en una regla y asigna roles si aplica."""
    regla_id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, _ = regla
    current_step = step or get_current_step(numero)
    media_list = media_urls.split('||') if media_urls else []
    if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
        enviar_mensaje(
            numero,
            resp,
            tipo_respuesta=tipo_resp,
            opciones=media_list[0],
            step=current_step,
            regla_id=regla_id,
        )
        for extra in media_list[1:]:
            enviar_mensaje(
                numero,
                '',
                tipo_respuesta=tipo_resp,
                opciones=extra,
                step=current_step,
                regla_id=regla_id,
            )
    else:
        enviar_mensaje(
            numero,
            resp,
            tipo_respuesta=tipo_resp,
            opciones=opts,
            step=current_step,
            regla_id=regla_id,
        )
    if rol_kw:
        conn = get_connection(); c = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (rol_kw,))
        role = c.fetchone()
        if role:
            c.execute(
                "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                (numero, role[0])
            )
            conn.commit()
        conn.close()
    advance_steps(numero, next_step)


def advance_steps(numero: str, steps_str: str):
    """Avanza múltiples pasos enviando las reglas comodín correspondientes.

    El procesamiento de la lista de pasos ocurre únicamente en memoria; solo
    se persiste el último paso mediante ``set_user_step``. No se almacena el
    detalle de la lista en la base de datos.
    """
    steps = [s.strip().lower() for s in (steps_str or '').split(',') if s.strip()]
    if not steps:
        return
    for step in steps[:-1]:
        conn = get_connection(); c = conn.cursor()
        c.execute(
            """
            SELECT r.id, r.respuesta, r.siguiente_step, r.tipo,
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   r.opciones, r.rol_keyword, r.input_text
              FROM reglas r
              LEFT JOIN regla_medias m ON r.id = m.regla_id
             WHERE r.step=%s AND r.input_text='*'
             GROUP BY r.id
             ORDER BY r.id
             LIMIT 1
            """,
            (step,),
        )
        regla = c.fetchone(); conn.close()
        if regla:
            dispatch_rule(numero, regla, step)
    set_user_step(numero, steps[-1])




def process_step_chain(numero, text_norm=None):
    """Procesa el step actual una sola vez.

    Las reglas con ``input_text='*'`` pueden ejecutarse incluso si no se
    recibió texto del usuario, pero tras la primera ejecución el flujo se
    detiene y espera una nueva entrada.
    """
    step = get_current_step(numero)
    if not step:
        return

    conn = get_connection(); c = conn.cursor()
    # Ordenar reglas para evaluar primero las de menor ID (o prioridad).
    c.execute(
        """
        SELECT r.id, r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword, r.input_text
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s
         GROUP BY r.id
         ORDER BY r.id
        """,
        (step,),
    )
    reglas = c.fetchall(); conn.close()
    if not reglas:
        return

    comodines = [r for r in reglas if (r[7] or '').strip() == '*']

    # No avanzar si no hay texto del usuario, salvo que existan comodines
    if text_norm is None and not comodines:
        return

    # Coincidencia exacta
    for r in reglas:
        patt = (r[7] or '').strip()
        if patt and patt != '*' and normalize_text(patt) == text_norm:
            dispatch_rule(numero, r, step)
            return

    # Regla comodín
    if comodines:
        dispatch_rule(numero, comodines[0], step)
        # No procesar recursivamente otros comodines; esperar nueva entrada
        return

    logging.warning("Fallback en step '%s' para entrada '%s'", step, text_norm)
    enviar_mensaje(numero, DEFAULT_FALLBACK_TEXT)
    update_chat_state(numero, get_current_step(numero), 'sin_regla')


@register_handler('barra_medida')
@register_handler('meson_recto_medida')
@register_handler('meson_l_medida')
def handle_medicion(numero, texto):
    step_actual = get_current_step(numero)
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
        advance_steps(numero, next_step)
    except Exception:
        enviar_mensaje(numero, "Por favor ingresa la medida correcta.")
    return True


def handle_text_message(numero: str, texto: str):
    """Procesa un mensaje de texto y avanza los pasos del flujo."""
    now = datetime.now()
    row = get_chat_state(numero)
    step_db = row[0] if row else None
    last_time = row[1] if row else None
    if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
        delete_chat_state(numero)
        step_db = None
    elif row:
        update_chat_state(numero, step_db)

    if texto:
        guardar_mensaje(numero, texto, 'cliente', step=step_db)

    if not step_db:
        set_user_step(numero, Config.INITIAL_STEP)
        process_step_chain(numero, 'iniciar')
        return

    text_norm = normalize_text(texto or "")

    if handle_global_command(numero, texto):
        return

    process_step_chain(numero, text_norm)


def process_buffered_messages(numero):
    with cache_lock:
        textos = message_buffer.pop(numero, None)
        timer = pending_timers.pop(numero, None)
    if timer:
        timer.cancel()
    if not textos:
        return
    combined = " ".join(textos)
    normalized = normalize_text(combined)
    handle_text_message(numero, normalized)

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
                    step = get_current_step(from_number)
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
                        step=step,
                    )
                    update_chat_state(from_number, step, 'sin_respuesta')
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

                    step = get_current_step(from_number)
                    db_id = guardar_mensaje(
                        from_number,
                        "",
                        'audio',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        media_id=media_id,
                        media_url=public_url,
                        mime_type=mime_clean,
                        step=step,
                    )

                    update_chat_state(from_number, step, 'sin_respuesta')

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
                    step = get_current_step(from_number)
                    guardar_mensaje(
                        from_number,
                        "",               # sin texto
                        'video',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        media_id=media_id,
                        media_url=public_url,
                        mime_type=mime_clean,
                        step=step,
                    )

                    update_chat_state(from_number, step, 'sin_respuesta')

                    # 4) Registro interno
                    logging.info("Video recibido: %s", media_id)
                    continue

                # IMAGEN
                if msg_type == 'image':
                    media_id  = msg['image']['id']
                    media_url = get_media_url(media_id)
                    step = get_current_step(from_number)
                    guardar_mensaje(
                        from_number,
                        "",
                        'cliente_image',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        media_id=media_id,
                        media_url=media_url,
                        step=step,
                    )
                    update_chat_state(from_number, step, 'sin_respuesta')
                    logging.info("Imagen recibida: %s", media_id)
                    continue

                # TEXTO / INTERACTIVO
                if 'text' in msg:
                    text = msg['text']['body'].strip()
                    normalized_text = normalize_text(text)
                    step = get_current_step(from_number)
                    guardar_mensaje(
                        from_number,
                        text,
                        'cliente',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        step=step,
                    )
                    update_chat_state(from_number, step, 'sin_respuesta')
                    with cache_lock:
                        message_buffer.setdefault(from_number, []).append(normalized_text)
                        if from_number in pending_timers:
                            pending_timers[from_number].cancel()
                        timer = threading.Timer(3, process_buffered_messages, args=(from_number,))
                        pending_timers[from_number] = timer
                    timer.start()
                    return jsonify({'status': 'buffered'}), 200
                elif 'interactive' in msg:
                    opt = msg['interactive'].get('list_reply') or msg['interactive'].get('button_reply') or {}
                    option_id = opt.get('id') or ''
                    text = (opt.get('title') or '').strip()
                    step = get_current_step(from_number)
                    guardar_mensaje(
                        from_number,
                        text,
                        'cliente',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        step=step,
                    )
                    update_chat_state(from_number, step, 'sin_respuesta')
                    if handle_option_reply(from_number, option_id):
                        continue
                    normalized_text = normalize_text(text)
                    with cache_lock:
                        message_buffer.setdefault(from_number, []).append(normalized_text)
                        if from_number in pending_timers:
                            pending_timers[from_number].cancel()
                        timer = threading.Timer(3, process_buffered_messages, args=(from_number,))
                        pending_timers[from_number] = timer
                    timer.start()
                    return jsonify({'status': 'buffered'}), 200
                else:
                    continue
    return jsonify({'status':'received'}), 200
