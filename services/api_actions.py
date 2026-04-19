"""Servicio que ejecuta acciones de tipo 'api_call' y 'guardar_input'
desde el motor de reglas del chatbot.

Tipos de regla soportados:
  - api_call    : llama a una api_conexion, almacena resultado, envía respuesta al chat
  - guardar_input: guarda el texto del usuario o su selección como chat_variable

Formato JSON del campo `respuesta` para tipo `api_call`:
{
  "conexion_id": 1,
  "path": "/api/v1/origins-and-destinies",
  "method": "GET",          // opcional, sobreescribe el de la conexión
  "url_vars": {             // valores para placeholders {{var}} en `path`
    "originId": "{{origen_id}}",
    "destinyId": "{{destino_id}}",
    "departureDate": "{{fecha_viaje}}"
  },
  "body": {                 // body para POST/PUT/PATCH; puede usar {{var}}
    "bearingId": "{{bearing_id}}"
  },
  "store_as": "resultado_bearings",   // clave en chat_variables donde guardar la respuesta
  "data_path": "data",                // ruta punteada para extraer sub-objeto (ej: "data.items")
  "format": {                         // cómo presentar la respuesta al usuario
    "tipo": "lista",                  // "lista" | "boton" | "texto"
    "id_field": "id",
    "title_field": "nombre",
    "description_field": "ciudad",    // opcional
    "section_title": "Orígenes",
    "header": "¿Desde dónde viajas?",
    "footer": "Toca para elegir",
    "button": "Ver opciones",
    "store_selected_as": "origen_id"  // guarda el ID elegido en chat_vars al seleccionar
  },
  "message": "Elige tu ciudad de origen:"  // texto del mensaje / cuerpo de la lista
}

Formato JSON del campo `respuesta` para tipo `guardar_input`:
{
  "store_as": "nombre_cliente",    // clave en chat_variables
  "from": "text"                   // "text" (texto del usuario) | "option_id" (id de opción)
}
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_int(value: Any, default: int) -> int:
    """Convierte `value` a int; retorna `default` si falla."""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_date_input(value: str) -> str | None:
    """Normaliza input del usuario a formato YYYY-MM-DD.

    Acepta: hoy, mañana/manana, dd/mm, dd/mm/yyyy, dd-mm-yyyy, yyyy-mm-dd.
    Retorna None si no puede parsear o la fecha ya pasó (solo aplica a dd/mm).
    """
    v = value.strip().lower()
    today = datetime.now()

    if v == "hoy":
        return today.strftime("%Y-%m-%d")
    if v in ("mañana", "manana"):
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")

    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y"):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # dd/mm o dd-mm sin año → asumir año actual; si ya pasó, año siguiente
    for sep in ("/", "-"):
        parts = v.split(sep)
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            try:
                day, month = int(parts[0]), int(parts[1])
                dt = datetime(today.year, month, day)
                if dt.date() < today.date():
                    dt = datetime(today.year + 1, month, day)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    return None


# ---------------------------------------------------------------------------
# Interpolación de variables {{var}}
# ---------------------------------------------------------------------------

def interpolate(text: str, variables: dict) -> str:
    """Reemplaza {{clave}} por el valor correspondiente en `variables`."""
    if not text:
        return text

    def _replace(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))

    return re.sub(r"\{\{(\w+)\}\}", _replace, text)


def interpolate_obj(obj: Any, variables: dict) -> Any:
    """Aplica interpolación recursivamente a dicts, listas y strings."""
    if isinstance(obj, str):
        return interpolate(obj, variables)
    if isinstance(obj, dict):
        return {k: interpolate_obj(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [interpolate_obj(i, variables) for i in obj]
    return obj


# ---------------------------------------------------------------------------
# Extracción por ruta punteada ("data.items.0")
# ---------------------------------------------------------------------------

def extract_path(data: Any, path: str) -> Any:
    """Navega `data` usando una ruta de claves separadas por punto."""
    if not path:
        return data
    for key in path.split("."):
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and key.isdigit():
            idx = int(key)
            data = data[idx] if idx < len(data) else None
        else:
            return None
        if data is None:
            return None
    return data


# ---------------------------------------------------------------------------
# Construcción de opciones WhatsApp
# ---------------------------------------------------------------------------

def _build_lista_sections(items: list, fmt: dict) -> list[dict]:
    """Construye el array de secciones para tipo_respuesta='lista'."""
    id_field    = fmt.get("id_field", "id")
    title_field = fmt.get("title_field", "nombre")
    desc_field  = fmt.get("description_field")
    section_title = fmt.get("section_title", "Opciones")
    store_selected_as = fmt.get("store_selected_as", "")

    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row_id    = str(item.get(id_field, ""))
        row_title = str(item.get(title_field, ""))[:24]  # WhatsApp limit
        row_desc  = str(item.get(desc_field, ""))[:72] if desc_field and item.get(desc_field) else ""

        if not row_id or not row_title:
            continue

        row: dict = {"id": row_id, "title": row_title}
        if row_desc:
            row["description"] = row_desc

        # Encode store_selected_as so the next rule can persist the chosen value
        if store_selected_as:
            row["_store_as"] = store_selected_as

        rows.append(row)

    if not rows:
        return []

    return [{"title": section_title, "rows": rows[:10]}]  # WA max 10 per section


def _build_boton_buttons(items: list, fmt: dict) -> list[dict]:
    """Construye botones para tipo_respuesta='boton' (máx 3)."""
    id_field    = fmt.get("id_field", "id")
    title_field = fmt.get("title_field", "nombre")
    store_selected_as = fmt.get("store_selected_as", "")

    buttons = []
    for item in items[:3]:
        if not isinstance(item, dict):
            continue
        b: dict = {
            "type": "reply",
            "reply": {
                "id": str(item.get(id_field, "")),
                "title": str(item.get(title_field, ""))[:20],
            },
        }
        if store_selected_as:
            b["_store_as"] = store_selected_as
        buttons.append(b)
    return buttons


# ---------------------------------------------------------------------------
# Ejecución de una regla api_call
# ---------------------------------------------------------------------------

def execute_api_call(numero: str, config: dict, last_user_text: str = "") -> tuple[str, str, str | None]:
    """Ejecuta una llamada API configurada en una regla.

    Retorna: (mensaje, tipo_respuesta, opciones_json)
    """
    from services import db as db_service  # late import

    # 1. Variables del chat
    chat_vars = db_service.get_all_chat_vars(numero)
    # Variables especiales siempre disponibles
    today = datetime.now()
    chat_vars.setdefault("_numero", numero)
    chat_vars.setdefault("_hoy",    today.strftime("%Y-%m-%d"))
    chat_vars.setdefault("_manana", (today + timedelta(days=1)).strftime("%Y-%m-%d"))
    if last_user_text:
        chat_vars.setdefault("_input", last_user_text)
    # Resolver alias de fechas relativas almacenados como chat_var
    _fecha_aliases = {"hoy": chat_vars["_hoy"], "manana": chat_vars["_manana"], "mañana": chat_vars["_manana"]}
    for k, v in list(chat_vars.items()):
        if isinstance(v, str) and v.lower() in _fecha_aliases:
            chat_vars[k] = _fecha_aliases[v.lower()]

    # 2. Conexión base
    conexion_id = config.get("conexion_id")
    if not conexion_id:
        raise ValueError("api_call: falta 'conexion_id' en la configuración.")

    conexion = db_service.get_conexion(int(conexion_id))
    if not conexion:
        raise ValueError(f"api_call: conexión id={conexion_id} no encontrada.")

    base_url = (conexion.get("url") or "").rstrip("/")
    auth_tipo = (conexion.get("auth_tipo") or "none").lower()
    auth_valor = (conexion.get("auth_valor") or "").strip()

    # 3. Construir URL
    raw_path = config.get("path", "")
    # Interpolar url_vars primero
    url_vars = interpolate_obj(config.get("url_vars") or {}, chat_vars)
    path = interpolate(raw_path, {**chat_vars, **url_vars})
    url = f"{base_url}{path}"

    # 4. Headers
    headers: dict = {"Content-Type": "application/json", "Accept": "application/json"}
    conn_headers_raw = (conexion.get("headers") or "").strip()
    if conn_headers_raw:
        try:
            conn_headers = json.loads(conn_headers_raw)
            if isinstance(conn_headers, dict):
                headers.update(conn_headers)
        except json.JSONDecodeError:
            pass

    if auth_tipo == "bearer" and auth_valor:
        headers["Authorization"] = f"Bearer {auth_valor}"
    elif auth_tipo == "basic" and auth_valor:
        import base64
        encoded = base64.b64encode(auth_valor.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
    elif auth_tipo == "api_key" and auth_valor:
        if ":" in auth_valor:
            hkey, hval = auth_valor.split(":", 1)
            headers[hkey.strip()] = hval.strip()
        else:
            headers["X-Api-Key"] = auth_valor

    # 5. Método y body
    method = (config.get("method") or conexion.get("metodo") or "GET").upper()
    raw_body = config.get("body")
    body = interpolate_obj(raw_body, chat_vars) if raw_body is not None else None

    # 6. Llamada HTTP
    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=body if isinstance(body, dict) else None,
            data=json.dumps(body) if isinstance(body, str) and body else None,
            timeout=20,
        )
        resp.raise_for_status()
        try:
            response_data = resp.json()
        except Exception:
            response_data = resp.text
    except requests.exceptions.HTTPError as exc:
        raise RuntimeError(f"Error HTTP {exc.response.status_code} desde la API: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Error de red al llamar la API: {exc}") from exc

    # 7. Guardar respuesta en chat_variables
    store_as = config.get("store_as", "")
    data_path = config.get("data_path", "")
    extracted = extract_path(response_data, data_path)
    if store_as:
        stored_value = json.dumps(extracted, ensure_ascii=False) if not isinstance(extracted, str) else extracted
        db_service.set_chat_var(numero, store_as, stored_value)

    # 8. Formatear respuesta para el usuario
    fmt = config.get("format") or {}
    format_tipo = (fmt.get("tipo") or "texto").lower()
    message_template = config.get("message") or ""
    message = interpolate(message_template, chat_vars)

    items: list = []
    if isinstance(extracted, list):
        items = extracted
    elif isinstance(extracted, dict):
        # Try common wrappers
        for key in ("data", "items", "results", "rows", "list"):
            if isinstance(extracted.get(key), list):
                items = extracted[key]
                break
        if not items:
            items = [extracted]

    if format_tipo == "lista" and items:
        sections = _build_lista_sections(items, fmt)
        opciones_obj = {
            "sections": sections,
            "header": fmt.get("header", "Opciones"),
            "footer": fmt.get("footer", "Selecciona una opción"),
            "button": fmt.get("button", "Ver opciones"),
        }
        return message, "lista", json.dumps(opciones_obj, ensure_ascii=False)

    if format_tipo == "boton" and items:
        buttons = _build_boton_buttons(items, fmt)
        return message, "boton", json.dumps(buttons, ensure_ascii=False)

    # texto por defecto — interpolar con datos extraídos si es un dict
    if isinstance(extracted, dict):
        merged_vars = {**chat_vars, **{k: str(v) for k, v in extracted.items()}}
        message = interpolate(message_template, merged_vars)
    elif isinstance(extracted, list) and not message:
        message = json.dumps(extracted, ensure_ascii=False, indent=2)

    return message or "✅ Operación completada.", "texto", None


# ---------------------------------------------------------------------------
# Ejecución desde dispatch_rule
# ---------------------------------------------------------------------------

def handle_api_call_rule(
    numero: str,
    respuesta_json: str,
    next_step_raw: str | None,
    current_step: str,
    platform: str | None,
    visited: set,
    selected_option_id: str | None,
    opts: str | None,
    last_user_text: str = "",
) -> None:
    """Punto de entrada llamado desde dispatch_rule para tipo='api_call'."""
    from services.whatsapp_api import enviar_mensaje  # late import
    from routes.webhook import advance_steps, _resolve_next_step, _apply_role_keyword  # type: ignore

    try:
        config = json.loads(respuesta_json or "{}")
    except json.JSONDecodeError as exc:
        logger.error("api_call: JSON inválido en respuesta de regla: %s", exc)
        enviar_mensaje(numero, "Error interno de configuración. Contáctanos.", tipo="bot")
        return

    try:
        message, tipo_resp, opciones = execute_api_call(numero, config, last_user_text=last_user_text)
    except Exception as exc:
        logger.error("api_call: error ejecutando llamada API para %s: %s", numero, exc)
        enviar_mensaje(numero, "Hubo un problema consultando la información. Intenta de nuevo.", tipo="bot")
        return

    enviar_mensaje(
        numero,
        message,
        tipo="bot",
        tipo_respuesta=tipo_resp,
        opciones=opciones,
        step=current_step,
    )

    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)


def handle_guardar_input_rule(
    numero: str,
    respuesta_json: str,
    next_step_raw: str | None,
    current_step: str,
    platform: str | None,
    visited: set,
    selected_option_id: str | None,
    opts: str | None,
    last_user_text: str = "",
) -> None:
    """Captura el input del usuario y lo guarda como chat_variable.

    El campo `respuesta` debe ser JSON con:
      store_as: nombre de la variable
      from: "option_id" | "text"  (por defecto "text")
      message: mensaje de confirmación opcional (puede usar {{store_as}})
    """
    from services import db as db_service
    from services.whatsapp_api import enviar_mensaje
    from routes.webhook import advance_steps, _resolve_next_step  # type: ignore

    try:
        config = json.loads(respuesta_json or "{}")
    except json.JSONDecodeError as exc:
        logger.error("guardar_input: JSON inválido: %s", exc)
        advance_steps(numero, _resolve_next_step(next_step_raw, selected_option_id, opts),
                      visited=visited, platform=platform)
        return

    store_as = (config.get("store_as") or "").strip()
    source = (config.get("from") or "text").lower()
    confirmation = (config.get("message") or "").strip()

    value = ""
    if source == "option_id":
        # Solo usar el ID seleccionado; NO caer en last_user_text
        # (evita guardar el texto anterior del usuario cuando el motor
        # de pasos dispara el comodín antes de que el usuario responda).
        if selected_option_id:
            value = str(selected_option_id)
    elif last_user_text:
        value = last_user_text.strip()
    elif selected_option_id:
        value = str(selected_option_id)

    # Si se esperaba capturar algo pero no hay dato aún, esperar al usuario.
    if store_as and not value:
        from routes.webhook import set_user_step  # type: ignore
        set_user_step(numero, current_step)
        return

    # Normalización de formato (actualmente solo "date")
    fmt_field = (config.get("format") or "").lower()
    if fmt_field == "date" and value:
        normalized = _parse_date_input(value)
        if normalized is None:
            error_msg = (config.get("error_message") or
                         "❌ No entendí esa fecha. Escríbela así: *dd/mm/yyyy* (ej: 25/04/2026)")
            enviar_mensaje(numero, error_msg, tipo="bot")
            from routes.webhook import set_user_step  # type: ignore
            set_user_step(numero, current_step)
            return
        value = normalized

    if store_as and value:
        db_service.set_chat_var(numero, store_as, value)
        logger.info("guardar_input: %s[%s] = %s", numero, store_as, value[:80])

    if confirmation:
        chat_vars = db_service.get_all_chat_vars(numero)
        msg = interpolate(confirmation, chat_vars)
        enviar_mensaje(numero, msg, tipo="bot", step=current_step)

    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)


# ---------------------------------------------------------------------------
# guardar_flow_inputs – extrae campos del último nfm_reply y los guarda
# ---------------------------------------------------------------------------
# Formato JSON del campo `respuesta`:
# {
#   "flow_name": "kiryapp_pasajero",   // opcional: filtra por nombre de flow
#   "field_map": {                      // clave_en_flow → nombre_chat_var
#     "document_type": "doc_tipo",
#     "document":      "doc_numero",
#     "name":          "nombre_pasajero",
#     "last_name":     "apellido_pasajero",
#     "email":         "email_pasajero",
#     "seat_id":       "silla_id"
#   },
#   "message": "✅ Datos recibidos, procesando tu reserva..."
# }

def handle_flow_inputs_rule(
    numero: str,
    respuesta_json: str,
    next_step_raw: str | None,
    current_step: str,
    platform: str | None,
    visited: set,
    selected_option_id: str | None,
    opts: str | None,
    last_user_text: str = "",
) -> None:
    """Extrae campos de la última respuesta de un WhatsApp Flow y los persiste
    como chat_variables individuales."""
    from services import db as db_service
    from services.whatsapp_api import enviar_mensaje
    from routes.webhook import advance_steps, _resolve_next_step  # type: ignore

    try:
        config = json.loads(respuesta_json or "{}")
    except json.JSONDecodeError as exc:
        logger.error("guardar_flow_inputs: JSON inválido: %s", exc)
        advance_steps(numero, _resolve_next_step(next_step_raw, selected_option_id, opts),
                      visited=visited, platform=platform)
        return

    flow_name = (config.get("flow_name") or "").strip() or None
    field_map: dict = config.get("field_map") or {}
    confirmation = (config.get("message") or "").strip()

    row = db_service.get_last_flow_response(numero, flow_name)
    if not row:
        logger.warning("guardar_flow_inputs: no hay flow_response para %s flow=%s", numero, flow_name)
        enviar_mensaje(numero, "No se recibieron datos del formulario. Intenta de nuevo.", tipo="bot")
        return

    try:
        payload = json.loads(row["response_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    saved: list[str] = []
    for flow_field, var_name in field_map.items():
        if not var_name:
            continue
        value = payload.get(flow_field)
        if value is None:
            continue
        db_service.set_chat_var(numero, var_name, str(value))
        saved.append(var_name)

    logger.info("guardar_flow_inputs: %s → guardadas %s", numero, saved)

    if confirmation:
        chat_vars = db_service.get_all_chat_vars(numero)
        chat_vars.setdefault("_numero", numero)
        msg = interpolate(confirmation, chat_vars)
        enviar_mensaje(numero, msg, tipo="bot", step=current_step)

    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)


# ---------------------------------------------------------------------------
# kiryapp_reserve – reserva tiquetes usando chat_variables y la API KiryApp
# ---------------------------------------------------------------------------
# Formato JSON del campo `respuesta`:
# {
#   "conexion_id": 1,
#   "tickets": [
#     {
#       "bearingId": "{{bearing_id}}",
#       "seatId":    "{{silla_id}}",
#       "date":      "{{fecha_viaje}}",
#       "origin":    "{{origen_id}}",
#       "destiny":   "{{destino_id}}",
#       "details":   "WHATSAPP",
#       "document":  "{{doc_numero}}",
#       "name":      "{{nombre_pasajero}}",
#       "telephone": "{{_numero}}"
#     }
#   ],
#   "store_as": "kiryapp_reserva",
#   "store_ticket_ids_as": "ticket_ids",
#   "store_client_id_as": "client_id",
#   "message": "✅ Reserva creada. Elige cómo pagar:",
#   "timeout_minutes": 5
# }

_reservation_timers: dict = {}  # numero → threading.Timer


def handle_kiryapp_reserve_rule(
    numero: str,
    respuesta_json: str,
    next_step_raw: str | None,
    current_step: str,
    platform: str | None,
    visited: set,
    selected_option_id: str | None,
    opts: str | None,
    last_user_text: str = "",
) -> None:
    """Reserva tiquetes en KiryApp y programa la liberación automática si no
    se completa el pago en el tiempo indicado."""
    import threading
    from services import db as db_service
    from services.whatsapp_api import enviar_mensaje
    from services.kiryapp import get_client_from_conexion, KiryappError
    from routes.webhook import advance_steps, _resolve_next_step  # type: ignore

    try:
        config = json.loads(respuesta_json or "{}")
    except json.JSONDecodeError as exc:
        logger.error("kiryapp_reserve: JSON inválido: %s", exc)
        enviar_mensaje(numero, "Error de configuración interna. Contáctanos.", tipo="bot")
        return

    chat_vars = db_service.get_all_chat_vars(numero)
    chat_vars["_numero"] = numero

    conexion_id = config.get("conexion_id")
    if not conexion_id:
        logger.error("kiryapp_reserve: falta conexion_id en configuración.")
        enviar_mensaje(numero, "Error de configuración interna.", tipo="bot")
        return

    raw_tickets = config.get("tickets") or []
    tickets: list[dict] = []
    for t in interpolate_obj(raw_tickets, chat_vars):
        if not isinstance(t, dict):
            continue
        for int_field in ("bearingId", "origin", "destiny"):
            if t.get(int_field) is not None:
                try:
                    t[int_field] = int(t[int_field])
                except (ValueError, TypeError):
                    pass
        tickets.append(t)

    customer_payload = {
        "customerName":         interpolate(config.get("customerName", "{{nombre_pasajero}}"), chat_vars),
        "customerLastName":     interpolate(config.get("customerLastName", "{{apellido_pasajero}}"), chat_vars),
        "customerDocument":     interpolate(config.get("customerDocument", "{{doc_numero}}"), chat_vars),
        "customerDocumentType": _to_int(interpolate(config.get("customerDocumentType", "{{doc_tipo}}"), chat_vars), 9),
        "customerEmail":        interpolate(config.get("customerEmail", "{{email_pasajero}}"), chat_vars),
        "customerPhone":        interpolate(config.get("customerPhone", "{{_numero}}"), chat_vars),
        "customerAddress":      interpolate(config.get("customerAddress", ""), chat_vars),
        "SellerName":           "WHATSAPP",
        "tickets":              tickets,
    }

    try:
        client = get_client_from_conexion(int(conexion_id))
        result = client.reserve_ticket(customer_payload)
    except KiryappError as exc:
        logger.error("kiryapp_reserve: KiryappError para %s: %s", numero, exc)
        msg = str(exc)
        if any(w in msg.lower() for w in ("silla", "seat", "ocupad", "taken")):
            enviar_mensaje(
                numero,
                "⚠️ La silla seleccionada ya fue ocupada. Por favor elige otra.",
                tipo="bot",
                step=current_step,
            )
        else:
            enviar_mensaje(numero, f"Error al reservar: {msg}", tipo="bot")
        return
    except Exception as exc:
        logger.error("kiryapp_reserve: error inesperado para %s: %s", numero, exc)
        enviar_mensaje(numero, "Hubo un problema creando la reserva. Intenta de nuevo.", tipo="bot")
        return

    store_as = (config.get("store_as") or "kiryapp_reserva").strip()
    db_service.set_chat_var(numero, store_as, json.dumps(result, ensure_ascii=False))

    if isinstance(result, dict):
        if config.get("store_client_id_as"):
            client_id = result.get("thirdClientId") or result.get("clientId") or result.get("id")
            if client_id is not None:
                db_service.set_chat_var(numero, config["store_client_id_as"], str(client_id))

        if config.get("store_ticket_ids_as"):
            tickets_resp = result.get("tickets") or result.get("data") or result.get("items") or []
            if isinstance(tickets_resp, list):
                ids = [str(t.get("id") or t.get("ticketId") or "") for t in tickets_resp if isinstance(t, dict)]
                db_service.set_chat_var(numero, config["store_ticket_ids_as"], json.dumps(ids))

    # Timer de liberación automática
    timeout_min = int(config.get("timeout_minutes") or 5)
    if timeout_min > 0:
        prev = _reservation_timers.pop(numero, None)
        if prev:
            prev.cancel()

        _snap_chat_vars = dict(chat_vars)  # snapshot para el closure

        def _liberar_reserva(
            _numero=numero, _conexion_id=conexion_id,
            _ticket_ids_var=config.get("store_ticket_ids_as", "ticket_ids"),
            _bearing_id=_snap_chat_vars.get("bearing_id", "0"),
            _seat_id=_snap_chat_vars.get("silla_id", ""),
        ):
            _reservation_timers.pop(_numero, None)
            paid_flag = db_service.get_chat_var(_numero, "_kiryapp_paid")
            if paid_flag == "1":
                return
            logger.info("kiryapp_reserve: timeout %s min, cancelando reserva para %s", timeout_min, _numero)
            try:
                cancel_client = get_client_from_conexion(int(_conexion_id))
                ticket_ids_json = db_service.get_chat_var(_numero, _ticket_ids_var)
                if ticket_ids_json:
                    tid_list = json.loads(ticket_ids_json)
                    payload = [{"id": _to_int(tid, 0), "bearingId": _to_int(_bearing_id, 0), "seatId": _seat_id}
                               for tid in tid_list if tid]
                    if payload:
                        cancel_client.cancel_ticket(payload)
            except Exception as e:
                logger.warning("kiryapp_reserve: error al cancelar reserva automática: %s", e)
            enviar_mensaje(
                _numero,
                "⏰ Tu reserva expiró por falta de pago. Puedes buscar de nuevo cuando quieras.",
                tipo="bot",
            )

        timer = threading.Timer(timeout_min * 60, _liberar_reserva)
        timer.daemon = True
        timer.start()
        _reservation_timers[numero] = timer

    msg_template = (config.get("message") or "✅ Reserva creada. Elige el método de pago:").strip()
    chat_vars_fresh = db_service.get_all_chat_vars(numero)
    chat_vars_fresh["_numero"] = numero
    msg = interpolate(msg_template, chat_vars_fresh)

    payment_methods_config = config.get("payment_methods") or [
        {"id": "14", "title": "Tarjeta débito"},
        {"id": "9",  "title": "Tarjeta crédito"},
        {"id": "2",  "title": "Efectivo"},
        {"id": "8",  "title": "Transferencia bancaria"},
    ]
    sections = [{
        "title": "Métodos de pago",
        "rows": [{"id": str(pm["id"]), "title": pm["title"]} for pm in payment_methods_config],
    }]
    opciones_obj = {
        "sections": sections,
        "header": "Método de pago",
        "footer": "Selecciona cómo pagar",
        "button": "Ver métodos",
    }
    enviar_mensaje(
        numero, msg, tipo="bot",
        tipo_respuesta="lista",
        opciones=json.dumps(opciones_obj, ensure_ascii=False),
        step=current_step,
    )

    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)


# ---------------------------------------------------------------------------
# kiryapp_pay – ejecuta el pago de una reserva ya creada
# ---------------------------------------------------------------------------
# Formato JSON del campo `respuesta`:
# {
#   "conexion_id": 1,
#   "client_id_var":      "client_id",
#   "ticket_ids_var":     "ticket_ids",
#   "bearing_id_var":     "bearing_id",
#   "seat_id_var":        "silla_id",
#   "payment_method_var": "metodo_pago_id",
#   "amount_var":         "precio_total",
#   "reference":          "WHATSAPP-{{_numero}}",
#   "message_ok":         "🎉 ¡Pago confirmado! Tu tiquete está listo.",
#   "message_error":      "❌ Error al procesar el pago. Intenta de nuevo."
# }

def handle_kiryapp_pay_rule(
    numero: str,
    respuesta_json: str,
    next_step_raw: str | None,
    current_step: str,
    platform: str | None,
    visited: set,
    selected_option_id: str | None,
    opts: str | None,
    last_user_text: str = "",
) -> None:
    """Ejecuta el pago de una reserva KiryApp y marca la conversación como pagada."""
    from services import db as db_service
    from services.whatsapp_api import enviar_mensaje
    from services.kiryapp import get_client_from_conexion, KiryappError
    from routes.webhook import advance_steps, _resolve_next_step  # type: ignore

    try:
        config = json.loads(respuesta_json or "{}")
    except json.JSONDecodeError as exc:
        logger.error("kiryapp_pay: JSON inválido: %s", exc)
        enviar_mensaje(numero, "Error de configuración interna.", tipo="bot")
        return

    chat_vars = db_service.get_all_chat_vars(numero)
    chat_vars["_numero"] = numero

    conexion_id = config.get("conexion_id")
    if not conexion_id:
        logger.error("kiryapp_pay: falta conexion_id")
        enviar_mensaje(numero, "Error de configuración interna.", tipo="bot")
        return

    client_id_var    = config.get("client_id_var",      "client_id")
    ticket_ids_var   = config.get("ticket_ids_var",     "ticket_ids")
    bearing_id_var   = config.get("bearing_id_var",     "bearing_id")
    seat_id_var      = config.get("seat_id_var",        "silla_id")
    payment_meth_var = config.get("payment_method_var", "metodo_pago_id")
    amount_var       = config.get("amount_var",         "precio_total")

    third_client_id = _to_int(chat_vars.get(client_id_var), 0)
    ticket_ids_json = chat_vars.get(ticket_ids_var) or "[]"
    bearing_id      = _to_int(chat_vars.get(bearing_id_var), 0)
    seat_id         = chat_vars.get(seat_id_var) or ""
    payment_method  = _to_int(chat_vars.get(payment_meth_var) or selected_option_id, 2)
    amount          = chat_vars.get(amount_var) or "0"
    reference       = interpolate(config.get("reference") or "WHATSAPP-{{_numero}}", chat_vars)

    try:
        ticket_ids = json.loads(ticket_ids_json)
    except (json.JSONDecodeError, TypeError):
        ticket_ids = []

    if not ticket_ids or not third_client_id:
        logger.error("kiryapp_pay: faltan ticket_ids o client_id para %s", numero)
        enviar_mensaje(numero, "No encontré la reserva activa. Inicia de nuevo.", tipo="bot")
        return

    tickets_to_pay = [
        {"id": _to_int(tid, 0), "bearingId": bearing_id, "seatId": seat_id}
        for tid in ticket_ids if tid
    ]

    try:
        client = get_client_from_conexion(int(conexion_id))
        result = client.pay_ticket({
            "thirdClientId": third_client_id,
            "ticketsToPay": tickets_to_pay,
            "payments": [{"paymentMethod": payment_method, "value": str(amount), "reference": reference}],
        })
    except KiryappError as exc:
        logger.error("kiryapp_pay: KiryappError para %s: %s", numero, exc)
        msg_error = interpolate(
            config.get("message_error") or "❌ Error al procesar el pago. Intenta de nuevo.",
            {**chat_vars, "_error": str(exc)},
        )
        enviar_mensaje(numero, msg_error, tipo="bot")
        return
    except Exception as exc:
        logger.error("kiryapp_pay: error inesperado para %s: %s", numero, exc)
        enviar_mensaje(numero, "Hubo un problema procesando el pago. Intenta de nuevo.", tipo="bot")
        return

    # Marcar como pagado y cancelar timer
    db_service.set_chat_var(numero, "_kiryapp_paid", "1")
    timer = _reservation_timers.pop(numero, None)
    if timer:
        timer.cancel()

    db_service.set_chat_var(numero, "kiryapp_pago", json.dumps(result, ensure_ascii=False))

    chat_vars_fresh = db_service.get_all_chat_vars(numero)
    chat_vars_fresh["_numero"] = numero
    msg_ok = interpolate(
        config.get("message_ok") or "🎉 ¡Pago confirmado! Tu reserva está lista.",
        chat_vars_fresh,
    )
    enviar_mensaje(numero, msg_ok, tipo="bot", step=current_step)

    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)
