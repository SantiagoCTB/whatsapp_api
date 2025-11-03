import os
import logging
import threading
import json
import unicodedata
from flask import Blueprint, Response, jsonify, request, url_for
from datetime import datetime
from config import Config
from services.db import (
    get_connection,
    guardar_mensaje,
    guardar_flow_response,
    get_chat_state,
    update_chat_state,
    delete_chat_state,
)
from services.whatsapp_api import (
    download_audio,
    get_media_url,
    enviar_mensaje,
    start_typing_feedback,
)
from services.job_queue import enqueue_transcription
from services.normalize_text import normalize_text
from services.global_commands import handle_global_command

webhook_bp = Blueprint('webhook', __name__)
logger = logging.getLogger(__name__)

VERIFY_TOKEN    = Config.VERIFY_TOKEN
SESSION_TIMEOUT = Config.SESSION_TIMEOUT
SESSION_TIMEOUT_MESSAGE = Config.SESSION_TIMEOUT_MESSAGE
DEFAULT_FALLBACK_TEXT = "No entendí tu respuesta, intenta de nuevo."

# Mapa numero -> lista de textos recibidos para procesar tras un delay
message_buffer     = {}
pending_timers     = {}
cache_lock         = threading.Lock()

MAX_AUTO_STEPS = 25


def clear_chat_runtime_state(numero: str):
    """Limpia timers y mensajes en memoria asociados a un chat."""

    with cache_lock:
        timer = pending_timers.pop(numero, None)
        entries = message_buffer.pop(numero, None)

    if timer:
        try:
            timer.cancel()
        except Exception:  # pragma: no cover - cancel solo falla en casos extremos
            logger.exception(
                "No se pudo cancelar el temporizador pendiente del chat",
                extra={"numero": numero},
            )

    if entries:
        logger.debug(
            "Se descartaron %d entradas en buffer para el chat finalizado",
            len(entries),
            extra={"numero": numero},
        )

RELEVANT_HEADERS = (
    'X-Hub-Signature-256',
    'User-Agent',
    'Content-Type',
)


def _normalize_step_name(step):
    return (step or '').strip().lower()


def _mask_identifier(value, visible=4):
    if not value:
        return value
    value = str(value)
    if len(value) <= visible:
        return '*' * len(value)
    return f"{value[:visible]}...{value[-2:]}"


def _extract_message_ids(payload):
    ids = []
    for entry in (payload or {}).get('entry', []):
        for change in entry.get('changes', []):
            for msg in change.get('value', {}).get('messages', []) or []:
                msg_id = msg.get('id')
                if msg_id:
                    ids.append(msg_id)
    return ids


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
    current_step = get_current_step(numero)
    if not current_step:
        return False

    def _normalize_option_value(value):
        if not isinstance(value, str):
            return ''
        normalized = unicodedata.normalize('NFKD', value)
        normalized = ''.join(
            ch for ch in normalized if unicodedata.category(ch) != 'Mn'
        )
        return normalized.strip().lower()

    option_norm = _normalize_option_value(option_id)
    if not option_norm:
        return False

    def _fetch_rules(step_filter=None):
        conn = get_connection(); c = conn.cursor()
        try:
            if step_filter is not None:
                c.execute(
                    """
                    SELECT r.step,
                           r.id, r.respuesta, r.siguiente_step, r.tipo,
                           GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                           r.opciones, r.rol_keyword, r.input_text
                     FROM reglas r
                     LEFT JOIN regla_medias m ON r.id = m.regla_id
                     WHERE r.step=%s
                     GROUP BY r.step, r.id
                     ORDER BY r.id
                    """,
                    (step_filter,),
                )
            else:
                c.execute(
                    """
                    SELECT r.step,
                           r.id, r.respuesta, r.siguiente_step, r.tipo,
                           GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                           r.opciones, r.rol_keyword, r.input_text
                      FROM reglas r
                      LEFT JOIN regla_medias m ON r.id = m.regla_id
                     WHERE LOWER(r.input_text)=LOWER(%s)
                     GROUP BY r.step, r.id
                     ORDER BY r.id
                    """,
                    (option_id,),
                )
            rows = c.fetchall()
        finally:
            conn.close()
        return rows

    def _select_rule(rows):
        if not rows:
            return None
        matches = [
            row for row in rows
            if _normalize_option_value((row[8] or '').strip()) == option_norm
        ]
        if not matches:
            return None
        for row in matches:
            if _normalize_option_value((row[0] or '').strip()) == option_norm:
                return row
        return matches[0]

    rule_row = _select_rule(_fetch_rules(current_step))
    if not rule_row:
        rule_row = _select_rule(_fetch_rules())

    if rule_row:
        rule_step = (rule_row[0] or '').strip().lower()
        rule = rule_row[1:]
        effective_step = rule_step or current_step
        set_user_step(numero, effective_step)
        dispatch_rule(numero, rule, step=effective_step)
        return True

    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT opciones FROM reglas WHERE step=%s", (current_step,))
    rows = c.fetchall(); conn.close()
    for (opcs,) in rows:
        nxt = _get_step_from_options(opcs or '', option_id)
        if nxt:
            advance_steps(numero, nxt)
            return True
    return False


def dispatch_rule(numero, regla, step=None, visited=None):
    """Envía la respuesta definida en una regla y asigna roles si aplica."""
    if visited is None:
        visited = set()
    regla_id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, _ = regla
    current_step = step or get_current_step(numero)
    current_step_norm = _normalize_step_name(current_step)
    if current_step_norm:
        visited.add(current_step_norm)
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
    advance_steps(numero, next_step, visited=visited)


def advance_steps(numero: str, steps_str: str, visited=None):
    """Avanza múltiples pasos enviando las reglas comodín correspondientes.

    El procesamiento de la lista de pasos ocurre únicamente en memoria; solo
    se persiste el último paso mediante ``set_user_step``. No se almacena el
    detalle de la lista en la base de datos.
    """
    steps = [_normalize_step_name(s) for s in (steps_str or '').split(',') if s.strip()]
    if not steps:
        return
    if visited is None:
        visited = set()
    for step in steps[:-1]:
        if step in visited:
            logging.warning(
                "Se detectó un ciclo de pasos; se omite la regla comodín",
                extra={"numero": numero, "step": step},
            )
            continue
        if len(visited) >= MAX_AUTO_STEPS:
            logging.warning(
                "Se alcanzó el límite de pasos automáticos encadenados",
                extra={"numero": numero, "step": step},
            )
            return
        visited.add(step)
        conn = get_connection(); c = conn.cursor()
        try:
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
            regla = c.fetchone()
        finally:
            conn.close()
        if regla:
            dispatch_rule(numero, regla, step, visited=visited)
    final_step = steps[-1]
    final_step_norm = _normalize_step_name(final_step)
    if final_step_norm in visited and len(steps) > 1:
        logging.warning(
            "Paso final ya procesado; se evita actualizar el estado para prevenir bucles",
            extra={"numero": numero, "step": final_step},
        )
        return
    set_user_step(numero, final_step)
    if final_step_norm and final_step_norm not in visited:
        process_step_chain(
            numero,
            text_norm=None,
            visited=visited,
        )




def process_step_chain(
    numero,
    text_norm=None,
    visited=None,
    *,
    allow_wildcard_with_text=True,
):
    """Procesa el step actual una sola vez.

    Las reglas con ``input_text='*'`` pueden ejecutarse incluso si no se
    recibió texto del usuario, pero tras la primera ejecución el flujo se
    detiene y espera una nueva entrada.
    """
    if visited is None:
        visited = set()
    step = get_current_step(numero)
    if not step:
        return
    step_norm = _normalize_step_name(step)
    if step_norm:
        visited.add(step_norm)

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
    specific_rules = [r for r in reglas if (r[7] or '').strip() not in ('', '*')]

    wildcard_allowed = (
        text_norm is None or allow_wildcard_with_text or not specific_rules
    )

    # No avanzar si no hay texto del usuario, salvo que existan comodines
    if text_norm is None and not comodines:
        return

    # Coincidencia exacta
    for r in reglas:
        patt = (r[7] or '').strip()
        if patt and patt != '*' and normalize_text(patt) == text_norm:
            dispatch_rule(numero, r, step, visited=visited)
            return

    # Regla comodín
    if comodines and wildcard_allowed:
        dispatch_rule(numero, comodines[0], step, visited=visited)
        # No procesar recursivamente otros comodines; esperar nueva entrada
        return

    if text_norm is None:
        return

    if specific_rules and not wildcard_allowed:
        # Se recibió texto pero no hay coincidencias y se decidió no ejecutar
        # comodines. Esto ocurre, por ejemplo, en el primer mensaje del
        # usuario tras iniciar el flujo, donde se espera que el bot ya haya
        # enviado las instrucciones y aguarde una nueva respuesta válida.
        return

    logging.warning("Fallback en step '%s' para entrada '%s'", step, text_norm)
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


def handle_text_message(numero: str, texto: str, save: bool = True):
    """Procesa un mensaje de texto y avanza los pasos del flujo.

    Parameters
    ----------
    numero: str
        Número del usuario.
    texto: str
        Texto recibido del usuario.
    save: bool, optional
        Si ``True`` se guarda el mensaje en la base de datos. Permite
        reutilizar esta función en flujos donde el texto ya fue
        almacenado para evitar duplicados en el historial.
    """
    now = datetime.now()
    row = get_chat_state(numero)
    step_db = row[0] if row else None
    last_time = row[1] if row else None
    bootstrapped = False
    if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
        delete_chat_state(numero)
        step_db = None
        if SESSION_TIMEOUT_MESSAGE:
            enviar_mensaje(numero, SESSION_TIMEOUT_MESSAGE)
    elif row:
        update_chat_state(numero, step_db)

    if texto and save:
        guardar_mensaje(numero, texto, 'cliente', step=step_db)

    text_norm = normalize_text(texto or "")

    if not step_db:
        bootstrapped = True
        set_user_step(numero, Config.INITIAL_STEP)
        process_step_chain(numero, 'iniciar')
        if not text_norm or text_norm == 'iniciar':
            return

    if handle_global_command(numero, texto):
        return

    process_step_chain(
        numero,
        text_norm,
        allow_wildcard_with_text=not bootstrapped,
    )


def process_buffered_messages(numero):
    with cache_lock:
        entries = message_buffer.pop(numero, None) or []
        timer = pending_timers.pop(numero, None)
    if timer:
        timer.cancel()
    if not entries:
        return

    for entry in entries:
        if isinstance(entry, dict):
            raw_text = entry.get('raw', '')
            normalized_text = entry.get('normalized')
        else:
            raw_text = entry
            normalized_text = None

        normalized_text = normalize_text(
            (normalized_text if normalized_text is not None else raw_text) or ""
        )

        if not normalized_text:
            continue

        handle_text_message(
            numero,
            raw_text if raw_text else normalized_text,
            save=False,
        )

@webhook_bp.route('/webhook', methods=['GET', 'POST'])
def webhook():
    relevant_headers = {
        header: request.headers.get(header)
        for header in RELEVANT_HEADERS
        if request.headers.get(header) is not None
    }
    payload = {}
    masked_message_ids = []
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        masked_message_ids = [_mask_identifier(mid) for mid in _extract_message_ids(payload)]

    logger.info(
        "Webhook request: method=%s headers=%s message_ids=%s",
        request.method,
        relevant_headers,
        masked_message_ids,
    )

    if request.method == 'GET':
        token     = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge', '')

        if token == VERIFY_TOKEN:
            logger.info("Returning verification challenge with status=200")
            return Response(challenge, status=200, mimetype='text/plain')

        logger.info("Verification failed: invalid token received; returning 403")
        return Response('Forbidden', status=403, mimetype='text/plain')

    data = payload
    if not data.get('object'):
        logger.info("Returning status=no_object reason=missing object field")
        return jsonify({'status': 'no_object'}), 400

    summary = {
        'processed': 0,
        'duplicates': 0,
        'unsupported': 0,
    }

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
                    summary['duplicates'] += 1
                    logger.info(
                        "Message skipped as duplicate: message_id=%s",
                        _mask_identifier(wa_id),
                    )
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
                    start_typing_feedback(from_number, wa_id)
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
                    start_typing_feedback(from_number, wa_id)

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
                    summary['processed'] += 1
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
                    start_typing_feedback(from_number, wa_id)

                    # 4) Registro interno
                    logging.info("Video recibido: %s", media_id)
                    summary['processed'] += 1
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
                    start_typing_feedback(from_number, wa_id)
                    logging.info("Imagen recibida: %s", media_id)
                    summary['processed'] += 1
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
                    start_typing_feedback(from_number, wa_id)
                    with cache_lock:
                        message_buffer.setdefault(from_number, []).append(
                            {'raw': text, 'normalized': normalized_text}
                        )
                        if from_number in pending_timers:
                            pending_timers[from_number].cancel()
                        timer = threading.Timer(3, process_buffered_messages, args=(from_number,))
                        pending_timers[from_number] = timer
                    timer.start()
                    summary['processed'] += 1
                    logger.info(
                        "Returning status=buffered reason=text message buffered for aggregation"
                    )
                    return jsonify({'status': 'buffered'}), 200
                elif 'interactive' in msg:
                    interactive = msg['interactive'] or {}
                    interactive_type = interactive.get('type')
                    if interactive_type == 'nfm_reply':
                        nfm_reply = interactive.get('nfm_reply') or {}
                        flow_name = (nfm_reply.get('name') or '').strip()
                        response_payload = None
                        for key in (
                            'response_json',
                            'response',
                            'responses',
                            'response_objects',
                        ):
                            value = nfm_reply.get(key)
                            if value:
                                response_payload = value
                                break
                        if response_payload is None:
                            response_payload = nfm_reply.get('body') or ''
                        if isinstance(response_payload, (dict, list)):
                            response_json = json.dumps(response_payload, ensure_ascii=False)
                        else:
                            response_json = str(response_payload) if response_payload is not None else ''
                        guardar_flow_response(
                            numero=from_number,
                            flow_name=flow_name,
                            response_json=response_json,
                            wa_id=wa_id,
                        )
                        text = (nfm_reply.get('body') or '').strip()
                        if not text:
                            text = response_json or flow_name
                        text = (text or '').strip()
                        step = get_current_step(from_number)
                        if text:
                            guardar_mensaje(
                                from_number,
                                text,
                                'cliente',
                                wa_id=wa_id,
                                reply_to_wa_id=reply_to_id,
                                step=step,
                            )
                            update_chat_state(from_number, step, 'sin_respuesta')
                            start_typing_feedback(from_number, wa_id)
                            normalized_text = normalize_text(text)
                            with cache_lock:
                                message_buffer.setdefault(from_number, []).append(
                                    {'raw': text, 'normalized': normalized_text}
                                )
                                if from_number in pending_timers:
                                    pending_timers[from_number].cancel()
                                timer = threading.Timer(3, process_buffered_messages, args=(from_number,))
                                pending_timers[from_number] = timer
                            timer.start()
                            summary['processed'] += 1
                            logger.info(
                                "Returning status=buffered reason=nfm_reply response buffered for aggregation"
                            )
                            return jsonify({'status': 'buffered'}), 200
                        else:
                            update_chat_state(from_number, step, 'sin_respuesta')
                            start_typing_feedback(from_number, wa_id)
                            summary['processed'] += 1
                            continue
                    opt = interactive.get('list_reply') or interactive.get('button_reply') or {}
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
                    start_typing_feedback(from_number, wa_id)
                    if handle_option_reply(from_number, option_id):
                        continue
                    normalized_text = normalize_text(text)
                    with cache_lock:
                        message_buffer.setdefault(from_number, []).append(
                            {'raw': text, 'normalized': normalized_text}
                        )
                        if from_number in pending_timers:
                            pending_timers[from_number].cancel()
                        timer = threading.Timer(0, process_buffered_messages, args=(from_number,))
                        pending_timers[from_number] = timer
                    timer.start()
                    summary['processed'] += 1
                    logger.info(
                        "Returning status=buffered reason=interactive response buffered for aggregation"
                    )
                    return jsonify({'status': 'buffered'}), 200
                else:
                    summary['unsupported'] += 1
                    continue
    logger.info(
        "Returning status=received reason=processed payload summary=%s",
        summary,
    )
    return jsonify({'status':'received'}), 200
