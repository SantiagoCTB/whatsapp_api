import os
import base64
import logging
import threading
import json
import mimetypes
import re
from urllib.parse import urlparse
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
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
    update_mensaje_texto,
    guardar_flow_response,
    get_chat_state,
    obtener_historial_chat,
    obtener_ultimo_mensaje_cliente,
    obtener_ultimo_mensaje_cliente_info,
    obtener_ultimo_mensaje_cliente_media_info,
    update_chat_state,
    delete_chat_state,
)
from services.whatsapp_api import (
    download_audio,
    download_media_to_path,
    MediaTooLargeError,
    get_media_url,
    enviar_mensaje,
    _resolve_message_channel,
    start_typing_feedback,
    stop_typing_feedback,
)
from services.job_queue import enqueue_transcription
from services.normalize_text import normalize_text
from services.global_commands import handle_global_command
from services.ia_client import generate_response
from services.catalog import extract_text_from_image, find_relevant_pages
from services.ia_client import generate_response_with_image
from services.assignments import assign_chat_to_active_user

webhook_bp = Blueprint('webhook', __name__)
logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_TEXT = "No entendí tu respuesta, intenta de nuevo."
default_env = tenants.get_tenant_env(None)

# Mapa numero -> lista de textos recibidos para procesar tras un delay
message_buffer     = {}
pending_timers     = {}
cache_lock         = threading.Lock()
followup_timers    = {}
followup_lock      = threading.Lock()

MAX_AUTO_STEPS = 25
PLATFORM_AGNOSTIC_RULES = {"ia_chat"}
IA_TRIGGER_ALIASES = {"ia", "iachat"}
IA_CHAT_TIME_RANGE_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\s*$")
IA_CHAT_DAY_ALIASES = {
    "lun": 0,
    "lunes": 0,
    "mon": 0,
    "monday": 0,
    "mar": 1,
    "martes": 1,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "mie": 2,
    "mier": 2,
    "miercoles": 2,
    "miers": 2,
    "wed": 2,
    "weds": 2,
    "wednesday": 2,
    "jue": 3,
    "jueves": 3,
    "thu": 3,
    "thurs": 3,
    "thursday": 3,
    "vie": 4,
    "viernes": 4,
    "fri": 4,
    "friday": 4,
    "sab": 5,
    "sabado": 5,
    "sat": 5,
    "saturday": 5,
    "dom": 6,
    "domingo": 6,
    "sun": 6,
    "sunday": 6,
}


def _table_exists(cursor, table_name: str) -> bool:
    try:
        cursor.execute("SHOW TABLES LIKE %s", (table_name,))
        return cursor.fetchone() is not None
    except Exception:
        return False


def _is_ai_enabled() -> bool:
    conn = get_connection()
    c = conn.cursor()
    try:
        if not _table_exists(c, "ia_config"):
            return True
        c.execute("SHOW COLUMNS FROM ia_config LIKE 'enabled';")
        if not c.fetchone():
            return True
        c.execute("SELECT enabled FROM ia_config ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        return bool(row[0]) if row else True
    except Exception:
        return True
    finally:
        conn.close()


def _clear_followup_timers(numero: str) -> None:
    with followup_lock:
        timers = followup_timers.pop(numero, [])
    for timer in timers:
        timer.cancel()


def _get_ia_followup_config() -> dict | None:
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute(
            """
            SELECT followup_message_1, followup_message_2, followup_message_3,
                   followup_interval_minutes,
                   followup_media_url_1, followup_media_url_2, followup_media_url_3,
                   followup_media_tipo_1, followup_media_tipo_2, followup_media_tipo_3
              FROM ia_config
          ORDER BY id DESC
             LIMIT 1
            """
        )
        row = c.fetchone()
    except Exception:
        row = None
    finally:
        conn.close()

    if not row:
        return None

    return {
        "messages": [
            {
                "text": row[0],
                "media_url": row[4],
                "media_tipo": row[7],
            },
            {
                "text": row[1],
                "media_url": row[5],
                "media_tipo": row[8],
            },
            {
                "text": row[2],
                "media_url": row[6],
                "media_tipo": row[9],
            },
        ],
        "interval_minutes": row[3],
    }


def _get_last_message_info(numero: str) -> dict | None:
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute(
            """
            SELECT tipo, timestamp, step
              FROM mensajes
             WHERE numero = %s
          ORDER BY timestamp DESC
             LIMIT 1
            """,
            (numero,),
        )
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"tipo": row[0], "timestamp": row[1], "step": row[2]}


def _send_followup_if_pending(
    numero: str,
    followup: dict,
    *,
    interval_minutes: int,
    followup_index: int,
    message_step: str,
    scheduled_at: datetime,
    tenant_key: str | None,
    tenant_env: dict | None,
) -> None:
    message = (followup.get("text") or "").strip()
    media_url = (followup.get("media_url") or "").strip()
    media_tipo = (followup.get("media_tipo") or "").strip()
    if media_url and not media_tipo:
        mime_type, _ = mimetypes.guess_type(media_url)
        if mime_type:
            if mime_type.startswith("image/"):
                media_tipo = "image"
            elif mime_type.startswith("video/"):
                media_tipo = "video"
            elif mime_type.startswith("audio/"):
                media_tipo = "audio"
            else:
                media_tipo = "document"
            logger.info(
                "Tipo de media inferido para follow-up",
                extra={
                    "numero": numero,
                    "followup_index": followup_index,
                    "media_tipo": media_tipo,
                    "media_url": media_url,
                    "mime_type": mime_type,
                },
            )
        else:
            media_tipo = "document"
            logger.warning(
                "Tipo de media no detectado; se usará document en follow-up",
                extra={
                    "numero": numero,
                    "followup_index": followup_index,
                    "media_url": media_url,
                },
            )
    if not message and not media_url:
        return
    tenants.clear_current_tenant()
    if tenant_key:
        tenant = tenants.get_tenant(tenant_key)
        if tenant:
            tenants.set_current_tenant(tenant)
            if tenant_env:
                tenants.set_current_tenant_env(tenant_env)
            else:
                tenants.set_current_tenant_env(tenants.get_tenant_env(tenant))
    elif tenant_env:
        tenants.set_current_tenant_env(tenant_env)
    logger.info(
        "Contexto tenant para follow-up",
        extra={
            "numero": numero,
            "followup_index": followup_index,
            "tenant_key": tenant_key,
            "tenant_env_keys": sorted(list((tenant_env or {}).keys())),
        },
    )
    if interval_minutes <= 0 or followup_index <= 0:
        return
    required_seconds = interval_minutes * 60 * followup_index
    elapsed_seconds = (datetime.utcnow() - scheduled_at).total_seconds()
    remaining_seconds = max(0, required_seconds - elapsed_seconds)
    logger.info(
        "Tiempo restante para follow-up",
        extra={
            "numero": numero,
            "followup_index": followup_index,
            "remaining_seconds": remaining_seconds,
            "interval_minutes": interval_minutes,
        },
    )
    last_message_info = _get_last_message_info(numero)
    last_tipo = (last_message_info or {}).get("tipo") or ""
    if _is_client_message_type(last_tipo):
        logger.info(
            "Follow-up omitido; último mensaje es del cliente",
            extra={"numero": numero, "followup_index": followup_index, "last_tipo": last_tipo},
        )
        return
    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    last_client_ts = (last_client_info or {}).get("timestamp")
    if isinstance(last_client_ts, datetime) and last_client_ts >= scheduled_at:
        logger.info(
            "Follow-up omitido; cliente respondió después de programar",
            extra={
                "numero": numero,
                "followup_index": followup_index,
                "last_client_ts": last_client_ts,
                "scheduled_at": scheduled_at,
            },
        )
        return
    channel = _resolve_message_channel(numero)
    tipo_respuesta = media_tipo or "texto"
    logger.info(
        "Intentando enviar follow-up",
        extra={
            "numero": numero,
            "followup_index": followup_index,
            "channel": channel,
            "tipo_respuesta": tipo_respuesta,
            "has_media": bool(media_url),
            "message_len": len(message or ""),
        },
    )
    success = True
    error_reason = None
    if media_url and tipo_respuesta in {"image", "video", "audio", "document"}:
        if message and channel in {"messenger", "instagram"}:
            success, error_reason = enviar_mensaje(
                numero,
                message,
                tipo="bot",
                step=message_step,
                tipo_respuesta="texto",
                opciones=None,
                return_error=True,
            )
            if not success:
                logger.warning(
                    "No se pudo enviar texto previo al follow-up con media: %s",
                    error_reason or "sin motivo proporcionado",
                    extra={
                        "numero": numero,
                        "followup_index": followup_index,
                        "channel": channel,
                    },
                )
                return
            message = ""
        success, error_reason = enviar_mensaje(
            numero,
            message,
            tipo="bot",
            step=message_step,
            tipo_respuesta=tipo_respuesta,
            opciones=media_url,
            return_error=True,
        )
    else:
        success, error_reason = enviar_mensaje(
            numero,
            message,
            tipo="bot",
            step=message_step,
            tipo_respuesta=tipo_respuesta,
            opciones=None,
            return_error=True,
        )
    if success:
        logger.info(
            "Follow-up enviado",
            extra={"numero": numero, "followup_index": followup_index},
        )
    else:
        logger.warning(
            "No se pudo enviar follow-up: %s",
            error_reason or "sin motivo proporcionado",
            extra={
                "numero": numero,
                "followup_index": followup_index,
                "error_reason": error_reason,
                "channel": channel,
            },
        )


def _schedule_followup_messages(numero: str, message_step: str) -> None:
    message_step_norm = _normalize_step_name(message_step)
    if message_step_norm in {"ia", "ia_chat"}:
        active_hours, active_days = _get_schedule_for_step(message_step, None)
        if not _is_ia_rule_active(active_hours, active_days):
            return
    config = _get_ia_followup_config()
    if not config:
        return
    interval_minutes = config.get("interval_minutes")
    try:
        interval_minutes = int(interval_minutes)
    except (TypeError, ValueError):
        interval_minutes = None
    if not interval_minutes or interval_minutes <= 0:
        return

    raw_messages = config.get("messages") or []
    messages = [
        message
        for message in raw_messages
        if (message.get("text") or "").strip() or (message.get("media_url") or "").strip()
    ]
    if not messages:
        return

    _clear_followup_timers(numero)
    current_tenant = tenants.get_current_tenant()
    tenant_key = current_tenant.tenant_key if current_tenant else None
    tenant_env = dict(tenants.get_current_tenant_env() or {})
    scheduled = []
    scheduled_at = datetime.utcnow()
    for idx, message in enumerate(messages, start=1):
        delay_seconds = interval_minutes * 60 * idx
        timer = threading.Timer(
            delay_seconds,
            _send_followup_if_pending,
            args=(numero, message),
            kwargs={
                "interval_minutes": interval_minutes,
                "followup_index": idx,
                "message_step": message_step,
                "scheduled_at": scheduled_at,
                "tenant_key": tenant_key,
                "tenant_env": tenant_env,
            },
        )
        timer.daemon = True
        scheduled.append(timer)
        timer.start()
    with followup_lock:
        followup_timers[numero] = scheduled


def _resolve_rule_platform(numero: str) -> str:
    last_client_info = obtener_ultimo_mensaje_cliente_info(numero)
    last_tipo = (last_client_info or {}).get("tipo") or ""
    last_tipo_lower = str(last_tipo).lower()
    if "messenger" in last_tipo_lower:
        return "messenger"
    if "instagram" in last_tipo_lower:
        return "instagram"
    return "whatsapp"


def _tenant_has_channel_credentials(env: dict, channel: str) -> bool:
    if channel == "instagram":
        token = (env.get("INSTAGRAM_TOKEN") or "").strip()
        account_id = (
            (env.get("INSTAGRAM_ACCOUNT_ID") or "").strip()
            or (env.get("INSTAGRAM_PAGE_ID") or "").strip()
        )
        return bool(token and account_id)
    if channel == "messenger":
        token = (
            (env.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()
            or (env.get("PAGE_ACCESS_TOKEN") or "").strip()
            or (env.get("MESSENGER_TOKEN") or "").strip()
        )
        page_id = (
            (env.get("MESSENGER_PAGE_ID") or "").strip()
            or (env.get("PAGE_ID") or "").strip()
        )
        return bool(token and page_id)
    return True


def _resolve_channel_page_id(env: dict, channel: str) -> str:
    if channel == "instagram":
        return (
            (env.get("INSTAGRAM_ACCOUNT_ID") or "").strip()
            or (env.get("INSTAGRAM_PAGE_ID") or "").strip()
        )
    if channel == "messenger":
        return (
            (env.get("MESSENGER_PAGE_ID") or "").strip()
            or (env.get("PAGE_ID") or "").strip()
        )
    return ""


def _ensure_tenant_context_for_page(page_id: str | None, channel: str) -> None:
    if not page_id:
        return
    current_env = tenants.get_current_tenant_env() or {}
    current_page_id = _resolve_channel_page_id(current_env, channel)
    if (
        _tenant_has_channel_credentials(current_env, channel)
        and current_page_id
        and str(current_page_id).strip() == str(page_id).strip()
    ):
        return
    tenant = tenants.find_tenant_by_page_id(page_id)
    if tenant:
        tenants.set_current_tenant(tenant)


def _media_root():
    return tenants.get_media_root()


def _local_media_path_from_url(media_url: str | None) -> str | None:
    if not media_url:
        return None
    try:
        parsed = urlparse(media_url)
    except Exception:
        return None
    filename = os.path.basename(parsed.path or "")
    if not filename:
        return None
    return os.path.join(_media_root(), filename)


def _ocr_text_from_media_url(media_url: str | None) -> str:
    if not media_url:
        return ""
    local_path = _local_media_path_from_url(media_url)
    if not local_path or not os.path.exists(local_path):
        return ""
    return extract_text_from_image(local_path)


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
    update_chat_state(numero, None, "inactivo")
    return True

RELEVANT_HEADERS = (
    'X-Hub-Signature-256',
    'User-Agent',
    'Content-Type',
)


def _normalize_step_name(step):
    return (step or '').strip().lower()


def _is_client_message_type(message_type: str | None) -> bool:
    normalized = str(message_type or "").lower()
    return normalized == "cliente" or normalized.startswith("cliente_")


def _split_input_variants(value: str) -> list[str]:
    if not isinstance(value, str):
        return []
    parts = re.split(r"[,;\n]+", value)
    return [part.strip() for part in parts if part.strip()]


def _input_text_matches(text_norm: str, rule_input: str) -> bool:
    if not text_norm or not isinstance(rule_input, str):
        return False
    for part in _split_input_variants(rule_input):
        if normalize_text(part) == text_norm:
            return True
    return False


def _is_ia_trigger(value: str | None) -> bool:
    if not value:
        return False
    normalized = normalize_text(value)
    canonical = normalized.replace(" ", "")
    return canonical in IA_TRIGGER_ALIASES


def _rule_has_ia_trigger(value: str | None) -> bool:
    if not value:
        return False
    for part in _split_input_variants(str(value)):
        if _is_ia_trigger(part):
            return True
    return False


def _parse_time_range(value: str) -> tuple[int, int] | None:
    if not value:
        return None
    match = IA_CHAT_TIME_RANGE_RE.match(value)
    if not match:
        return None
    start_hour = int(match.group(1))
    start_minute = int(match.group(2) or 0)
    end_hour = int(match.group(3))
    end_minute = int(match.group(4) or 0)
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        return None
    if not (0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return None
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    return start_total, end_total


def _parse_time_ranges(value) -> list[tuple[int, int]]:
    if not value:
        return []
    ranges = []
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = re.split(r"[;,]+", str(value))
    for raw in parts:
        parsed = _parse_time_range(str(raw).strip())
        if parsed:
            ranges.append(parsed)
    return ranges


def _parse_active_days(value) -> set[int]:
    if not value:
        return set()
    if isinstance(value, (list, tuple, set)):
        raw_parts = value
    else:
        raw_parts = re.split(r"[,\s;]+", str(value))
    days = set()
    for raw in raw_parts:
        token = str(raw).strip()
        if not token:
            continue
        if "-" in token:
            start_raw, end_raw = [part.strip() for part in token.split("-", 1)]
            start_day = _coerce_weekday(start_raw)
            end_day = _coerce_weekday(end_raw)
            if start_day is None or end_day is None:
                continue
            _add_weekday_range(days, start_day, end_day)
            continue
        day = _coerce_weekday(token)
        if day is not None:
            days.add(day)
    return days


def _coerce_weekday(value: str) -> int | None:
    if not value:
        return None
    cleaned = normalize_text(value).replace(" ", "")
    if cleaned.isdigit():
        num = int(cleaned)
        if 0 <= num <= 6:
            return num
        if 1 <= num <= 7:
            return num - 1
        return None
    return IA_CHAT_DAY_ALIASES.get(cleaned)


def _add_weekday_range(days: set[int], start: int, end: int) -> None:
    if start == end:
        days.add(start)
        return
    if start < end:
        for day in range(start, end + 1):
            days.add(day)
    else:
        for day in range(start, 7):
            days.add(day)
        for day in range(0, end + 1):
            days.add(day)


def _is_minutes_in_range(minutes: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def _is_schedule_active(
    hours_value: str | None,
    days_value: str | None,
    now: datetime | None = None,
) -> bool:
    ranges = _parse_time_ranges(hours_value)
    days = _parse_active_days(days_value)
    if not ranges and not days:
        return True
    tz_name = tenants.get_runtime_setting(
        "IA_CHAT_ACTIVE_TZ",
        default=Config.IA_CHAT_ACTIVE_TZ,
    ) or "America/Bogota"
    try:
        tzinfo = ZoneInfo(str(tz_name))
    except Exception:
        logger.warning("Zona horaria inválida para IA chat: %s", tz_name)
        tzinfo = ZoneInfo("America/Bogota")
    if now is None:
        now = datetime.now(tzinfo)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tzinfo)
    else:
        now = now.astimezone(tzinfo)
    if not ranges:
        return not days or now.weekday() in days
    minutes = now.hour * 60 + now.minute
    weekday = now.weekday()
    previous_weekday = (weekday - 1) % 7
    for start, end in ranges:
        if not _is_minutes_in_range(minutes, start, end):
            continue
        if not days:
            return True
        if start < end:
            if weekday in days:
                return True
            continue
        if minutes >= start and weekday in days:
            return True
        if minutes < end and previous_weekday in days:
            return True
    return False


def _is_ia_rule_active(active_hours: str | None, active_days: str | None) -> bool:
    if not _is_ai_enabled():
        return False
    if active_hours or active_days:
        return _is_schedule_active(active_hours, active_days)
    schedule = tenants.get_runtime_setting(
        "IA_CHAT_ACTIVE_HOURS",
        default=Config.IA_CHAT_ACTIVE_HOURS,
    )
    return _is_schedule_active(schedule, None)


def _is_platform_agnostic_rule(
    *, step: str | None = None, input_text: str | None = None
) -> bool:
    for value in (step, input_text):
        if _normalize_step_name(value) in PLATFORM_AGNOSTIC_RULES:
            return True
    return False


def _platform_filter_sql(
    platform: str,
    *,
    step: str | None = None,
    input_text: str | None = None,
    column: str = "r.platform",
) -> tuple[str, tuple]:
    if _is_platform_agnostic_rule(step=step, input_text=input_text):
        return "1=1", ()
    return f"({column} IS NULL OR {column} = '' OR {column} = %s)", (platform,)


def _is_ia_step(step: str | None) -> bool:
    if _is_ia_trigger(step):
        return True
    return _normalize_step_name(step) in {'ia', 'ia_chat'}


def _should_use_ia_for_rule(input_text: str | None, step: str | None) -> bool:
    input_text_clean = (input_text or '').strip()
    if _rule_has_ia_trigger(input_text):
        return True
    return _is_ia_step(step) and input_text_clean == "*"


def _rule_schedule_fields(rule) -> tuple[str | None, str | None]:
    if not rule:
        return None, None
    active_hours = rule[8] if len(rule) > 8 else None
    active_days = rule[9] if len(rule) > 9 else None
    return active_hours, active_days


def _get_schedule_for_step(step: str | None, platform: str | None = None) -> tuple[str | None, str | None]:
    if not step:
        return None, None
    conn = get_connection()
    c = conn.cursor()
    try:
        if platform:
            filter_sql, filter_params = _platform_filter_sql(platform, step=step)
            c.execute(
                f"""
                SELECT r.active_hours, r.active_days
                  FROM reglas r
                 WHERE r.step=%s
                   AND {filter_sql}
                 ORDER BY (r.platform = %s) DESC, r.id
                 LIMIT 1
                """,
                (step, *filter_params, platform),
            )
        else:
            c.execute(
                """
                SELECT r.active_hours, r.active_days
                  FROM reglas r
                 WHERE r.step=%s
                 ORDER BY r.id
                 LIMIT 1
                """,
                (step,),
            )
        row = c.fetchone()
    finally:
        conn.close()
    if not row:
        return None, None
    return row[0], row[1]


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
    normalized_step = _normalize_step_name(step)
    if normalized_step == "ia_chat" and estado == "espera_usuario":
        estado = "ia_chat_pending"
    update_chat_state(numero, step, estado)


def get_current_step(numero):
    row = get_chat_state(numero)
    return (row[0] or '').strip().lower() if row else ''


def _extract_chat_status(row):
    if not row or len(row) < 3:
        return None
    return (row[2] or '').strip().lower() or None


def _is_ia_chat_pending(row, step: str | None = None) -> bool:
    status = _extract_chat_status(row)
    if status != "ia_chat_pending":
        return False
    normalized_step = _normalize_step_name(step if step is not None else (row[0] if row else None))
    return normalized_step == "ia_chat"


def _is_agent_mode(row) -> bool:
    return _extract_chat_status(row) == 'asesor'


def _catalog_context_for_prompt(prompt: str):
    """Obtiene contenido relevante del portafolio y prepara un contexto robusto."""

    pages = find_relevant_pages(prompt, limit=3)
    if not pages:
        return "", []

    price_range = _extract_price_range(prompt)
    if price_range:
        ranged_pages = [
            page for page in pages
            if _page_has_price_in_range(page, price_range)
        ]
        if ranged_pages:
            pages = ranged_pages

    context_lines: list[str] = []

    for page in pages:
        snippet = (page.text_content or "").strip()
        if len(snippet) > 800:
            snippet = f"{snippet[:780]}..."

        prices = _extract_prices(page.text_content or "")
        image_rel = ""
        if page.image_filename:
            image_rel = tenants.get_uploads_url_path(f"ia_pages/{page.image_filename}")

        context_lines.append(
            "- registro: {registro}\n  texto: {texto}\n  precios_detectados: {precios}\n  imagen_rel: {rel}".format(
                registro=page.page_number,
                texto=snippet,
                precios=", ".join(f"${price:,}".replace(",", ".") for price in prices) or "N/A",
                rel=f"/static/{image_rel}" if image_rel else "",
            )
        )

    return "\n".join(context_lines), pages


def _extract_prices(text: str) -> list[int]:
    if not text:
        return []
    normalized = text.replace("\u00a0", " ").lower()

    def _append_unique(values: list[int], value: int) -> None:
        if value in values:
            return
        values.append(value)

    def _parse_number_token(token: str) -> float | None:
        cleaned = token.strip()
        if not cleaned:
            return None
        has_dot = "." in cleaned
        has_comma = "," in cleaned
        if has_dot and has_comma:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif has_comma:
            parts = cleaned.split(",")
            if len(parts[-1]) == 3 and len(parts) > 1:
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
        elif has_dot:
            parts = cleaned.split(".")
            if len(parts[-1]) == 3 and len(parts) > 1:
                cleaned = cleaned.replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    prices: list[int] = []

    matches = re.findall(r"(?:\\$\\s*)?(\\d{1,3}(?:[\\.,]\\d{3})+|\\d{4,})", normalized)
    for token in matches:
        value = _parse_number_token(token)
        if value is None:
            continue
        price = int(round(value))
        if price <= 0:
            continue
        if price < 1000:
            continue
        _append_unique(prices, price)

    unit_matches = re.findall(
        r"(\\d+(?:[\\.,]\\d+)?)\\s*(mil|miles|k|millon(?:es)?|mm)",
        normalized,
    )
    for amount, unit in unit_matches:
        value = _parse_number_token(amount)
        if value is None:
            continue
        factor = 1000 if unit in {"mil", "miles", "k"} else 1_000_000
        price = int(round(value * factor))
        if price <= 0:
            continue
        _append_unique(prices, price)

    currency_prefix = re.findall(
        r"(?:\\$|cop|pesos?|usd|dolares?|eur|euros?)\\s*(\\d{1,3})(?![\\d\\.,])",
        normalized,
    )
    currency_suffix = re.findall(
        r"(\\d{1,3})(?![\\d\\.,])\\s*(?:pesos?|usd|dolares?|cop|eur|euros?)",
        normalized,
    )
    for token in currency_prefix + currency_suffix:
        if not token.isdigit():
            continue
        value = int(token)
        if value <= 0:
            continue
        _append_unique(prices, value)

    return prices


def _extract_price_range(prompt: str) -> tuple[int | None, int | None] | None:
    if not prompt:
        return None
    normalized = normalize_text(prompt)
    numbers = _extract_prices(prompt)
    if not numbers:
        return None
    numbers.sort()
    if "entre" in normalized and len(numbers) >= 2:
        return numbers[0], numbers[-1]
    if "hasta" in normalized or "menos" in normalized or "max" in normalized:
        return None, numbers[-1]
    if "mas de" in normalized or "mínimo" in normalized or "minimo" in normalized:
        return numbers[0], None
    if len(numbers) >= 2:
        return numbers[0], numbers[-1]
    return None


def _page_has_price_in_range(page, price_range: tuple[int | None, int | None]) -> bool:
    prices = _extract_prices(page.text_content or "")
    if not prices:
        return False
    min_price, max_price = price_range
    for price in prices:
        if min_price is not None and price < min_price:
            continue
        if max_price is not None and price > max_price:
            continue
        return True
    return False


def _page_keywords_for_match(page) -> set[str]:
    keywords = page.keywords if isinstance(page.keywords, list) else []
    normalized_keywords = {
        normalize_text(keyword)
        for keyword in keywords
        if isinstance(keyword, str) and keyword.strip()
    }
    if normalized_keywords:
        return {kw for kw in normalized_keywords if len(kw) > 2}

    tokens = normalize_text(page.text_content or "").split()
    selected: list[str] = []
    for token in tokens:
        if len(token) <= 3:
            continue
        if token in selected:
            continue
        selected.append(token)
        if len(selected) >= 20:
            break
    return set(selected)


def _matched_catalog_pages(response: str, pages):
    if not response or not pages:
        return []
    response_tokens = set(normalize_text(response).split())
    if not response_tokens:
        return []

    matched = []
    for page in pages:
        keywords = _page_keywords_for_match(page)
        if not keywords:
            continue

        matches = [keyword for keyword in keywords if keyword in response_tokens]
        if not matches:
            continue

        required_matches = 2
        if len(keywords) <= 2:
            required_matches = 1 if any(len(keyword) >= 6 for keyword in keywords) else 2

        if len(matches) >= required_matches:
            matched.append(page)
    return matched


def _combine_system_prompts(*prompts: str | None) -> str | None:
    cleaned = [prompt.strip() for prompt in prompts if prompt and prompt.strip()]
    if not cleaned:
        return None
    return "\n\n".join(cleaned)


def _get_ia_system_prompt() -> str | None:
    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute(
            """
            SELECT system_prompt, business_description
              FROM ia_config
          ORDER BY id DESC
             LIMIT 1
            """
        )
        row = c.fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not row:
        return None
    system_prompt = (row[0] or "").strip()
    business_description = (row[1] or "").strip()
    if business_description:
        business_description = f"Contexto del negocio:\n{business_description}"
    return _combine_system_prompts(system_prompt, business_description)


def _mark_ai_flow_error(numero: str, step: str | None, reason: str) -> None:
    resolved_step = step or get_current_step(numero)
    update_chat_state(numero, resolved_step, "error_flujo")
    logger.warning(
        "Se marcó el chat con error de flujo por IA",
        extra={"numero": numero, "reason": reason, "step": resolved_step},
    )
    enviar_mensaje(
        numero,
        "Estoy revisando tu solicitud, dame un momento",
        tipo="bot",
        step=resolved_step,
    )


def _reply_with_ai_image(
    numero: str,
    *,
    media_url: str,
    prompt_prefix: str | None = None,
    set_step: bool = True,
    history_step: str | None = None,
    message_step: str | None = None,
) -> bool:
    image_url = _normalize_media_url(media_url)
    if not image_url:
        logger.info("Sin URL de imagen para enviar a la IA", extra={"numero": numero})
        return False

    if set_step:
        set_user_step(numero, "ia")
        update_chat_state(numero, "ia", "ia_activa")

    if history_step:
        history = obtener_historial_chat(
            numero,
            limit=_ia_history_limit(),
            step=history_step,
            anchor_step="menu_principal",
        )
    else:
        history = obtener_historial_chat(
            numero,
            limit=_ia_history_limit(),
            anchor_step="menu_principal",
        )

    if not message_step:
        message_step = "ia" if set_step else get_current_step(numero)

    base_prompt = (prompt_prefix or "").strip() or (
        "El usuario envió una imagen. "
        "Lee el contenido como se procesa el catálogo y "
        "busca coincidencias con el catálogo para responder."
    )
    ocr_text = _ocr_text_from_media_url(image_url)
    if ocr_text:
        prompt = "\n".join(
            [
                base_prompt,
                "Texto detectado en la imagen:\n"
                f"{ocr_text}",
            ]
        )
    else:
        prompt = base_prompt

    response = generate_response_with_image(history, prompt, image_url, system_message=_get_ia_system_prompt())
    if not response:
        logger.warning("La IA no devolvió respuesta con imagen", extra={"numero": numero})
        _mark_ai_flow_error(numero, message_step, "ia_sin_respuesta_imagen")
        return False

    enviar_mensaje(numero, response, tipo="bot", step=message_step)
    message_step_norm = _normalize_step_name(message_step)
    _schedule_followup_messages(numero, message_step)
    media_pages = None
    if _is_ia_step(message_step_norm):
        media_pages = find_relevant_pages(response, limit=2)
    matched_pages = _matched_catalog_pages(response, media_pages or [])
    if not matched_pages:
        logger.info(
            "Respuesta IA sin referencias claras a producto; se omiten imágenes",
            extra={"numero": numero},
        )
        return True
    for page in matched_pages:
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


def _reply_with_ai(
    numero: str,
    user_text: str | None,
    *,
    system_prompt: str | None = None,
    set_step: bool = True,
    history_step: str | None = None,
    message_step: str | None = None,
    allow_empty_catalog: bool = False,
) -> bool:
    """Envía el mensaje al modelo de IA y responde al usuario."""

    prompt = (user_text or "").strip() or obtener_ultimo_mensaje_cliente(numero)
    if not prompt:
        last_media = obtener_ultimo_mensaje_cliente_media_info(numero)
        if last_media and last_media.get("media_url") and _is_ia_step(message_step or get_current_step(numero)):
            return _reply_with_ai_image(
                numero,
                media_url=last_media["media_url"],
                set_step=set_step,
                history_step=history_step,
                message_step=message_step,
            )
    if not prompt:
        logger.info("Sin texto para enviar a la IA", extra={"numero": numero})
        return False

    if system_prompt is None:
        system_prompt = _get_ia_system_prompt()

    if set_step:
        set_user_step(numero, "ia")
        update_chat_state(numero, "ia", "ia_activa")

    if history_step:
        history = obtener_historial_chat(
            numero,
            limit=_ia_history_limit(),
            step=history_step,
            anchor_step="menu_principal",
        )
    else:
        history = obtener_historial_chat(
            numero,
            limit=_ia_history_limit(),
            anchor_step="menu_principal",
        )

    if not message_step:
        message_step = "ia" if set_step else get_current_step(numero)
    catalog_context, pages = _catalog_context_for_prompt(prompt)
    if not catalog_context and not allow_empty_catalog:
        logger.warning(
            "Sin contexto de portafolio para la IA; se marcará error de flujo",
            extra={"numero": numero},
        )
        _mark_ai_flow_error(numero, message_step, "ia_sin_contexto_catalogo")
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
            "- Si no hay coincidencias claras, pide más detalles al usuario sin inventar datos.\n"
            "- Si el usuario pide un rango de precio y el contexto incluye precios, propone productos que cumplan; "
            "si no hay precios claros, pide más detalles sin asumir que no existen.\n"
            "- Usa el campo precios_detectados para comparar rangos de precio cuando esté disponible."
        )
    elif allow_empty_catalog:
        prompt_for_model = (
            f"{prompt}\n\n"
            "Instrucciones para la respuesta:\n"
            "- Responde únicamente con datos disponibles en este contexto.\n"
            "- Si la información es insuficiente, pide más detalles al usuario sin inventar datos.\n"
            "- No menciones procesos internos ni el origen del contenido."
        )

    logger.info(
        "Preparando solicitud a IA",
        extra={
            "numero": numero,
            "history_length": len(history or []),
            "catalog_pages": len(pages),
            "prompt_length": len(prompt_for_model or ""),
        },
    )
    response = generate_response(history, prompt_for_model, system_message=system_prompt)
    if not response:
        logger.warning("La IA no devolvió respuesta", extra={"numero": numero})
        _mark_ai_flow_error(numero, message_step, "ia_sin_respuesta")
        return False

    enviar_mensaje(numero, response, tipo="bot", step=message_step)
    message_step_norm = _normalize_step_name(message_step)
    _schedule_followup_messages(numero, message_step)
    media_pages = pages
    if _is_ia_step(message_step_norm):
        media_pages = find_relevant_pages(response, limit=2)
    matched_pages = _matched_catalog_pages(response, media_pages)
    if not matched_pages:
        logger.info(
            "Respuesta IA sin referencias claras a producto; se omiten imágenes",
            extra={"numero": numero},
        )
        return True
    for page in matched_pages:
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
        entry_page_id = entry.get("id")
        _ensure_tenant_context_for_page(entry_page_id, channel)
        events = list(entry.get("messaging", []) or [])
        for change in entry.get("changes", []) or []:
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
                            "field": change.get("field"),
                        }
                    )
            else:
                if isinstance(value, dict):
                    enriched_value = dict(value)
                    enriched_value["field"] = change.get("field")
                    events.append(enriched_value)
                else:
                    events.append({"value": value, "field": change.get("field")})
        for event in events:
            handled = False
            message = event.get("message") or {}
            sender_id = (event.get("sender") or {}).get("id")
            if not sender_id:
                summary["unsupported"] += 1
                continue
            recipient_id = (event.get("recipient") or {}).get("id")
            _ensure_tenant_context_for_page(recipient_id, channel)

            delivery = event.get("delivery") or {}
            if delivery:
                mids = delivery.get("mids") or []
                timestamp = _coerce_messenger_timestamp(delivery.get("watermark"))
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
            if not agent_mode and _is_ia_chat_pending(chat_state_row):
                estado_update = "ia_chat_pending"
            if agent_mode:
                logger.info(
                    "Chat en modo asesor; se omite flujo automático",
                    extra={"numero": sender_id, "message_id": _mask_identifier(message_id)},
                )

            quick_reply_payload = (message.get("quick_reply") or {}).get("payload")
            text = (message.get("text") or "").strip()
            if text or quick_reply_payload:
                _clear_followup_timers(sender_id)
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
                _clear_followup_timers(sender_id)
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
                _clear_followup_timers(sender_id)
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

    def _input_option_matches(option_norm: str, input_value: str) -> bool:
        if not option_norm or not isinstance(input_value, str):
            return False
        for part in _split_input_variants(input_value):
            if _normalize_option_value(part) == option_norm:
                return True
        return False

    option_norm = _normalize_option_value(option_id)
    if not option_norm:
        return False

    def _fetch_rules(step_filter=None):
        conn = get_connection(); c = conn.cursor()
        try:
            filter_sql, filter_params = _platform_filter_sql(
                platform,
                step=step_filter,
                input_text=option_id if step_filter is None else None,
            )
            if step_filter is not None:
                c.execute(
                    f"""
                    SELECT r.step,
                           r.id, r.respuesta, r.siguiente_step, r.tipo,
                           GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                           r.opciones, r.rol_keyword, r.input_text,
                           r.active_hours, r.active_days
                     FROM reglas r
                     LEFT JOIN regla_medias m ON r.id = m.regla_id
                     WHERE r.step=%s
                       AND {filter_sql}
                     GROUP BY r.step, r.id
                     ORDER BY (r.platform = %s) DESC, r.id
                    """,
                    (step_filter, *filter_params, platform),
                )
            else:
                c.execute(
                    f"""
                    SELECT r.step,
                           r.id, r.respuesta, r.siguiente_step, r.tipo,
                           GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                           r.opciones, r.rol_keyword, r.input_text,
                           r.active_hours, r.active_days
                     FROM reglas r
                     LEFT JOIN regla_medias m ON r.id = m.regla_id
                     WHERE r.input_text IS NOT NULL
                       AND r.input_text <> ''
                       AND {filter_sql}
                     GROUP BY r.step, r.id
                     ORDER BY (r.platform = %s) DESC, r.id
                    """,
                    (*filter_params, platform),
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
            if _input_option_matches(option_norm, (row[8] or '').strip())
        ]
        if not matches:
            return None
        for row in matches:
            is_ia = _should_use_ia_for_rule((row[8] or '').strip(), row[0])
            if is_ia and not _is_ia_rule_active(row[9], row[10]):
                continue
            if _normalize_option_value((row[0] or '').strip()) == option_norm:
                return row
        for row in matches:
            is_ia = _should_use_ia_for_rule((row[8] or '').strip(), row[0])
            if is_ia and not _is_ia_rule_active(row[9], row[10]):
                continue
            return row
        return None

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
    filter_sql, filter_params = _platform_filter_sql(platform, step=current_step)
    c.execute(
        f"""
        SELECT r.opciones
          FROM reglas r
         WHERE r.step=%s
           AND {filter_sql}
         ORDER BY (r.platform = %s) DESC, r.id
        """,
        (current_step, *filter_params, platform),
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


def _apply_role_keyword(numero: str, role_keyword: str | None) -> None:
    if not (role_keyword or "").strip():
        return
    role_keyword = role_keyword.strip()
    conn = get_connection(); c = conn.cursor()
    c.execute("SELECT id FROM roles WHERE keyword=%s", (role_keyword,))
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
            (numero, role[0]),
        )
        conn.commit()
    conn.close()
    assign_chat_to_active_user(numero, role_keyword)


def _apply_role_for_step(numero: str, step: str | None, platform: str) -> None:
    if not (step or "").strip():
        return
    conn = get_connection(); c = conn.cursor()
    try:
        filter_sql, filter_params = _platform_filter_sql(platform, step=step)
        c.execute(
            f"""
            SELECT r.rol_keyword
              FROM reglas r
             WHERE r.step=%s
               AND {filter_sql}
               AND r.rol_keyword IS NOT NULL
               AND r.rol_keyword <> ''
             ORDER BY (r.platform = %s) DESC, r.id
             LIMIT 1
            """,
            (step, *filter_params, platform),
        )
        row = c.fetchone()
    finally:
        conn.close()
    if row:
        _apply_role_keyword(numero, row[0])


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
    regla_id = regla[0]
    resp = regla[1] if len(regla) > 1 else None
    next_step_raw = regla[2] if len(regla) > 2 else None
    tipo_resp = regla[3] if len(regla) > 3 else None
    media_urls = regla[4] if len(regla) > 4 else None
    opts = regla[5] if len(regla) > 5 else None
    rol_kw = regla[6] if len(regla) > 6 else None
    input_text = regla[7] if len(regla) > 7 else None
    active_hours = regla[8] if len(regla) > 8 else None
    active_days = regla[9] if len(regla) > 9 else None
    current_step = step or get_current_step(numero)
    current_step_norm = _normalize_step_name(current_step)
    if current_step_norm:
        visited.add(current_step_norm)

    input_text_clean = (input_text or '').strip()
    use_ia = _should_use_ia_for_rule(input_text, current_step)
    if use_ia and not _is_ia_rule_active(active_hours, active_days):
        use_ia = False
    if use_ia:
        system_prompt = _combine_system_prompts(_get_ia_system_prompt(), resp)
        _reply_with_ai(
            numero,
            obtener_ultimo_mensaje_cliente(numero),
            system_prompt=system_prompt,
            set_step=False,
            history_step=None,
            message_step=current_step,
        )
        _apply_role_keyword(numero, rol_kw)
        next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
        if next_step:
            advance_steps(numero, next_step, visited=visited, platform=platform)
        return

    media_list = media_urls.split('||') if media_urls else []
    if tipo_resp in {'texto', 'lista', 'boton'} and not (resp or '').strip() and not media_list:
        _apply_role_keyword(numero, rol_kw)
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
    _schedule_followup_messages(numero, current_step)
    _apply_role_keyword(numero, rol_kw)
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
            filter_sql, filter_params = _platform_filter_sql(platform, step=step)
            c.execute(
                f"""
                SELECT r.id, r.respuesta, r.siguiente_step, r.tipo,
                       GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                       r.opciones, r.rol_keyword, r.input_text,
                       r.active_hours, r.active_days
                  FROM reglas r
                  LEFT JOIN regla_medias m ON r.id = m.regla_id
                 WHERE r.step=%s
                   AND r.input_text='*'
                   AND {filter_sql}
                 GROUP BY r.id
                 ORDER BY (r.platform = %s) DESC, r.id
                 LIMIT 1
                """,
                (step, *filter_params, platform),
            )
            regla = c.fetchone()
        finally:
            conn.close()
        if regla:
            active_hours, active_days = _rule_schedule_fields(regla)
            if _should_use_ia_for_rule(regla[7], step) and not _is_ia_rule_active(
                active_hours,
                active_days,
            ):
                set_user_step(numero, step)
                return
            dispatch_rule(numero, regla, step, visited=visited, platform=platform)
        else:
            # No hay regla comodín; respetar el orden y detener el avance
            # para esperar la respuesta del usuario en este paso.
            set_user_step(numero, step)
            return
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
    allow_wildcard_when_no_specific=True,
    raw_text: str | None = None,
):
    """Procesa el step actual una sola vez.

    Las reglas con ``input_text='*'`` pueden ejecutarse incluso si no se
    recibió texto del usuario, pero tras la primera ejecución el flujo se
    detiene y espera una nueva entrada.
    """
    handled = False
    if visited is None:
        visited = set()
    if not platform:
        platform = _resolve_rule_platform(numero)
    step = get_current_step(numero)
    if not step:
        return handled
    step_norm = _normalize_step_name(step)
    if step_norm:
        visited.add(step_norm)

    conn = get_connection(); c = conn.cursor()
    filter_sql, filter_params = _platform_filter_sql(platform, step=step)
    # Ordenar reglas para evaluar primero las de menor ID (o prioridad).
    c.execute(
        f"""
        SELECT r.id, r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword, r.input_text,
               r.active_hours, r.active_days
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s
           AND {filter_sql}
         GROUP BY r.id
         ORDER BY (r.platform = %s) DESC, r.id
        """,
        (step, *filter_params, platform),
    )
    reglas = c.fetchall(); conn.close()
    if not reglas:
        return handled

    comodines = [r for r in reglas if (r[7] or '').strip() == '*']
    specific_rules = [r for r in reglas if (r[7] or '').strip() not in ('', '*')]

    wildcard_allowed = (
        text_norm is None
        or allow_wildcard_with_text
        or (allow_wildcard_when_no_specific and not specific_rules)
    )

    # No avanzar si no hay texto del usuario, salvo que existan comodines
    if text_norm is None and not comodines:
        return handled

    # Coincidencia exacta
    for r in reglas:
        patt = (r[7] or '').strip()
        if not patt or patt == '*' or not _input_text_matches(text_norm, patt):
            continue
        active_hours, active_days = _rule_schedule_fields(r)
        if _should_use_ia_for_rule(patt, step) and not _is_ia_rule_active(
            active_hours,
            active_days,
        ):
            continue
        dispatch_rule(numero, r, step, visited=visited, platform=platform)
        handled = True
        return handled

    def _select_global_rule():
        if text_norm is None:
            return None
        conn = get_connection(); c = conn.cursor()
        try:
            c.execute(
                """
                SELECT r.step, r.platform, r.id, r.respuesta, r.siguiente_step, r.tipo,
                       GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                       r.opciones, r.rol_keyword, r.input_text,
                       r.active_hours, r.active_days
                  FROM reglas r
                  LEFT JOIN regla_medias m ON r.id = m.regla_id
                 WHERE r.input_text IS NOT NULL
                   AND r.input_text <> ''
                 GROUP BY r.step, r.platform, r.id
                 ORDER BY (r.platform = %s) DESC, r.id
                """,
                (platform,),
            )
            rows = c.fetchall()
        finally:
            conn.close()
        matches = []
        for row in rows:
            rule_step = (row[0] or '').strip()
            rule_platform = row[1]
            rule_input = (row[9] or '').strip()
            if not _input_text_matches(text_norm, rule_input):
                continue
            active_hours = row[10] if len(row) > 10 else None
            active_days = row[11] if len(row) > 11 else None
            if _is_platform_agnostic_rule(step=rule_step, input_text=rule_input):
                is_ia = _should_use_ia_for_rule(rule_input, rule_step)
                if is_ia and not _is_ia_rule_active(active_hours, active_days):
                    continue
                matches.append(row)
                continue
            if not rule_platform or rule_platform == platform:
                is_ia = _should_use_ia_for_rule(rule_input, rule_step)
                if is_ia and not _is_ia_rule_active(active_hours, active_days):
                    continue
                matches.append(row)
        if not matches:
            return None
        return matches[0]

    global_rule = _select_global_rule()
    if global_rule:
        rule_step = (global_rule[0] or '').strip().lower()
        rule = global_rule[2:]
        effective_step = rule_step or step
        set_user_step(numero, effective_step)
        dispatch_rule(numero, rule, step=effective_step, visited=visited, platform=platform)
        handled = True
        return handled

    # Regla comodín
    if comodines and wildcard_allowed:
        selected_rule = None
        for r in comodines:
            active_hours, active_days = _rule_schedule_fields(r)
            if _should_use_ia_for_rule(r[7], step) and not _is_ia_rule_active(
                active_hours,
                active_days,
            ):
                continue
            selected_rule = r
            break
        if selected_rule is None:
            return handled
        dispatch_rule(numero, selected_rule, step, visited=visited, platform=platform)
        # No procesar recursivamente otros comodines; esperar nueva entrada
        handled = True
        return handled

    if text_norm is None:
        return handled

    if specific_rules and not wildcard_allowed:
        # Se recibió texto pero no hay coincidencias y se decidió no ejecutar
        # comodines. Esto ocurre, por ejemplo, en el primer mensaje del
        # usuario tras iniciar el flujo, donde se espera que el bot ya haya
        # enviado las instrucciones y aguarde una nueva respuesta válida.
        return handled

    if raw_text and handle_option_reply(numero, raw_text, platform=platform):
        handled = True
        return handled

    logging.warning("Fallback en step '%s' para entrada '%s'", step, text_norm)
    update_chat_state(numero, get_current_step(numero), 'sin_regla')
    return handled


@register_handler('barra_medida')
@register_handler('meson_recto_medida')
@register_handler('meson_l_medida')
def handle_medicion(numero, texto):
    step_actual = get_current_step(numero)
    platform = _resolve_rule_platform(numero)
    conn = get_connection(); c = conn.cursor()
    filter_sql, filter_params = _platform_filter_sql(platform, step=step_actual)
    c.execute(
        f"""
        SELECT r.respuesta, r.siguiente_step, r.tipo,
               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
               r.opciones, r.rol_keyword, r.calculo, r.handler
          FROM reglas r
          LEFT JOIN regla_medias m ON r.id = m.regla_id
         WHERE r.step=%s
           AND r.input_text='*'
           AND {filter_sql}
         GROUP BY r.id
         ORDER BY (r.platform = %s) DESC, r.id
         LIMIT 1
        """,
        (step_actual, *filter_params, platform)
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
            assign_chat_to_active_user(numero, rol_kw)
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
    _clear_followup_timers(numero)
    # Se usa UTC para evitar desfaces de zona horaria entre la app y la base
    # de datos que puedan disparar expiraciones falsas.
    now = datetime.utcnow()
    row = get_chat_state(numero)
    step_db = row[0] if row else None
    last_time = row[1] if row else None
    chat_status = _extract_chat_status(row)
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
        chat_status = None
        notify_session_closed(numero, origin="timeout")
    elif row:
        update_chat_state(numero, step_db, chat_status)

    if texto and save:
        guardar_mensaje(numero, texto, 'cliente', step=step_db)

    text_norm = normalize_text(texto or "")
    if not text_norm:
        text_norm = None

    if not step_db:
        bootstrapped = True
        set_user_step(numero, Config.INITIAL_STEP)
        process_step_chain(numero, 'iniciar', platform=platform)
        if not text_norm or text_norm == 'iniciar':
            return

    if handle_global_command(numero, texto):
        return

    if _is_ia_chat_pending(row, step_db) and not bootstrapped:
        update_chat_state(numero, step_db, "espera_usuario")

    if _is_ia_step(get_current_step(numero)) and not bootstrapped:
        platform = platform or _resolve_rule_platform(numero)
        handled = process_step_chain(
            numero,
            text_norm,
            allow_wildcard_with_text=True,
            allow_wildcard_when_no_specific=True,
            platform=platform,
            raw_text=texto,
        )
        if handled:
            return
        current_step = get_current_step(numero)
        current_step_norm = _normalize_step_name(current_step)
        _apply_role_for_step(numero, current_step, platform)
        if not _is_ia_rule_active(*_get_schedule_for_step(current_step, platform)):
            return
        if current_step_norm == "ia_chat":
            _reply_with_ai(
                numero,
                texto,
                set_step=False,
                message_step=current_step,
            )
        else:
            _reply_with_ai(numero, texto)
        return

    process_step_chain(
        numero,
        text_norm,
        allow_wildcard_with_text=not bootstrapped,
        platform=platform,
        raw_text=texto,
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
                if tenant_env:
                    tenants.set_current_tenant_env(tenant_env)
                else:
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
        logger.info("Returning status=received reason=missing object field")
        return Response("EVENT_RECEIVED", status=200, mimetype="text/plain")

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
                if not agent_mode and _is_ia_chat_pending(chat_state_row):
                    estado_update = "ia_chat_pending"
                if agent_mode:
                    logger.info(
                        "Chat en modo asesor; se omite flujo automático",
                        extra={"numero": from_number, "message_id": _mask_identifier(wa_id)},
                    )

                _clear_followup_timers(from_number)

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

                    # 1) Descargar y guardar en static/uploads sin cargar todo en memoria
                    filename = f"{media_id}.{ext}"
                    path = os.path.join(_media_root(), filename)
                    try:
                        download_media_to_path(
                            media_id,
                            path,
                            max_bytes=Config.MAX_VIDEO_BYTES,
                        )
                    except MediaTooLargeError:
                        logging.warning(
                            "Video supera el máximo permitido",
                            extra={
                                "media_id": media_id,
                                "max_mb": Config.MAX_VIDEO_MB,
                            },
                        )
                        if os.path.exists(path):
                            os.remove(path)
                        summary['processed'] += 1
                        continue

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
                    caption = (msg.get("image") or {}).get("caption") or ""
                    caption = caption.strip()
                    db_id = guardar_mensaje(
                        from_number,
                        caption,
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
                    if _is_ia_step(step):
                        if _normalize_step_name(step) == "ia_chat":
                            local_path = None
                            public_url = None
                            mime_clean = "image/jpeg"
                            image_bytes = None
                            try:
                                mime_raw = msg['image'].get('mime_type', 'image/jpeg')
                                mime_clean = mime_raw.split(';')[0].strip() or "image/jpeg"
                                ext = mime_clean.split('/')[-1] or "jpg"
                                image_bytes = download_audio(media_id)
                                filename = f"{media_id}.{ext}"
                                local_path = os.path.join(_media_root(), filename)
                                with open(local_path, 'wb') as f:
                                    f.write(image_bytes)
                                public_url = url_for(
                                    'static',
                                    filename=tenants.get_uploads_url_path(filename),
                                    _external=True,
                                    _scheme=_preferred_url_scheme(),
                                )
                                public_url = _normalize_media_url(public_url)
                            except Exception:
                                logger.exception(
                                    "No se pudo descargar la imagen para IA",
                                    extra={"numero": from_number, "media_id": media_id},
                                )
                                public_url = _normalize_media_url(media_url)

                            image_text = extract_text_from_image(local_path) if local_path else ""

                            prompt_lines = [
                                "El usuario envió una imagen. "
                                "Lee el contenido como se procesa el catálogo y "
                                "busca coincidencias con el catálogo para responder."
                            ]
                            if caption:
                                prompt_lines.append(
                                    "Texto adjunto del usuario:\n"
                                    f"{caption}"
                                )
                            if image_text:
                                prompt_lines.append(
                                    "Texto detectado en la imagen:\n"
                                    f"{image_text}"
                                )
                            prompt_prefix = "\n".join(prompt_lines)

                            image_url = None
                            if image_bytes:
                                encoded = base64.b64encode(image_bytes).decode("utf-8")
                                image_url = f"data:{mime_clean};base64,{encoded}"

                            responded = _reply_with_ai_image(
                                from_number,
                                media_url=image_url or public_url or media_url,
                                prompt_prefix=prompt_prefix,
                                set_step=False,
                                history_step=None,
                                message_step=step,
                            )
                            if not responded:
                                enviar_mensaje(
                                    from_number,
                                    "No pude procesar la imagen en este momento. "
                                    "¿Puedes describir el producto o dar más detalles?",
                                    tipo="bot",
                                    step=step,
                                )
                            summary['processed'] += 1
                            continue
                        local_path = None
                        try:
                            mime_raw = msg['image'].get('mime_type', 'image/jpeg')
                            mime_clean = mime_raw.split(';')[0].strip()
                            ext = mime_clean.split('/')[-1] or "jpg"
                            image_bytes = download_audio(media_id)
                            filename = f"{media_id}.{ext}"
                            local_path = os.path.join(_media_root(), filename)
                            with open(local_path, 'wb') as f:
                                f.write(image_bytes)
                        except Exception:
                            logger.exception(
                                "No se pudo descargar la imagen para OCR",
                                extra={"numero": from_number, "media_id": media_id},
                            )
                            local_path = _local_media_path_from_url(media_url)
                        image_text = extract_text_from_image(local_path) if local_path else ""

                        prompt_parts = []
                        if caption:
                            prompt_parts.append(
                                "Texto adjunto del usuario:\n"
                                f"{caption}"
                            )
                        if image_text:
                            prompt_parts.append(
                                "Texto detectado en la imagen:\n"
                                f"{image_text}"
                            )
                        if prompt_parts:
                            combined_prompt = (
                                "El usuario envió una imagen. "
                                "Lee el contenido como se procesa el catálogo y "
                                "busca coincidencias con el catálogo para responder.\n"
                                f"{chr(10).join(prompt_parts)}"
                            )
                            update_mensaje_texto(db_id, "\n\n".join(prompt_parts))
                            _reply_with_ai(
                                from_number,
                                combined_prompt,
                                set_step=False,
                                history_step=None,
                                message_step=step,
                                allow_empty_catalog=True,
                            )
                        else:
                            enviar_mensaje(
                                from_number,
                                "No pude leer claramente el contenido de la imagen. "
                                "¿Puedes describir el producto o dar más detalles?",
                                tipo="bot",
                                step=step,
                            )
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
