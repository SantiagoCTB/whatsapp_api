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
from typing import Any

import requests

logger = logging.getLogger(__name__)


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
    # Agregar texto del usuario como variable especial
    if last_user_text:
        chat_vars.setdefault("_input", last_user_text)

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
    if source == "option_id" and selected_option_id:
        value = str(selected_option_id)
    elif last_user_text:
        value = last_user_text.strip()
    elif selected_option_id:
        value = str(selected_option_id)

    if store_as and value:
        db_service.set_chat_var(numero, store_as, value)
        logger.info("guardar_input: %s[%s] = %s", numero, store_as, value[:80])

    if confirmation:
        chat_vars = db_service.get_all_chat_vars(numero)
        msg = interpolate(confirmation, chat_vars)
        enviar_mensaje(numero, msg, tipo="bot", step=current_step)

    next_step = _resolve_next_step(next_step_raw, selected_option_id, opts)
    advance_steps(numero, next_step, visited=visited, platform=platform)
