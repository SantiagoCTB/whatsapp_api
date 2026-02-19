import logging

import requests
from flask import Blueprint, jsonify, render_template, request, session

from services import tenants
from services.template_builders import (
    TemplateValidationError,
    build_flow_send_payload,
    build_template_create_payload,
    build_template_send_payload,
)
from services.whatsapp_api import API_VERSION


logger = logging.getLogger(__name__)
plantillas_bp = Blueprint("plantillas", __name__)
GRAPH_BASE_URL = f"https://graph.facebook.com/{API_VERSION}"


def _require_login():
    return "user" in session


def _extract_graph_error_message(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if message:
            return str(message)
    return None


def _tenant_whatsapp_env() -> tuple[str, str, str]:
    env = tenants.get_current_tenant_env() or {}
    token = str(env.get("META_TOKEN") or "").strip()
    waba_id = str(env.get("WABA_ID") or "").strip()
    phone_id = str(env.get("PHONE_NUMBER_ID") or "").strip()

    missing = []
    if not token:
        missing.append("META_TOKEN")
    if not phone_id:
        missing.append("PHONE_NUMBER_ID")
    if missing:
        raise RuntimeError(
            "Faltan credenciales de WhatsApp para el tenant actual: " + ", ".join(missing)
        )

    return token, waba_id, phone_id


def _resolve_waba_id(token: str, waba_id: str, phone_id: str) -> str:
    if waba_id:
        return waba_id

    response = requests.get(
        f"{GRAPH_BASE_URL}/{phone_id}",
        params={"fields": "whatsapp_business_account", "access_token": token},
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        details = _extract_graph_error_message(payload)
        raise RuntimeError(
            "No se pudo resolver WABA_ID desde PHONE_NUMBER_ID. "
            + (details or "Verifica que el token tenga permisos de WhatsApp Management.")
        )

    resolved = str((payload.get("whatsapp_business_account") or {}).get("id") or "").strip()
    if not resolved:
        raise RuntimeError(
            "No se encontró whatsapp_business_account para el PHONE_NUMBER_ID configurado."
        )
    return resolved


@plantillas_bp.route("/plantillas", methods=["GET"])
def plantillas_page():
    if not _require_login():
        return jsonify({"ok": False, "error": "No autorizado"}), 403
    return render_template("plantillas.html")


@plantillas_bp.route("/api/plantillas/preview-create", methods=["POST"])
def preview_create_template_payload():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    payload = request.get_json(silent=True) or {}
    try:
        create_payload = build_template_create_payload(payload)
    except TemplateValidationError as exc:
        return {"ok": False, "error": str(exc)}, 400

    return {"ok": True, "payload": create_payload}


@plantillas_bp.route("/api/plantillas/preview-send", methods=["POST"])
def preview_send_template_payload():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    payload = request.get_json(silent=True) or {}
    try:
        send_payload = build_template_send_payload(payload)
    except TemplateValidationError as exc:
        return {"ok": False, "error": str(exc)}, 400

    return {"ok": True, "payload": send_payload}




@plantillas_bp.route("/api/plantillas/preview-send-flow", methods=["POST"])
def preview_send_flow_payload():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    payload = request.get_json(silent=True) or {}
    try:
        send_payload = build_flow_send_payload(payload)
    except TemplateValidationError as exc:
        return {"ok": False, "error": str(exc)}, 400

    return {"ok": True, "payload": send_payload}

@plantillas_bp.route("/api/plantillas/credentials", methods=["GET"])
def template_credentials_status():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    env = tenants.get_current_tenant_env() or {}
    token = str(env.get("META_TOKEN") or "").strip()
    waba_id = str(env.get("WABA_ID") or "").strip()
    phone_id = str(env.get("PHONE_NUMBER_ID") or "").strip()

    missing = []
    if not token:
        missing.append("META_TOKEN")
    if not phone_id:
        missing.append("PHONE_NUMBER_ID")

    resolved_waba_id = ""
    warnings = []
    if not missing:
        try:
            resolved_waba_id = _resolve_waba_id(token, waba_id, phone_id)
            if not waba_id:
                warnings.append(
                    "WABA_ID no está configurado. Se resolvió automáticamente usando PHONE_NUMBER_ID."
                )
        except RuntimeError as exc:
            warnings.append(str(exc))

    return {
        "ok": True,
        "ready": len(missing) == 0 and bool(resolved_waba_id),
        "missing": missing,
        "warnings": warnings,
        "configured": {
            "META_TOKEN": bool(token),
            "PHONE_NUMBER_ID": bool(phone_id),
            "WABA_ID": bool(waba_id),
        },
    }


@plantillas_bp.route("/api/plantillas", methods=["GET"])
def list_templates():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    search = (request.args.get("search") or "").strip()
    category = (request.args.get("category") or "").strip().upper()

    try:
        token, waba_id, phone_id = _tenant_whatsapp_env()
        waba_id = _resolve_waba_id(token, waba_id, phone_id)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}, 400

    params = {
        "limit": 100,
        "access_token": token,
    }
    if search:
        params["name"] = search
    if category in {"UTILITY", "MARKETING", "AUTHENTICATION"}:
        params["category"] = category

    response = requests.get(
        f"{GRAPH_BASE_URL}/{waba_id}/message_templates",
        params=params,
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        details = _extract_graph_error_message(payload)
        logger.warning(
            "Error listando plantillas",
            extra={"status": response.status_code, "payload": payload},
        )
        message = "No se pudieron listar las plantillas."
        if details:
            message = f"{message} {details}"
        return {"ok": False, "error": message, "details": payload}, 400

    return {"ok": True, "data": payload.get("data", []), "paging": payload.get("paging")}


@plantillas_bp.route("/api/plantillas", methods=["POST"])
def create_template():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    data = request.get_json(silent=True) or {}
    try:
        token, waba_id, phone_id = _tenant_whatsapp_env()
        waba_id = _resolve_waba_id(token, waba_id, phone_id)
        create_payload = build_template_create_payload(data)
    except (RuntimeError, TemplateValidationError) as exc:
        return {"ok": False, "error": str(exc)}, 400

    response = requests.post(
        f"{GRAPH_BASE_URL}/{waba_id}/message_templates",
        params={"access_token": token},
        json=create_payload,
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        details = _extract_graph_error_message(payload)
        logger.warning(
            "Error creando plantilla",
            extra={"status": response.status_code, "payload": payload, "template": create_payload.get("name")},
        )
        message = "No se pudo crear la plantilla."
        if details:
            message = f"{message} {details}"
        return {"ok": False, "error": message, "details": payload}, 400

    return {"ok": True, "message": "Plantilla creada y enviada a aprobación.", "data": payload}


@plantillas_bp.route("/api/plantillas/send", methods=["POST"])
def send_template():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    data = request.get_json(silent=True) or {}
    try:
        token, _, phone_id = _tenant_whatsapp_env()
        send_payload = build_template_send_payload(data)
    except (RuntimeError, TemplateValidationError) as exc:
        return {"ok": False, "error": str(exc)}, 400

    response = requests.post(
        f"{GRAPH_BASE_URL}/{phone_id}/messages",
        params={"access_token": token},
        json=send_payload,
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        details = _extract_graph_error_message(payload)
        logger.warning(
            "Error enviando plantilla",
            extra={"status": response.status_code, "payload": payload, "template": send_payload.get("template", {}).get("name")},
        )
        message = "No se pudo enviar la plantilla."
        if details:
            message = f"{message} {details}"
        return {"ok": False, "error": message, "details": payload}, 400

    return {"ok": True, "message": "Plantilla enviada correctamente.", "data": payload}




@plantillas_bp.route("/api/plantillas/send-flow", methods=["POST"])
def send_flow():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    data = request.get_json(silent=True) or {}
    try:
        token, _, phone_id = _tenant_whatsapp_env()
        send_payload = build_flow_send_payload(data)
    except (RuntimeError, TemplateValidationError) as exc:
        return {"ok": False, "error": str(exc)}, 400

    response = requests.post(
        f"{GRAPH_BASE_URL}/{phone_id}/messages",
        params={"access_token": token},
        json=send_payload,
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        details = _extract_graph_error_message(payload)
        logger.warning(
            "Error enviando flow",
            extra={"status": response.status_code, "payload": payload, "flow": send_payload.get("interactive", {}).get("action", {}).get("parameters", {})},
        )
        message = "No se pudo enviar el flow."
        if details:
            message = f"{message} {details}"
        return {"ok": False, "error": message, "details": payload}, 400

    return {"ok": True, "message": "Flow enviado correctamente.", "data": payload}

@plantillas_bp.route("/api/plantillas/<string:template_name>", methods=["DELETE"])
def delete_template(template_name: str):
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    try:
        token, waba_id, phone_id = _tenant_whatsapp_env()
        waba_id = _resolve_waba_id(token, waba_id, phone_id)
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}, 400

    response = requests.delete(
        f"{GRAPH_BASE_URL}/{waba_id}/message_templates",
        params={"name": template_name, "access_token": token},
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        details = _extract_graph_error_message(payload)
        logger.warning(
            "Error eliminando plantilla",
            extra={"status": response.status_code, "payload": payload, "template": template_name},
        )
        message = "No se pudo eliminar la plantilla."
        if details:
            message = f"{message} {details}"
        return {"ok": False, "error": message, "details": payload}, 400

    return {"ok": True, "message": "Plantilla eliminada.", "data": payload}
