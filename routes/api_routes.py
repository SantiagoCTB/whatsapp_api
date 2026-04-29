"""API pública para envío de mensajes de WhatsApp.

── API propia ────────────────────────────────────────────────────────────────
Autenticación: Authorization: Bearer <token>
Tenant:        X-Tenant-ID: <tenant_key>

  POST /api/v1/mensaje          — envía un mensaje de texto
  POST /api/v1/template         — envía una plantilla de WhatsApp

── Compatibilidad WAHA (Uptime Kuma / herramientas externas) ────────────────
Autenticación: X-Api-Key: <token>
Tenant:        campo "session" del body (= tenant_key)

  POST /api/sendText            — envía texto (formato WAHA)
  GET  /api/version             — responde versión (health-check de WAHA)

── Administración (requieren sesión admin) ───────────────────────────────────
  GET  /api/v1/token            — muestra si hay token configurado
  POST /api/v1/token/generar    — genera / rota el token del tenant actual
  POST /api/v1/token/revocar    — elimina el token del tenant actual
"""

from __future__ import annotations

import hmac
import logging
import secrets

from flask import Blueprint, jsonify, request, session

from services import tenants

logger = logging.getLogger(__name__)

api_bp = Blueprint("api_public", __name__)

_API_KEY_FIELD = "api_key"


# ── Helpers de autenticación ──────────────────────────────────────────────────

def _get_stored_token(tenant) -> str | None:
    if not tenant or not isinstance(tenant.metadata, dict):
        return None
    return tenant.metadata.get(_API_KEY_FIELD) or None


def _require_api_auth():
    """Valida el Bearer token contra el tenant activo.

    Retorna (tenant, None) si la autenticación es correcta,
    o (None, (response, status)) si debe rechazarse.
    """
    auth_header = (request.headers.get("Authorization") or "").strip()
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"ok": False, "error": "Se requiere Authorization: Bearer <token>."}), 401)

    incoming = auth_header[len("Bearer "):].strip()
    if not incoming:
        return None, (jsonify({"ok": False, "error": "Token vacío."}), 401)

    tenant = tenants.get_current_tenant()
    if not tenant:
        # Mismo mensaje que token inválido para no revelar si el tenant existe
        return None, (jsonify({"ok": False, "error": "Token inválido."}), 401)

    stored = _get_stored_token(tenant)
    if not stored:
        return None, (jsonify({"ok": False, "error": "Token inválido."}), 401)

    # Comparación en tiempo constante para evitar timing attacks
    if not hmac.compare_digest(stored.encode(), incoming.encode()):
        logger.warning(
            "API: intento con token inválido",
            extra={"tenant_key": tenant.tenant_key},
        )
        return None, (jsonify({"ok": False, "error": "Token inválido."}), 401)

    return tenant, None


def _require_admin() -> bool:
    return "user" in session and "admin" in (session.get("roles") or [])


# ── Endpoints públicos ────────────────────────────────────────────────────────

@api_bp.route("/api/v1/mensaje", methods=["POST"])
def api_enviar_mensaje():
    """Envía un mensaje de WhatsApp al número indicado.

    Body JSON:
      numero  — número en formato internacional sin '+' (ej. 573001234567)
      mensaje — texto plano a enviar
    """
    tenant, err = _require_api_auth()
    if err:
        return err

    if not request.is_json:
        return jsonify({"ok": False, "error": "Content-Type debe ser application/json."}), 415

    payload = request.get_json(silent=True) or {}
    numero = (payload.get("numero") or "").strip()
    mensaje = (payload.get("mensaje") or "").strip()

    if not numero:
        return jsonify({"ok": False, "error": "El campo 'numero' es requerido."}), 400
    if not mensaje:
        return jsonify({"ok": False, "error": "El campo 'mensaje' es requerido."}), 400
    if len(mensaje) > 4096:
        return jsonify({"ok": False, "error": "El mensaje excede el límite de 4096 caracteres."}), 400

    from services.whatsapp_api import enviar_mensaje

    ok, error_msg = enviar_mensaje(numero, mensaje, tipo="api", return_error=True)

    if not ok:
        logger.warning(
            "API: no se pudo enviar mensaje",
            extra={"tenant_key": tenant.tenant_key, "error": error_msg},
        )
        return jsonify({"ok": False, "error": error_msg or "No se pudo enviar el mensaje."}), 502

    logger.info(
        "API: mensaje enviado",
        extra={"tenant_key": tenant.tenant_key},
    )
    return jsonify({"ok": True, "mensaje": "Mensaje enviado correctamente."})


@api_bp.route("/api/v1/template", methods=["POST"])
def api_enviar_template():
    """Envía una plantilla de WhatsApp aprobada al número indicado.

    Body JSON:
      numero        — número en formato internacional sin '+' (ej. 573001234567)
      template      — nombre exacto de la plantilla (ej. "hello_world")
      idioma        — código de idioma (ej. "es_CO", "en_US"). Default: "es"
      params        — lista de strings para los {{1}}, {{2}}... del body (opcional)
    """
    tenant, err = _require_api_auth()
    if err:
        return err

    if not request.is_json:
        return jsonify({"ok": False, "error": "Content-Type debe ser application/json."}), 415

    payload = request.get_json(silent=True) or {}
    numero        = (payload.get("numero") or "").strip()
    template_name = (payload.get("template") or "").strip()
    idioma        = (payload.get("idioma") or "es").strip()
    params        = payload.get("params") or []

    if not numero:
        return jsonify({"ok": False, "error": "El campo 'numero' es requerido."}), 400
    if not template_name:
        return jsonify({"ok": False, "error": "El campo 'template' es requerido."}), 400
    if not isinstance(params, list):
        return jsonify({"ok": False, "error": "'params' debe ser una lista."}), 400

    from services import tenants as _tenants
    env = _tenants.get_tenant_env(tenant)
    token    = (env.get("META_TOKEN") or "").strip()
    phone_id = (env.get("PHONE_NUMBER_ID") or "").strip()

    if not token or not phone_id:
        return jsonify({"ok": False, "error": "El tenant no tiene META_TOKEN o PHONE_NUMBER_ID configurado."}), 503

    from services.template_builders import build_template_send_payload, TemplateValidationError
    try:
        graph_payload = build_template_send_payload({
            "to": numero,
            "template_name": template_name,
            "language_code": idioma,
            "body_parameters": [str(p) for p in params],
        })
    except TemplateValidationError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    import requests as _requests
    try:
        resp = _requests.post(
            f"https://graph.facebook.com/v22.0/{phone_id}/messages",
            params={"access_token": token},
            json=graph_payload,
            timeout=20,
        )
    except _requests.RequestException as exc:
        logger.warning("API template: error de red: %s", exc, extra={"tenant_key": tenant.tenant_key})
        return jsonify({"ok": False, "error": "No se pudo conectar con la API de Meta."}), 502

    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        error_detail = (data.get("error") or {}).get("message") or "Error de Meta API."
        logger.warning("API template: error Meta %s", resp.status_code, extra={"tenant_key": tenant.tenant_key, "detail": error_detail})
        return jsonify({"ok": False, "error": error_detail}), 502

    logger.info("API template: enviada '%s'", template_name, extra={"tenant_key": tenant.tenant_key})
    return jsonify({"ok": True, "mensaje": "Plantilla enviada correctamente.", "data": data})


# ── Endpoints de administración ───────────────────────────────────────────────

@api_bp.route("/api/v1/token", methods=["GET"])
def api_ver_token():
    """Muestra el token de API del tenant actual (solo admin)."""
    if not _require_admin():
        return jsonify({"ok": False, "error": "No autorizado."}), 403

    tenant = tenants.get_current_tenant()
    if not tenant:
        return jsonify({"ok": False, "error": "No se encontró el tenant actual."}), 400

    # Recarga para tener metadata fresca (evita caché)
    tenant = tenants.get_tenant(tenant.tenant_key, force_reload=True)
    token = _get_stored_token(tenant)
    return jsonify({"ok": True, "configurado": bool(token), "token": token})


@api_bp.route("/api/v1/token/generar", methods=["POST"])
def api_generar_token():
    """Genera (o rota) el token de API del tenant actual (solo admin)."""
    if not _require_admin():
        return jsonify({"ok": False, "error": "No autorizado."}), 403

    tenant = tenants.get_current_tenant()
    if not tenant:
        return jsonify({"ok": False, "error": "No se encontró el tenant actual."}), 400

    new_token = secrets.token_urlsafe(32)  # 256 bits de entropía
    tenants.update_tenant_metadata(tenant.tenant_key, {_API_KEY_FIELD: new_token})

    logger.info(
        "API: token generado/rotado",
        extra={"tenant_key": tenant.tenant_key},
    )
    return jsonify({"ok": True, "token": new_token})


@api_bp.route("/api/v1/token/revocar", methods=["POST"])
def api_revocar_token():
    """Elimina el token de API del tenant actual (solo admin)."""
    if not _require_admin():
        return jsonify({"ok": False, "error": "No autorizado."}), 403

    tenant = tenants.get_current_tenant()
    if not tenant:
        return jsonify({"ok": False, "error": "No se encontró el tenant actual."}), 400

    tenants.update_tenant_metadata(tenant.tenant_key, {_API_KEY_FIELD: None})

    logger.info(
        "API: token revocado",
        extra={"tenant_key": tenant.tenant_key},
    )
    return jsonify({"ok": True, "mensaje": "Token revocado correctamente."})


# ── Compatibilidad WAHA ───────────────────────────────────────────────────────

def _parse_chat_id(chat_id: str) -> str:
    """Extrae el número de teléfono de un chatId estilo WAHA.

    Ejemplos de entrada:
      573001234567          → 573001234567
      573001234567@c.us     → 573001234567
      573001234567@s.whatsapp.net → 573001234567
    """
    return (chat_id or "").split("@")[0].strip()


def _waha_auth() -> tuple:
    """Valida X-Api-Key + session (tenant).

    Retorna (tenant, None) si OK, o (None, (response, status)) si falla.
    El mismo mensaje de error para clave inválida y tenant inexistente,
    para no revelar qué falla.
    """
    api_key = (request.headers.get("X-Api-Key") or "").strip()
    if not api_key:
        return None, (jsonify({"error": "X-Api-Key requerida."}), 401)

    payload = request.get_json(silent=True) or {}
    session = (payload.get("session") or "").strip()
    if not session:
        return None, (jsonify({"error": "El campo 'session' es requerido."}), 400)

    tenant = tenants.get_tenant(session)
    if not tenant:
        return None, (jsonify({"error": "Clave API o sesión inválida."}), 401)

    stored = _get_stored_token(tenant)
    if not stored:
        return None, (jsonify({"error": "Clave API o sesión inválida."}), 401)

    if not hmac.compare_digest(stored.encode(), api_key.encode()):
        logger.warning(
            "WAHA API: intento con clave inválida",
            extra={"session": session},
        )
        return None, (jsonify({"error": "Clave API o sesión inválida."}), 401)

    return tenant, None


@api_bp.route("/api/sendText", methods=["POST"])
def waha_send_text():
    """Endpoint compatible con WAHA para herramientas como Uptime Kuma.

    Body JSON:
      chatId   — número con prefijo internacional o ID@c.us / @g.us
      text     — texto del mensaje
      session  — nombre de sesión (= tenant_key en Whapco)

    Header:
      X-Api-Key — token de API del tenant
    """
    tenant, err = _waha_auth()
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    chat_id = (payload.get("chatId") or "").strip()
    text    = (payload.get("text")   or "").strip()

    if not chat_id:
        return jsonify({"error": "El campo 'chatId' es requerido."}), 400
    if not text:
        return jsonify({"error": "El campo 'text' es requerido."}), 400
    if len(text) > 4096:
        return jsonify({"error": "El texto excede el límite de 4096 caracteres."}), 400

    numero = _parse_chat_id(chat_id)

    # Activar el contexto del tenant para que enviar_mensaje use sus credenciales
    tenants.set_current_tenant(tenant)

    from services.whatsapp_api import enviar_mensaje

    ok, error_msg = enviar_mensaje(numero, text, tipo="api", return_error=True)

    if not ok:
        logger.warning(
            "WAHA API: no se pudo enviar mensaje",
            extra={"tenant_key": tenant.tenant_key, "error": error_msg},
        )
        return jsonify({"error": error_msg or "No se pudo enviar el mensaje."}), 502

    logger.info(
        "WAHA API: mensaje enviado",
        extra={"tenant_key": tenant.tenant_key},
    )
    # WAHA devuelve el objeto del mensaje; devolvemos un subset compatible
    return jsonify({"id": None, "timestamp": None, "chatId": chat_id})


@api_bp.route("/api/version", methods=["GET"])
def waha_version():
    """Health-check compatible con WAHA. Uptime Kuma lo usa para verificar la conexión."""
    return jsonify({"version": "whapco", "engine": "WEBJS"})
