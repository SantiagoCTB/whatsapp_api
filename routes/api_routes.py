"""API pública para envío de mensajes de WhatsApp.

Autenticación: Authorization: Bearer <token>
Tenant:        X-Tenant-ID: <tenant_key>

Endpoints públicos (requieren token de API):
  POST /api/v1/mensaje          — envía un mensaje de texto

Endpoints de administración (requieren sesión admin):
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
