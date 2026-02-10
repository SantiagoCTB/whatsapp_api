import logging

import requests
from flask import Blueprint, jsonify, render_template, request, session

from services import tenants
from services.template_builders import (
    TemplateValidationError,
    build_template_create_payload,
    build_template_send_payload,
)
from services.whatsapp_api import API_VERSION


logger = logging.getLogger(__name__)
plantillas_bp = Blueprint("plantillas", __name__)
GRAPH_BASE_URL = f"https://graph.facebook.com/{API_VERSION}"


def _require_login():
    return "user" in session


def _tenant_whatsapp_env() -> tuple[str, str, str]:
    env = tenants.get_current_tenant_env() or {}
    token = str(env.get("META_TOKEN") or "").strip()
    waba_id = str(env.get("WABA_ID") or "").strip()
    phone_id = str(env.get("PHONE_NUMBER_ID") or "").strip()

    missing = []
    if not token:
        missing.append("META_TOKEN")
    if not waba_id:
        missing.append("WABA_ID")
    if not phone_id:
        missing.append("PHONE_NUMBER_ID")
    if missing:
        raise RuntimeError(
            "Faltan credenciales de WhatsApp para el tenant actual: " + ", ".join(missing)
        )

    return token, waba_id, phone_id


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


@plantillas_bp.route("/api/plantillas", methods=["GET"])
def list_templates():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    search = (request.args.get("search") or "").strip()
    category = (request.args.get("category") or "").strip().upper()

    try:
        token, waba_id, _ = _tenant_whatsapp_env()
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
        logger.warning(
            "Error listando plantillas",
            extra={"status": response.status_code, "payload": payload},
        )
        return {"ok": False, "error": "No se pudieron listar las plantillas.", "details": payload}, 400

    return {"ok": True, "data": payload.get("data", []), "paging": payload.get("paging")}


@plantillas_bp.route("/api/plantillas", methods=["POST"])
def create_template():
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    data = request.get_json(silent=True) or {}
    try:
        token, waba_id, _ = _tenant_whatsapp_env()
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
        logger.warning(
            "Error creando plantilla",
            extra={"status": response.status_code, "payload": payload, "template": create_payload.get("name")},
        )
        return {"ok": False, "error": "No se pudo crear la plantilla.", "details": payload}, 400

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
        logger.warning(
            "Error enviando plantilla",
            extra={"status": response.status_code, "payload": payload, "template": send_payload.get("template", {}).get("name")},
        )
        return {"ok": False, "error": "No se pudo enviar la plantilla.", "details": payload}, 400

    return {"ok": True, "message": "Plantilla enviada correctamente.", "data": payload}


@plantillas_bp.route("/api/plantillas/<string:template_name>", methods=["DELETE"])
def delete_template(template_name: str):
    if not _require_login():
        return {"ok": False, "error": "No autorizado"}, 403

    try:
        token, waba_id, _ = _tenant_whatsapp_env()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}, 400

    response = requests.delete(
        f"{GRAPH_BASE_URL}/{waba_id}/message_templates",
        params={"name": template_name, "access_token": token},
        timeout=20,
    )
    payload = response.json() if response.content else {}

    if response.status_code >= 400:
        logger.warning(
            "Error eliminando plantilla",
            extra={"status": response.status_code, "payload": payload, "template": template_name},
        )
        return {"ok": False, "error": "No se pudo eliminar la plantilla.", "details": payload}, 400

    return {"ok": True, "message": "Plantilla eliminada.", "data": payload}
