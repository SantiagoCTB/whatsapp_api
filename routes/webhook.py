import os
import logging
import threading
import json
import unicodedata
from datetime import datetime
from flask import (
    Blueprint,
    Response,
    jsonify,
    request,
    url_for,
    has_request_context,
)

from config import Config
from services import tenants
from services.db import (
    get_connection,
    guardar_mensaje,
    guardar_estado_mensaje,
    guardar_flow_response,
    get_chat_state,
    obtener_historial_chat,
    obtener_ultimo_mensaje_cliente,
    obtener_ultimo_mensaje_cliente_info,
    update_chat_state,
    delete_chat_state,
)
from services.whatsapp_api import (
    download_audio,
    get_media_url,
    enviar_mensaje,
    start_typing_feedback,
    stop_typing_feedback,
)
from services.job_queue import enqueue_transcription
from services.normalize_text import normalize_text
from services.global_commands import handle_global_command
from services.ia_client import generate_response
from services.catalog import find_relevant_pages

webhook_bp = Blueprint('webhook', __name__)
logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_TEXT = "No entendí tu respuesta, intenta de nuevo."
default_env = tenants.get_tenant_env(None)

# Mapa numero -> lista de textos recibidos para procesar tras un delay
message_buffer     = {}
pending_timers     = {}
cache_lock         = threading.Lock()

MAX_AUTO_STEPS = 25


def _resolve_rule_platform(numero: str) -> str:
    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    last_tipo = (last_client_info or {}).get("tipo") or ""
    last_tipo_lower = str(last_tipo).lower()
    if "messenger" in last_tipo_lower:
        return "messenger"
    if "instagram" in last_tipo_lower:
        return "instagram"
    return "whatsapp"


def _media_root():
    return tenants.get_media_root()


def _build_public_url(path: str) -> str | None:
    clean_path = path.lstrip("/")
    if has_request_context():
        base_url = request.url_root
    else:
        base_url = tenants.get_runtime_setting("PUBLIC_BASE_URL", default=Config.PUBLIC_BASE_URL)

    if not base_url:
        logger.warning("PUBLIC_BASE_URL no está configurado; no se puede construir URL pública.")
        return None

    return f"{base_url.rstrip('/')}/{clean_path}"


def _preferred_url_scheme() -> str:
    scheme = tenants.get_runtime_setting(
        "PREFERRED_URL_SCHEME", default=Config.PREFERRED_URL_SCHEME
    )
    if scheme:
        return str(scheme).strip()
    return "https"


def _normalize_media_url(url: str | None) -> str | None:
    if not url:
        return url
    normalized = str(url).strip()
    if normalized.lower().startswith("http://"):
        return f"https://{normalized[len('http://'):]}"
    return normalized


def _get_verify_token():
    return tenants.get_runtime_setting("VERIFY_TOKEN", default=Config.VERIFY_TOKEN)


def _get_session_timeout():
    return tenants.get_runtime_setting(
        "SESSION_TIMEOUT", default=Config.SESSION_TIMEOUT, cast=int
    )


def _get_session_timeout_message():
    return tenants.get_runtime_setting(
        "SESSION_TIMEOUT_MESSAGE", default=Config.SESSION_TIMEOUT_MESSAGE
    )


def _get_messenger_disclosure_message():
    return tenants.get_runtime_setting(
        "MESSENGER_AUTOMATION_DISCLOSURE_MESSAGE",
        default=Config.MESSENGER_AUTOMATION_DISCLOSURE_MESSAGE,
    )


def _get_messenger_disclosure_reset_seconds() -> int:
    return tenants.get_runtime_setting(
        "MESSENGER_DISCLOSURE_RESET_SECONDS",
        default=Config.MESSENGER_DISCLOSURE_RESET_SECONDS,
        cast=int,
    )


# Valores por defecto expuestos para compatibilidad con pruebas/llamadas externas.
SESSION_TIMEOUT = default_env.get("SESSION_TIMEOUT") or Config.SESSION_TIMEOUT
SESSION_TIMEOUT_MESSAGE = (
    default_env.get("SESSION_TIMEOUT_MESSAGE") or Config.SESSION_TIMEOUT_MESSAGE
)
VERIFY_TOKEN = default_env.get("VERIFY_TOKEN") or Config.VERIFY_TOKEN


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

    stop_typing_feedback(numero)


def notify_session_closed(numero: str, *, origin: str = "timeout") -> bool:
    """Envía al usuario el mensaje configurado al cerrar la sesión del chat."""

    message = (_get_session_timeout_message() or "").strip()
    if not numero or not message:
        return False

    log_extra = {"numero": numero, "origin": origin}
    try:
        sent = enviar_mensaje(numero, message, tipo="bot")
    except Exception:  # pragma: no cover - enviar_mensaje ya maneja errores comunes
        logger.exception(
            "Error inesperado al enviar mensaje de cierre de sesión",
            extra=log_extra,
        )
        return False

    if not sent:
        logger.warning(
            "No se pudo enviar el mensaje de cierre de sesión",
            extra=log_extra,
        )
        return False

    logger.info(
        "Mensaje de cierre de sesión enviado",
        extra=log_extra,
    )
    return True

RELEVANT_HEADERS = (
    'X-Hub-Signature-256',
    'User-Agent',
    'Content-Type',
)


def _normalize_step_name(step):
    return (step or '').strip().lower()


def _is_ia_step(step: str | None) -> bool:
    return _normalize_step_name(step) == 'ia'


def _ia_history_limit() -> int:
    return tenants.get_runtime_setting(
        "IA_HISTORY_LIMIT", default=Config.IA_HISTORY_LIMIT, cast=int
    )


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
        for event in entry.get("messaging", []) or []:
            message = event.get("message") or {}
            message_id = message.get("mid")
            if message_id:
                ids.append(message_id)
            delivery = event.get("delivery") or {}
            for mid in delivery.get("mids") or []:
                ids.append(mid)
            read_event = event.get("read") or {}
            for mid in read_event.get("mids") or []:
                ids.append(mid)
            reaction_event = event.get("reaction") or {}
            reaction_mid = reaction_event.get("mid")
            if reaction_mid:
                ids.append(reaction_mid)
            postback = event.get("postback") or {}
            postback_mid = postback.get("mid")
            if postback_mid:
                ids.append(postback_mid)
        for change in entry.get('changes', []):
            for msg in change.get('value', {}).get('messages', []) or []:
                msg_id = msg.get('id')
                if msg_id:
                    ids.append(msg_id)
            value = change.get("value") or {}
            message_value = value.get("message")
            if isinstance(message_value, dict):
                msg_id = message_value.get("mid")
                if msg_id:
                    ids.append(msg_id)
            for status in change.get('value', {}).get('statuses', []) or []:
                status_id = status.get('id')
                if status_id:
                    ids.append(status_id)
    return ids


def _coerce_status_timestamp(raw_value):
    if raw_value in (None, ""):
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _coerce_messenger_timestamp(raw_value):
    timestamp = _coerce_status_timestamp(raw_value)
    if timestamp is None:
        return None
    if timestamp > 10_000_000_000:
        return int(timestamp / 1000)
    return timestamp


def _should_send_messenger_disclosure(chat_state_row) -> bool:
    message = _get_messenger_disclosure_message()
    if not message:
        return False

    reset_seconds = _get_messenger_disclosure_reset_seconds()
    if reset_seconds is None or reset_seconds <= 0:
        return False

    if not chat_state_row:
        return True

    last_activity = chat_state_row[1] if len(chat_state_row) > 1 else None
    if not isinstance(last_activity, datetime):
        return True

    elapsed_seconds = (datetime.utcnow() - last_activity).total_seconds()
    return elapsed_seconds >= reset_seconds


def _maybe_send_messenger_disclosure(numero: str, chat_state_row, step: str | None):
    if not _should_send_messenger_disclosure(chat_state_row):
        return
    message = _get_messenger_disclosure_message()
    if not message:
        return
    sent = enviar_mensaje(numero, message, tipo="bot", step=step)
    if not sent:
        logger.warning(
            "No se pudo enviar el mensaje de divulgación automatizada",
            extra={"numero": numero},
        )


def _normalize_status_error(errors):
    if not errors:
        return {}
    if isinstance(errors, list):
        error = errors[0] if errors else {}
    elif isinstance(errors, dict):
        error = errors
    else:
        return {}

    return {
        "code": error.get("code"),
        "title": error.get("title"),
        "message": error.get("message"),
        "details": error.get("details") or error.get("error_data"),
    }


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


def _extract_chat_status(row):
    if not row or len(row) < 3:
        return None
    return (row[2] or '').strip().lower() or None


def _is_agent_mode(row) -> bool:
    return _extract_chat_status(row) == 'asesor'


def _catalog_context_for_prompt(prompt: str):
    """Obtiene contenido relevante del portafolio y prepara un contexto robusto."""

    pages = find_relevant_pages(prompt, limit=3)
    if not pages:
        return "", []

    context_lines: list[str] = []

    for page in pages:
        snippet = (page.text_content or "").strip()
        if len(snippet) > 800:
            snippet = f"{snippet[:780]}..."

        image_rel = ""
        if page.image_filename:
            image_rel = tenants.get_uploads_url_path(f"ia_pages/{page.image_filename}")

        context_lines.append(
            "- registro: {registro}\n  texto: {texto}\n  imagen_rel: {rel}".format(
                registro=page.page_number,
                texto=snippet,
                rel=f"/static/{image_rel}" if image_rel else "",
            )
        )

    return "\n".join(context_lines), pages


def _reply_with_ai(
    numero: str,
    user_text: str | None,
    *,
    system_prompt: str | None = None,
    set_step: bool = True,
    history_step: str | None = "ia",
    message_step: str | None = None,
) -> bool:
    """Envía el mensaje al modelo de IA y responde al usuario."""

    prompt = (user_text or "").strip() or obtener_ultimo_mensaje_cliente(numero)
    if not prompt:
        logger.info("Sin texto para enviar a la IA", extra={"numero": numero})
        return False

    if set_step:
        set_user_step(numero, "ia")
        update_chat_state(numero, "ia", "ia_activa")

    if history_step:
        history = obtener_historial_chat(numero, limit=_ia_history_limit(), step=history_step)
    else:
        history = obtener_historial_chat(numero, limit=_ia_history_limit())

    if not message_step:
        message_step = "ia" if set_step else get_current_step(numero)
    catalog_context, pages = _catalog_context_for_prompt(prompt)
    if not catalog_context:
        logger.warning(
            "Sin contexto de portafolio para la IA; se solicitará más información",
            extra={"numero": numero},
        )
        pedir_datos = (
            "Ahora mismo no encuentro información suficiente para tu consulta. "
            "¿Puedes darme más detalles del producto, marca o categoría que buscas?"
        )
        enviar_mensaje(numero, pedir_datos, tipo="bot", step=message_step)
        return False

    prompt_for_model = prompt
    if catalog_context:
        prompt_for_model = (
            f"{prompt}\n\n"
            "Contexto del portafolio (usa solo esta información):\n"
            f"{catalog_context}\n\n"
            "Instrucciones para la respuesta:\n"
            "- Responde únicamente con datos disponibles en este contexto.\n"
            "- Evita mencionar el origen del contenido o detalles internos.\n"
            "- No incluyas enlaces ni imágenes en formato markdown/HTML.\n"
            "- Si no hay coincidencias claras, pide más detalles al usuario sin inventar datos."
        )

    response = generate_response(history, prompt_for_model, system_message=system_prompt)
    if not response:
        logger.warning("La IA no devolvió respuesta", extra={"numero": numero})
        return False

    enviar_mensaje(numero, response, tipo="bot", step=message_step)
    for page in pages:
        if not page.image_filename:
            continue

        image_path = os.path.join(_media_root(), 'ia_pages', page.image_filename)
        if not os.path.exists(image_path):
            continue

        image_path = tenants.get_uploads_url_path(f"ia_pages/{page.image_filename}")
        if has_request_context():
            image_url = url_for(
                'static',
                filename=image_path,
                _external=True,
                _scheme=_preferred_url_scheme(),
            )
        else:
            image_url = _build_public_url(f"static/{image_path}")
        if not image_url:
            continue
        caption = "Vista del producto"

        enviar_mensaje(
            numero,
            caption,
            tipo="bot",
            tipo_respuesta="image",
            opciones=image_url,
            step=message_step,
        )
    return True


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
                    if nxt is None or str(nxt).strip() == '':
                        return (option_id or '').strip().lower() or None
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
                if nxt is None or str(nxt).strip() == '':
                    return (option_id or '').strip().lower() or None
                return (nxt or '').strip().lower() or None
    return None


def _mark_message_processed(message_id: str | None) -> bool:
    if not message_id:
        return False
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM mensajes_procesados WHERE mensaje_id = %s", (message_id,))
    if c.fetchone():
        conn.close()
        return True
    c.execute("INSERT INTO mensajes_procesados (mensaje_id) VALUES (%s)", (message_id,))
    conn.commit()
    conn.close()
    return False


def _handle_messenger_payload(data, summary, channel="messenger"):
    tipo_prefix = "cliente_instagram" if channel == "instagram" else "cliente_messenger"
    for entry in data.get("entry", []):
        events = list(entry.get("messaging", []) or [])
        for change in entry.get("changes", []) or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            messages = value.get("messages")
            if isinstance(messages, list) and messages:
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    events.append(
                        {
                            "sender": value.get("sender"),
                            "recipient": value.get("recipient"),
                            "timestamp": value.get("timestamp"),
                            "message": message,
                        }
                    )
            else:
                events.append(value)
        for event in events:
            handled = False
            message = event.get("message") or {}
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                summary["unsupported"] += 1
                continue

            delivery = event.get("delivery") or {}
            if delivery:
                mids = delivery.get("mids") or []
                timestamp = _coerce_messenger_timestamp(delivery.get("watermark"))
                recipient_id = (event.get("recipient") or {}).get("id")
                for mid in mids:
                    guardar_estado_mensaje(
                        mid,
                        "delivered",
                        status_timestamp=timestamp,
                        recipient_id=recipient_id,
                        payload=delivery,
                    )
                    summary["statuses"] += 1
                handled = True

            read_event = event.get("read") or {}
            if read_event:
                mids = read_event.get("mids") or []
                if read_event.get("mid"):
                    mids.append(read_event.get("mid"))
                timestamp = _coerce_messenger_timestamp(read_event.get("watermark"))
                recipient_id = (event.get("recipient") or {}).get("id")
                for mid in mids:
                    guardar_estado_mensaje(
                        mid,
                        "read",
                        status_timestamp=timestamp,
                        recipient_id=recipient_id,
                        payload=read_event,
                    )
                    summary["statuses"] += 1
                handled = True

            reaction_event = event.get("reaction") or {}
            if reaction_event:
                reaction_mid = reaction_event.get("mid")
                if reaction_mid:
                    recipient_id = (event.get("recipient") or {}).get("id")
                    guardar_estado_mensaje(
                        reaction_mid,
                        "reaction",
                        status_timestamp=_coerce_messenger_timestamp(event.get("timestamp")),
                        recipient_id=recipient_id,
                        payload=reaction_event,
                    )
                    summary["statuses"] += 1
                handled = True

            if message.get("is_echo"):
                message_id = message.get("mid")
                if message_id:
                    guardar_estado_mensaje(
                        message_id,
                        "sent",
                        status_timestamp=_coerce_messenger_timestamp(event.get("timestamp")),
                        recipient_id=sender_id,
                        payload=message,
                    )
                    summary["statuses"] += 1
                handled = True
                continue

            message_id = message.get("mid")
            if _mark_message_processed(message_id):
                summary["duplicates"] += 1
                continue

            chat_state_row = get_chat_state(sender_id)
            agent_mode = _is_agent_mode(chat_state_row)
            estado_update = None if agent_mode else "sin_respuesta"
            if agent_mode:
                logger.info(
                    "Chat en modo asesor; se omite flujo automático",
                    extra={"numero": sender_id, "message_id": _mask_identifier(message_id)},
                )

            quick_reply_payload = (message.get("quick_reply") or {}).get("payload")
            text = (message.get("text") or "").strip()
            if text or quick_reply_payload:
                stored_text = text or (quick_reply_payload or "")
                normalized_text = normalize_text(quick_reply_payload or text)
                step = get_current_step(sender_id)
                guardar_mensaje(
                    sender_id,
                    stored_text,
                    tipo_prefix,
                    wa_id=message_id,
                    step=step,
                )
                if channel == "messenger" and not agent_mode:
                    _maybe_send_messenger_disclosure(sender_id, chat_state_row, step)
                update_chat_state(sender_id, step, estado_update)
                if agent_mode:
                    summary["processed"] += 1
                    handled = True
                elif quick_reply_payload and handle_option_reply(
                    sender_id,
                    quick_reply_payload,
                    platform=channel,
                ):
                    summary["processed"] += 1
                    handled = True
                elif normalized_text:
                    current_tenant = tenants.get_current_tenant()
                    tenant_env = dict(tenants.get_current_tenant_env() or {})
                    with cache_lock:
                        message_buffer.setdefault(sender_id, []).append(
                            {
                                "raw": quick_reply_payload or text,
                                "normalized": normalized_text,
                                "tenant_key": current_tenant.tenant_key if current_tenant else None,
                                "tenant_env": tenant_env,
                            }
                        )
                        if sender_id in pending_timers:
                            pending_timers[sender_id].cancel()
                        timer = threading.Timer(3, process_buffered_messages, args=(sender_id,))
                        pending_timers[sender_id] = timer
                    timer.start()
                    summary["processed"] += 1
                    handled = True
                    continue
                else:
                    summary["processed"] += 1
                    handled = True

            attachments = message.get("attachments") or []
            if attachments:
                step = get_current_step(sender_id)
                for attachment in attachments:
                    attach_type = (attachment.get("type") or "").lower()
                    payload = attachment.get("payload") or {}
                    media_url = payload.get("url")
                    tipo_db = (
                        f"{tipo_prefix}_{attach_type}" if attach_type else tipo_prefix
                    )
                    guardar_mensaje(
                        sender_id,
                        "",
                        tipo_db,
                        wa_id=message_id,
                        media_url=media_url,
                        step=step,
                    )
                if channel == "messenger" and not agent_mode:
                    _maybe_send_messenger_disclosure(sender_id, chat_state_row, step)
                update_chat_state(sender_id, step, estado_update)
                if agent_mode:
                    summary["processed"] += 1
                    handled = True
                else:
                    handle_text_message(sender_id, "", save=False)
                    summary["processed"] += 1
                    handled = True

            postback = event.get("postback") or {}
            if postback and not message:
                postback_id = postback.get("mid")
                if postback_id and _mark_message_processed(postback_id):
                    summary["duplicates"] += 1
                    continue
                payload_text = (postback.get("payload") or "").strip()
                title_text = (postback.get("title") or "").strip()
                effective_text = payload_text or title_text
                step = get_current_step(sender_id)
                guardar_mensaje(
                    sender_id,
                    effective_text,
                    f"{tipo_prefix}_postback",
                    wa_id=postback_id,
                    step=step,
                )
                if channel == "messenger" and not agent_mode:
                    _maybe_send_messenger_disclosure(sender_id, chat_state_row, step)
                update_chat_state(sender_id, step, estado_update)
                if agent_mode:
                    summary["processed"] += 1
                    handled = True
                elif effective_text and handle_option_reply(
                    sender_id,
                    effective_text,
                    platform=channel,
                ):
                    summary["processed"] += 1
                    handled = True
                elif effective_text:
                    current_tenant = tenants.get_current_tenant()
                    tenant_env = dict(tenants.get_current_tenant_env() or {})
                    normalized_payload = normalize_text(effective_text)
                    with cache_lock:
                        message_buffer.setdefault(sender_id, []).append(
                            {
                                "raw": effective_text,
                                "normalized": normalized_payload,
                                "tenant_key": current_tenant.tenant_key if current_tenant else None,
                                "tenant_env": tenant_env,
                            }
                        )
                        if sender_id in pending_timers:
                            pending_timers[sender_id].cancel()
                        timer = threading.Timer(3, process_buffered_messages, args=(sender_id,))
                        pending_timers[sender_id] = timer
                    timer.start()
                    summary["processed"] += 1
                    handled = True
                else:
                    summary["processed"] += 1
                    handled = True

            if not handled:
                summary["unsupported"] += 1


def _canonicalize_step_name(value: str) -> str:
    value = _normalize_step_name(value)
    return ''.join(ch for ch in value if ch.isalnum())


def _match_selected_step(next_step: str, option_id: str, opciones: str):
    """Devuelve el paso asociado a una opción seleccionada.

    Solo devuelve un valor cuando la opción coincide con alguno de los pasos
    listados en ``next_step`` o con un mapeo explícito dentro de ``opciones``.
    Si no hay coincidencia, retorna ``None`` para evitar avanzar erróneamente.
    """

    if not option_id or not next_step:
        return None

    selected_norm = _normalize_step_name(option_id)
    if not selected_norm:
        return None

    candidate_steps = [
        _normalize_step_name(step)
        for step in (next_step or '').split(',')
        if step and step.strip()
    ]

    if selected_norm in candidate_steps:
        return selected_norm

    mapped = _get_step_from_options(opciones, option_id)
    if mapped:
        return _normalize_step_name(mapped)

    return None


def handle_option_reply(numero, option_id, platform: str | None = None):
    if not option_id:
        return False
    current_step = get_current_step(numero)
    if not current_step:
        return False
    if not platform:
        platform = _resolve_rule_platform(numero)

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
                       AND (r.platform IS NULL OR r.platform = '' OR r.platform = %s)
                     GROUP BY r.step, r.id
                     ORDER BY r.id
                    """,
                    (step_filter, platform),
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
                       AND (r.platform IS NULL OR r.platform = '' OR r.platform = %s)
                     GROUP BY r.step, r.id
                     ORDER BY r.id
                    """,
                    (option_id, platform),
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

    current_step_rules = _fetch_rules(current_step)
    rule_row = _select_rule(current_step_rules)
    if not rule_row:
        rule_row = _select_rule(_fetch_rules())

    if rule_row:
        rule_step = (rule_row[0] or '').strip().lower()
        rule = rule_row[1:]
        effective_step = rule_step or current_step
        set_user_step(numero, effective_step)
        dispatch_rule(
            numero,
            rule,
            step=effective_step,
            selected_option_id=option_id,
            platform=platform,
        )
        return True

    for row in current_step_rules:
        matched_step = _match_selected_step(row[3] or '', option_id, row[6] or '')
        if matched_step:
            advance_steps(numero, matched_step, platform=platform)
            return True

    conn = get_connection(); c = conn.cursor()
    c.execute(
        """
        SELECT opciones
          FROM reglas
         WHERE step=%s
           AND (platform IS NULL OR platform = '' OR platform = %s)
        """,
        (current_step, platform),
    )
    rows = c.fetchall(); conn.close()
    for (opcs,) in rows:
        nxt = _get_step_from_options(opcs or '', option_id)
        if nxt:
            advance_steps(numero, nxt, platform=platform)
            return True
    return False


def _resolve_next_step(next_step: str, selected_option_id: str, opciones: str):
    """Devuelve el siguiente paso teniendo en cuenta una selección interactiva."""

    if not selected_option_id:
        return next_step

    selected_norm = _normalize_step_name(selected_option_id)
    if not selected_norm:
        return next_step

    candidate_steps = [
        _normalize_step_name(step)
        for step in (next_step or '').split(',')
        if step and step.strip()
    ]
    if selected_norm in candidate_steps:
        return selected_norm

    mapped_step = _get_step_from_options(opciones, selected_option_id)
    if mapped_step:
        return mapped_step

    return next_step


def dispatch_rule(
    numero,
    regla,
    step=None,
    visited=None,
    selected_option_id=None,
    platform: str | None = None,
):
    """Envía la respuesta definida en una regla y asigna roles si aplica."""
    if visited is None:
        visited = set()
    if not platform:
        platform = _resolve_rule_platform(numero)
    (
        regla_id,
        resp,
        next_step_raw,
        tipo_resp,
        media_urls,
        opts,
        rol_kw,
        input_text,
    ) = regla
    current_step = step or get_current_step(numero)
    current_step_norm = _normalize_step_name(current_step)
    if current_step_norm:
        visited.add(current_step_norm)

    rule_input_norm = normalize_text(input_text or "") if input_text else ""
    if rule_input_norm == "ia":
        _reply_with_ai(
            numero,
            obtener_ultimo_mensaje_cliente(numero),
            system_prompt=resp,
            set_step=False,
            history_step=None,
            message_step=current_step,
        )
        next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
        if next_step:
            advance_steps(numero, next_step, visited=visited, platform=platform)
        return

    media_list = media_urls.split('||') if media_urls else []
    if tipo_resp in {'texto', 'lista', 'boton'} and not (resp or '').strip() and not media_list:
        next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
        advance_steps(numero, next_step, visited=visited, platform=platform)
        return
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
            # Mantener únicamente el rol definido por la regla para evitar
            # asignaciones accidentales múltiples por coincidencia de palabras.
            c.execute(
                "DELETE FROM chat_roles WHERE numero = %s AND role_id != %s",
                (numero, role[0]),
            )
            c.execute(
                "INSERT IGNORE INTO chat_roles (numero, role_id) VALUES (%s, %s)",
                (numero, role[0])
            )
            conn.commit()
        conn.close()
    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)


def advance_steps(numero: str, steps_str: str, visited=None, platform: str | None = None):
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
    if not platform:
        platform = _resolve_rule_platform(numero)
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
                 WHERE r.step=%s
                   AND r.input_text='*'
                   AND (r.platform IS NULL OR r.platform = '' OR r.platform = %s)
                 GROUP BY r.id
                 ORDER BY r.id
                 LIMIT 1
                """,
                (step, platform),
            )
            regla = c.fetchone()
        finally:
            conn.close()
        if regla:
            dispatch_rule(numero, regla, step, visited=visited, platform=platform)
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
            platform=platform,
        )




def process_step_chain(
    numero,
    text_norm=None,
    visited=None,
    platform: str | None = None,
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
    if not platform:
        platform = _resolve_rule_platform(numero)
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
           AND (r.platform IS NULL OR r.platform = '' OR r.platform = %s)
         GROUP BY r.id
         ORDER BY r.id
        """,
        (step, platform),
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
    platform = _resolve_rule_platform(numero)
    conn = get_connection(); c = conn.cursor()
    c.execute(
        """
        SELECT r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword, r.calculo, r.handler
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s
           AND r.input_text='*'
           AND (r.platform IS NULL OR r.platform = '' OR r.platform = %s)
         GROUP BY r.id
        """,
        (step_actual, platform)
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
        advance_steps(numero, next_step, platform=platform)
    except Exception:
        enviar_mensaje(numero, "Por favor ingresa la medida correcta.")
    return True


def handle_text_message(
    numero: str,
    texto: str,
    save: bool = True,
    platform: str | None = None,
):
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
    # Se usa UTC para evitar desfaces de zona horaria entre la app y la base
    # de datos que puedan disparar expiraciones falsas.
    now = datetime.utcnow()
    row = get_chat_state(numero)
    step_db = row[0] if row else None
    last_time = row[1] if row else None
    bootstrapped = False
    timeout_seconds = _get_session_timeout()
    expired_session = False
    has_active_session = bool(step_db)
    if (
        has_active_session
        and isinstance(last_time, datetime)
        and timeout_seconds
        and timeout_seconds > 0
    ):
        elapsed_seconds = (now - last_time).total_seconds()
        expired_session = elapsed_seconds > timeout_seconds

    if expired_session:
        delete_chat_state(numero)
        clear_chat_runtime_state(numero)
        step_db = None
        notify_session_closed(numero, origin="timeout")
    elif row:
        update_chat_state(numero, step_db)

    if texto and save:
        guardar_mensaje(numero, texto, 'cliente', step=step_db)

    text_norm = normalize_text(texto or "")

    if not step_db:
        bootstrapped = True
        set_user_step(numero, Config.INITIAL_STEP)
        process_step_chain(numero, 'iniciar', platform=platform)
        if not text_norm or text_norm == 'iniciar':
            return

    if handle_global_command(numero, texto):
        return

    if _is_ia_step(get_current_step(numero)) and not bootstrapped:
        _reply_with_ai(numero, texto)
        return

    process_step_chain(
        numero,
        text_norm,
        allow_wildcard_with_text=not bootstrapped,
        platform=platform,
    )


def process_buffered_messages(numero):
    with cache_lock:
        entries = message_buffer.pop(numero, None) or []
        timer = pending_timers.pop(numero, None)
    if timer:
        timer.cancel()
    if not entries:
        return

    def _apply_tenant_context(entry):
        tenant_key = entry.get("tenant_key") if isinstance(entry, dict) else None
        tenant_env = entry.get("tenant_env") if isinstance(entry, dict) else None

        tenants.clear_current_tenant()

        if tenant_key:
            tenant = tenants.get_tenant(tenant_key)
            if tenant:
                tenants.set_current_tenant(tenant)
                tenants.set_current_tenant_env(tenants.get_tenant_env(tenant))
                return

        if tenant_env:
            tenants.set_current_tenant_env(tenant_env)
        else:
            tenants.set_current_tenant_env(tenants.get_tenant_env(None))

    state_row = get_chat_state(numero)
    if _is_agent_mode(state_row):
        step = state_row[0] if state_row else None
        update_chat_state(numero, step)
        logger.info(
            "Se omiten mensajes en buffer por estar en modo asesor",
            extra={"numero": numero, "entradas": len(entries)},
        )
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

        _apply_tenant_context(entry)

        handle_text_message(
            numero,
            raw_text if raw_text else normalized_text,
            save=False,
        )

    tenants.clear_current_tenant()

@webhook_bp.route('/webhook', methods=['GET', 'POST'])
@webhook_bp.route('/messaging-webhook', methods=['GET', 'POST'])
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

        if token == _get_verify_token():
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
        'statuses': 0,
    }

    if data.get("object") == "page":
        _handle_messenger_payload(data, summary, channel="messenger")
        logger.info(
            "Returning status=received reason=processed messenger payload summary=%s",
            summary,
        )
        return Response("EVENT_RECEIVED", status=200, mimetype="text/plain")
    if data.get("object") == "instagram":
        _handle_messenger_payload(data, summary, channel="instagram")
        logger.info(
            "Returning status=received reason=processed instagram payload summary=%s",
            summary,
        )
        return Response("EVENT_RECEIVED", status=200, mimetype="text/plain")

    for entry in data.get('entry', []):
        for change in entry.get('changes', []):
            for status in change.get('value', {}).get('statuses', []) or []:
                status_id = status.get('id')
                status_state = status.get('status')
                if not status_id or not status_state:
                    summary['unsupported'] += 1
                    continue

                status_timestamp = _coerce_status_timestamp(status.get('timestamp'))
                recipient_id = status.get('recipient_id')
                error_info = _normalize_status_error(status.get('errors'))

                guardar_estado_mensaje(
                    status_id,
                    status_state,
                    status_timestamp=status_timestamp,
                    recipient_id=recipient_id,
                    error=error_info,
                    payload=status,
                )
                summary['statuses'] += 1
                logger.info(
                    "Estado de mensaje recibido",
                    extra={
                        "message_id": _mask_identifier(status_id),
                        "status": status_state,
                        "timestamp": status_timestamp,
                        "recipient_id": recipient_id,
                        "error_code": error_info.get("code") if error_info else None,
                    },
                )

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

                chat_state_row = get_chat_state(from_number)
                agent_mode = _is_agent_mode(chat_state_row)
                estado_update = None if agent_mode else 'sin_respuesta'
                if agent_mode:
                    logger.info(
                        "Chat en modo asesor; se omite flujo automático",
                        extra={"numero": from_number, "message_id": _mask_identifier(wa_id)},
                    )

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
                    update_chat_state(from_number, step, estado_update)
                    if not agent_mode:
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
                    path = os.path.join(_media_root(), filename)
                    with open(path, 'wb') as f:
                        f.write(audio_bytes)

                    public_url = url_for(
                        'static',
                        filename=tenants.get_uploads_url_path(filename),
                        _external=True,
                        _scheme=_preferred_url_scheme(),
                    )
                    public_url = _normalize_media_url(public_url)

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

                    update_chat_state(from_number, step, estado_update)
                    if not agent_mode:
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
                    if agent_mode:
                        summary['processed'] += 1
                        continue
                    handle_text_message(from_number, "", save=False)
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
                    path        = os.path.join(_media_root(), filename)
                    with open(path, 'wb') as f:
                        f.write(media_bytes)

                    # 2) URL pública
                    public_url = url_for(
                        'static',
                        filename=tenants.get_uploads_url_path(filename),
                        _external=True,
                        _scheme=_preferred_url_scheme(),
                    )
                    public_url = _normalize_media_url(public_url)

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

                    update_chat_state(from_number, step, estado_update)
                    if not agent_mode:
                        start_typing_feedback(from_number, wa_id)

                    # 4) Registro interno
                    logging.info("Video recibido: %s", media_id)
                    if agent_mode:
                        summary['processed'] += 1
                        continue
                    handle_text_message(from_number, "", save=False)
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
                    update_chat_state(from_number, step, estado_update)
                    if not agent_mode:
                        start_typing_feedback(from_number, wa_id)
                    logging.info("Imagen recibida: %s", media_id)
                    if agent_mode:
                        summary['processed'] += 1
                        continue
                    handle_text_message(from_number, "", save=False)
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
                    update_chat_state(from_number, step, estado_update)
                    if agent_mode:
                        summary['processed'] += 1
                        continue
                    start_typing_feedback(from_number, wa_id)
                    current_tenant = tenants.get_current_tenant()
                    tenant_env = dict(tenants.get_current_tenant_env() or {})

                    with cache_lock:
                        message_buffer.setdefault(from_number, []).append(
                            {
                                'raw': text,
                                'normalized': normalized_text,
                                'tenant_key': current_tenant.tenant_key if current_tenant else None,
                                'tenant_env': tenant_env,
                            }
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
                            update_chat_state(from_number, step, estado_update)
                            if agent_mode:
                                summary['processed'] += 1
                                continue
                            start_typing_feedback(from_number, wa_id)
                            normalized_text = normalize_text(text)
                            current_tenant = tenants.get_current_tenant()
                            tenant_env = dict(tenants.get_current_tenant_env() or {})

                            with cache_lock:
                                message_buffer.setdefault(from_number, []).append(
                                    {
                                        'raw': text,
                                        'normalized': normalized_text,
                                        'tenant_key': current_tenant.tenant_key if current_tenant else None,
                                        'tenant_env': tenant_env,
                                    }
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
                            update_chat_state(from_number, step, estado_update)
                            if not agent_mode:
                                start_typing_feedback(from_number, wa_id)
                            summary['processed'] += 1
                            continue
                    opt = interactive.get('list_reply') or interactive.get('button_reply') or {}
                    option_id = opt.get('id') or ''
                    text = (opt.get('title') or '').strip()
                    step = get_current_step(from_number)
                    message_step = option_id or step
                    guardar_mensaje(
                        from_number,
                        text,
                        'cliente',
                        wa_id=wa_id,
                        reply_to_wa_id=reply_to_id,
                        step=message_step,
                    )
                    update_chat_state(from_number, step, estado_update)
                    if agent_mode:
                        summary['processed'] += 1
                        continue
                    start_typing_feedback(from_number, wa_id)
                    if handle_option_reply(from_number, option_id, platform="whatsapp"):
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
