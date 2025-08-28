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

pending_texts = {}


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
        trigger_auto_steps(numero)
    except Exception:
        enviar_mensaje(numero, "Por favor ingresa la medida correcta.")
    return True


def handle_text_message(numero: str, texto: str):
    """
    Orquestador principal de mensajes de texto:
    - Respeta reglas con '*' como 'pendiente' (en-hilo): NO auto-encadena; consume en el SIGUIENTE mensaje.
    - Evita doble envío (respuesta del salto + prompt del step '*') usando _mark_sent/_did_send.
    - Mantiene coincidencias normales (exactas/fuzzy) cuando no hay '*' en-hilo.
    """
    # ----------------- PREPARACIÓN Y SESIÓN -----------------
    try:
        now = datetime.now()
        last_time = user_last_activity.get(numero)
        session_reset = False

        # Expiración de sesión (si ya lo usas)
        if last_time and (now - last_time).total_seconds() > SESSION_TIMEOUT:
            user_steps.pop(numero, None)
            delete_chat_state(numero)
            session_reset = True

        user_last_activity[numero] = now

        # Normaliza el texto del usuario
        raw_text = texto or ""
        text_norm = normalize_text(raw_text)

        # Obtén el step actual
        current_step = (user_steps.get(numero) or "").strip().lower()
        if not current_step:
            # Si no tienes step, podrías iniciar alguno por defecto o devolver un mensaje
            # Aquí simplemente no hacemos nada especial: cae al fallback de tu flujo de arranque
            pass

        # ----------------- CARGA REGLAS DEL STEP -----------------
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            """
            SELECT
                r.id,
                r.respuesta,
                r.siguiente_step,
                r.tipo,
                GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                r.opciones,
                r.role_keyword,
                r.input_text
            FROM reglas r
            LEFT JOIN regla_medias m ON r.id = m.regla_id
            WHERE r.step = %s
            GROUP BY r.id
            """,
            (current_step,)
        )
        reglas = c.fetchall()
        conn.close()

        # ----------------- A) CONSUMIR EN-HILO '*' SI EXISTE -----------------
        # Si hay una regla pendiente en-hilo, se evalúa PRIMERO.
        regla_hilo_id = pending_rules.get(numero)
        if regla_hilo_id is not None:
            r_activa = next((r for r in reglas if str(r[0]) == str(regla_hilo_id)), None)
            if r_activa:
                _id, resp, next_step, tipo_resp, media_urls, opts, rol_kw, input_db = r_activa
                if (input_db or "").strip() == "*":
                    # (Opcional) asignar rol si la regla lo indica
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

                    # Avanzar al siguiente paso y limpiar la pendiente
                    set_user_step(numero, (next_step or "").strip().lower())
                    pending_rules.pop(numero, None)

                    # Importante: NO enviar aquí el 'resp' del step actual (evita duplicados).
                    # Al entrar al nuevo step, si es '*' único, trigger_auto_steps lo marcará pendiente
                    # y SOLO mostrará su prompt si aún no se ha enviado nada en este turno.
                    trigger_auto_steps(numero)
                    return
            # Si la regla pendiente no existe o no coincide, continúa al matching normal.

        # ----------------- B) MATCH EXACTO (cuando NO hay en-hilo) -----------------
        # Busca coincidencias exactas por input_text (ignorando mayúsculas/acentos con normalize_text)
        # Nota: si usas botones/listas con 'id', usualmente guardas ese 'id' en input_text.
        for (rid, resp, next_step, tipo_resp, media_urls, opts, rol_kw, input_db) in reglas:
            patt = (input_db or "").strip()
            if patt and patt != "*":
                if normalize_text(patt) == text_norm:
                    # Coincidió exacto -> responder y avanzar
                    # (Opcional) Rol
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

                    # Enviar respuesta de la regla
                    media_list = media_urls.split("||") if media_urls else []
                    if tipo_resp in ["image", "video", "audio", "document"] and media_list:
                        _mark_sent(numero); enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
                        for extra in media_list[1:]:
                            _mark_sent(numero); enviar_mensaje(numero, "", tipo_respuesta=tipo_resp, opciones=extra)
                    else:
                        _mark_sent(numero); enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)

                    # Avanza de step
                    set_user_step(numero, (next_step or "").strip().lower())

                    # Si el nuevo step es '*' único, quedará en-hilo y SOLO mostrará su prompt
                    # si no hemos enviado nada más en este turno (evita doble mensaje).
                    trigger_auto_steps(numero)
                    return

        # ----------------- C) MATCH 'CATCH-ALL' '*' EN EL MISMO STEP -----------------
        # Si en este step hay una regla con '*' junto a otras (no-única), úsala como "cualquiera".
        # Pero OJO: no confundir con la lógica en-hilo única.
        comodines = [r for r in reglas if (r[7] or "").strip() == "*"]
        if comodines:
            # Si NO estamos en situación de en-hilo (ya revisado arriba),
            # y hay comodín en el mismo step, úsalo como fallback del step.
            # Regla de negocio: si el step tiene SOLO 1 regla y es '*',
            # ese caso lo maneja trigger_auto_steps (en-hilo). Aquí aplicamos cuando no es único.
            if len(reglas) > 1:
                rid, resp, next_step, tipo_resp, media_urls, opts, rol_kw, input_db = comodines[0]

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

                media_list = media_urls.split("||") if media_urls else []
                if tipo_resp in ["image", "video", "audio", "document"] and media_list:
                    _mark_sent(numero); enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
                    for extra in media_list[1:]:
                        _mark_sent(numero); enviar_mensaje(numero, "", tipo_respuesta=tipo_resp, opciones=extra)
                else:
                    _mark_sent(numero); enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)

                set_user_step(numero, (next_step or "").strip().lower())
                trigger_auto_steps(numero)
                return

        # ----------------- D) FALLBACK (NO MATCH EN ESTE STEP) -----------------
        # Si llegamos aquí: no había en-hilo, no hubo match exacto, ni catch-all aplicable.
        _mark_sent(numero)
        enviar_mensaje(numero, "No entendí tu respuesta, intenta de nuevo.")
        return

    finally:
        # Limpia la bandera para el próximo turno de este usuario
        pending_texts.pop(numero, None)


def set_en_hilo(numero, regla_id):
    """Registra que el número tiene una regla en-hilo pendiente."""
    pending_rules[numero] = regla_id


def process_en_hilo_rule(numero, regla_id):
    """Procesa inmediatamente una regla en-hilo enviando su respuesta y avanzando de step."""
    conn = get_connection(); c = conn.cursor()
    c.execute(
        """
        SELECT r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.id=%s
         GROUP BY r.id
        """,
        (regla_id,),
    )
    row = c.fetchone(); conn.close()
    if not row:
        return
    resp, next_step, tipo_resp, media_urls, opts, rol_kw = row
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
    pending_rules.pop(numero, None)
    if next_step:
        trigger_auto_steps(numero)

# Evita doble envío en el mismo turno de usuario
def _mark_sent(numero):
    pending_texts[numero] = True

def _did_send(numero):
    return pending_texts.get(numero) is True


def trigger_auto_steps(numero):
    """Si el step actual tiene exactamente UNA regla con '*', la marca como pendiente (en-hilo).
    Solo envía su prompt si en ESTE MISMO TURNO aún no se ha enviado nada.
    Así evitamos el doble mensaje (respuesta del salto + prompt del step '*')."""

    step = user_steps.get(numero, '').strip().lower()
    if not step:
        return

    # Si ya hay una regla pendiente, no hagas nada
    if numero in pending_rules:
        return

    conn = get_connection(); c = conn.cursor()
    c.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN TRIM(input_text)='*' THEN 1 ELSE 0 END) AS comodines
          FROM reglas
         WHERE step=%s
        """,
        (step,)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return

    total, comodines = row
    comodines = comodines or 0

    # Caso: único comodín en el step
    if total == 1 and comodines == 1:
        c.execute(
            """
            SELECT r.id, r.respuesta, r.tipo,
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   r.opciones
              FROM reglas r
              LEFT JOIN regla_medias m ON r.id = m.regla_id
             WHERE r.step=%s AND TRIM(r.input_text)='*'
             GROUP BY r.id
            """,
            (step,)
        )
        r = c.fetchone()
        conn.close()

        if not r:
            return

        regla_id, resp, tipo_resp, media_urls, opts = r

        # 1) marcar en-hilo
        set_en_hilo(numero, regla_id)

        # 2) SOLO si aún no hemos enviado nada en este turno, mostramos su prompt
        if not _did_send(numero):
            media_list = media_urls.split('||') if media_urls else []
            if tipo_resp in ['image', 'video', 'audio', 'document'] and media_list:
                _mark_sent(numero); enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=media_list[0])
                for extra in media_list[1:]:
                    _mark_sent(numero); enviar_mensaje(numero, '', tipo_respuesta=tipo_resp, opciones=extra)
            else:
                _mark_sent(numero); enviar_mensaje(numero, resp, tipo_respuesta=tipo_resp, opciones=opts)
        return

    conn.close()




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
