import json
import logging

import requests
from flask import Blueprint, jsonify, render_template, request, session

from services import db as db_service

logger = logging.getLogger(__name__)
conexiones_bp = Blueprint("conexiones", __name__)

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_ALLOWED_AUTH_TIPOS = {"none", "bearer", "basic", "api_key"}


def _require_login():
    return "user" in session


def _validate_payload(data: dict) -> tuple[dict | None, str]:
    """Validates and normalises a conexion payload. Returns (clean_data, error)."""
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return None, "El campo 'nombre' es obligatorio."

    url = (data.get("url") or "").strip()
    if not url:
        return None, "El campo 'url' es obligatorio."

    metodo = (data.get("metodo") or "GET").strip().upper()
    if metodo not in _ALLOWED_METHODS:
        return None, f"Método HTTP inválido. Valores permitidos: {', '.join(sorted(_ALLOWED_METHODS))}."

    auth_tipo = (data.get("auth_tipo") or "none").strip().lower()
    if auth_tipo not in _ALLOWED_AUTH_TIPOS:
        return None, f"Tipo de autenticación inválido. Valores permitidos: {', '.join(sorted(_ALLOWED_AUTH_TIPOS))}."

    # headers: accept dict or raw JSON string, store as JSON string
    raw_headers = data.get("headers") or {}
    if isinstance(raw_headers, dict):
        headers_str = json.dumps(raw_headers, ensure_ascii=False)
    else:
        headers_str = str(raw_headers).strip()
        if headers_str:
            try:
                json.loads(headers_str)
            except json.JSONDecodeError:
                return None, "El campo 'headers' debe ser un JSON válido."

    raw_body = data.get("body_template") or ""
    body_str = str(raw_body).strip()

    return {
        "nombre": nombre,
        "url": url,
        "metodo": metodo,
        "descripcion": (data.get("descripcion") or "").strip(),
        "headers": headers_str,
        "body_template": body_str,
        "auth_tipo": auth_tipo,
        "auth_valor": (data.get("auth_valor") or "").strip(),
    }, ""


# ── Page ─────────────────────────────────────────────────────────────────────

@conexiones_bp.route("/conexiones", methods=["GET"])
def conexiones_page():
    if not _require_login():
        return jsonify({"ok": False, "error": "No autorizado"}), 403
    return render_template("conexiones.html")


# ── API: list ─────────────────────────────────────────────────────────────────

@conexiones_bp.route("/api/conexiones", methods=["GET"])
def list_conexiones():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403
    rows = db_service.get_all_conexiones()
    # Mask auth_valor for security
    for row in rows:
        if row.get("auth_valor"):
            row["auth_valor_masked"] = True
            row["auth_valor"] = ""
        else:
            row["auth_valor_masked"] = False
        # Parse headers JSON for display
        if row.get("headers"):
            try:
                row["headers"] = json.loads(row["headers"])
            except (json.JSONDecodeError, TypeError):
                pass
    return {"ok": True, "data": rows}


# ── API: create ───────────────────────────────────────────────────────────────

@conexiones_bp.route("/api/conexiones", methods=["POST"])
def create_conexion():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    data = request.get_json(silent=True) or {}
    clean, error = _validate_payload(data)
    if error:
        return {"ok": False, "error": error}, 400

    new_id = db_service.create_conexion(**clean)
    return {"ok": True, "id": new_id, "message": "Conexión creada correctamente."}, 201


# ── API: get single ───────────────────────────────────────────────────────────

@conexiones_bp.route("/api/conexiones/<int:conexion_id>", methods=["GET"])
def get_conexion(conexion_id: int):
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    row = db_service.get_conexion(conexion_id)
    if not row:
        return {"ok": False, "error": "Conexión no encontrada."}, 404

    if row.get("auth_valor"):
        row["auth_valor_masked"] = True
        row["auth_valor"] = ""
    else:
        row["auth_valor_masked"] = False
    if row.get("headers"):
        try:
            row["headers"] = json.loads(row["headers"])
        except (json.JSONDecodeError, TypeError):
            pass
    return {"ok": True, "data": row}


# ── API: update ───────────────────────────────────────────────────────────────

@conexiones_bp.route("/api/conexiones/<int:conexion_id>", methods=["PUT"])
def update_conexion(conexion_id: int):
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    existing = db_service.get_conexion(conexion_id)
    if not existing:
        return {"ok": False, "error": "Conexión no encontrada."}, 404

    data = request.get_json(silent=True) or {}

    # If auth_valor is omitted or empty AND masked flag is set on existing,
    # keep original value (caller didn't change the secret).
    if not data.get("auth_valor") and existing.get("auth_valor"):
        data["auth_valor"] = existing["auth_valor"]

    clean, error = _validate_payload(data)
    if error:
        return {"ok": False, "error": error}, 400

    db_service.update_conexion(conexion_id, **clean)
    return {"ok": True, "message": "Conexión actualizada correctamente."}


# ── API: delete ───────────────────────────────────────────────────────────────

@conexiones_bp.route("/api/conexiones/<int:conexion_id>", methods=["DELETE"])
def delete_conexion(conexion_id: int):
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    deleted = db_service.delete_conexion(conexion_id)
    if not deleted:
        return {"ok": False, "error": "Conexión no encontrada."}, 404
    return {"ok": True, "message": "Conexión eliminada correctamente."}


# ── API: test / probar ────────────────────────────────────────────────────────

@conexiones_bp.route("/api/conexiones/<int:conexion_id>/probar", methods=["POST"])
def probar_conexion(conexion_id: int):
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    row = db_service.get_conexion(conexion_id)
    if not row:
        return {"ok": False, "error": "Conexión no encontrada."}, 404

    # Optional overrides from request body
    req_data = request.get_json(silent=True) or {}
    test_path = (req_data.get("path") or "").strip()
    test_method = (req_data.get("method") or "").strip().upper()
    test_body_raw = req_data.get("body")  # None means "use body_template"

    # Construct final URL: base_url + optional path
    base_url = (row.get("url") or "").rstrip("/")
    url = f"{base_url}{test_path}" if test_path else base_url

    # Build headers dict
    headers: dict = {"Accept": "application/json"}
    if row.get("headers"):
        try:
            parsed = json.loads(row["headers"])
            if isinstance(parsed, dict):
                headers.update(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    # Apply authentication
    auth_tipo = (row.get("auth_tipo") or "none").lower()
    auth_valor = (row.get("auth_valor") or "").strip()
    if auth_tipo == "bearer" and auth_valor:
        headers["Authorization"] = f"Bearer {auth_valor}"
    elif auth_tipo == "api_key" and auth_valor:
        if ":" in auth_valor:
            key, val = auth_valor.split(":", 1)
            headers[key.strip()] = val.strip()
        else:
            headers["X-Api-Key"] = auth_valor
    elif auth_tipo == "basic" and auth_valor:
        import base64
        encoded = base64.b64encode(auth_valor.encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"

    # Resolve body: explicit override > body_template from DB
    body_json = None
    if test_body_raw is not None:
        body_json = test_body_raw
    else:
        body_str = (row.get("body_template") or "").strip()
        if body_str:
            try:
                body_json = json.loads(body_str)
            except json.JSONDecodeError:
                body_json = body_str

    method = test_method or (row.get("metodo") or "GET").upper()

    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=body_json if isinstance(body_json, (dict, list)) else None,
            data=body_json if isinstance(body_json, str) else None,
            timeout=15,
        )
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text

        return {
            "ok": True,
            "status_code": resp.status_code,
            "url_tested": url,
            "response": resp_body,
        }
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "La solicitud superó el tiempo de espera (15 s)."}, 504
    except requests.exceptions.ConnectionError as exc:
        return {"ok": False, "error": f"No se pudo conectar: {exc}"}, 502
    except Exception as exc:
        logger.warning("Error probando conexión %s: %s", conexion_id, exc)
        return {"ok": False, "error": str(exc)}, 500
