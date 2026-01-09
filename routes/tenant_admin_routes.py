from __future__ import annotations

import json
import logging
from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from config import Config
from services import tenants


tenant_admin_bp = Blueprint("tenant_admin", __name__, url_prefix="/admin/tenants")
logger = logging.getLogger(__name__)


def _sync_page_selection_metadata(tenant: tenants.TenantInfo) -> None:
    tenant_env = tenants.get_tenant_env(tenant)
    metadata = dict(tenant.metadata or {})
    raw_selection = metadata.get("page_selection") if isinstance(metadata.get("page_selection"), dict) else {}

    normalized = {}
    for platform in ("messenger", "instagram"):
        page_id = (tenant_env.get(f"{platform.upper()}_PAGE_ID") or "").strip()
        if not page_id:
            legacy_platform = (tenant_env.get("PLATFORM") or "").strip().lower()
            if legacy_platform == platform:
                page_id = (tenant_env.get("PAGE_ID") or "").strip()

        if not page_id:
            continue

        page_name = None
        legacy_entry = raw_selection.get(platform) if isinstance(raw_selection, dict) else None
        if isinstance(legacy_entry, dict) and legacy_entry.get("page_id") == page_id:
            page_name = legacy_entry.get("page_name")
        elif raw_selection.get("page_id") == page_id:
            page_name = raw_selection.get("page_name")

        normalized[platform] = {"page_id": page_id, "page_name": page_name}

    if not normalized:
        if "page_selection" in metadata:
            metadata.pop("page_selection", None)
            tenants.update_tenant_metadata(tenant.tenant_key, metadata)
        return

    metadata["page_selection"] = normalized
    tenants.update_tenant_metadata(tenant.tenant_key, metadata)


@tenant_admin_bp.before_request
def require_superadmin():
    roles = set(session.get("roles", []) or [])
    if "superadmin" not in roles:
        abort(403)


@tenant_admin_bp.route("/", methods=["GET"])
def dashboard():
    all_tenants = tenants.list_tenants(force_reload=True)
    selected_key = request.args.get("tenant")
    selected_tenant = tenants.get_tenant(selected_key) if selected_key else None

    tenant_roles = tenants.get_tenant_roles(selected_tenant) if selected_tenant else []
    tenant_users = tenants.list_tenant_users(selected_tenant) if selected_tenant else []
    tenant_env = tenants.get_tenant_env(selected_tenant) if selected_tenant else {}

    message = request.args.get("msg")
    error = request.args.get("error")

    return render_template(
        "admin_tenants.html",
        tenants=all_tenants,
        selected_tenant=selected_tenant,
        tenant_roles=tenant_roles,
        tenant_users=tenant_users,
        tenant_env=tenant_env,
        signup_config_code=Config.SIGNUP_FACEBOOK,
        facebook_app_id=Config.FACEBOOK_APP_ID,
        message=message,
        error=error,
    )


@tenant_admin_bp.route("/", methods=["POST"])
def create_or_update_tenant():
    tenant_key = (request.form.get("tenant_key") or "").strip()
    name = (request.form.get("name") or tenant_key).strip()
    db_name = (request.form.get("db_name") or "").strip()
    db_host = (request.form.get("db_host") or "").strip()
    db_port_raw = request.form.get("db_port")
    db_port = Config.DB_PORT
    db_user = (request.form.get("db_user") or Config.DB_USER or "").strip()
    db_password = (request.form.get("db_password") or Config.DB_PASSWORD or "").strip()
    metadata_raw = request.form.get("metadata") or ""
    # Siempre intentamos crear/actualizar el esquema aislado, incluso si el
    # checkbox no viene en la petición (p.ej. clientes que no envían el campo).
    ensure_schema = request.form.get("ensure_schema", "1") == "1"

    try:
        db_port = int(db_port_raw) if db_port_raw else Config.DB_PORT or 3306
    except ValueError:
        return redirect(
            url_for(
                "tenant_admin.dashboard",
                error="El puerto de la base de datos no es válido.",
            )
        )

    if not tenant_key or not db_name or not db_host or not db_user or not db_password:
        return redirect(
            url_for(
                "tenant_admin.dashboard",
                error="Faltan campos obligatorios para registrar la empresa.",
            )
        )

    env_db_name = (Config.DB_NAME or "").strip()
    if env_db_name and db_name.lower() == env_db_name.lower():
        return redirect(
            url_for(
                "tenant_admin.dashboard",
                error="La base de datos del tenant debe ser distinta a la configurada en el servicio (DB_NAME).",
            )
        )

    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except json.JSONDecodeError:
        return redirect(
            url_for(
                "tenant_admin.dashboard",
                error="El metadata debe ser JSON válido.",
            )
        )

    tenant_obj = tenants.TenantInfo(
        tenant_key=tenant_key,
        name=name or tenant_key,
        db_name=db_name,
        db_host=db_host,
        db_port=db_port,
        db_user=db_user,
        db_password=db_password,
        metadata=metadata,
    )

    created = tenants.register_tenant(tenant_obj, ensure_schema=ensure_schema)
    if created:
        msg = "Empresa registrada correctamente."
        return redirect(url_for("tenant_admin.dashboard", tenant=tenant_key, msg=msg))

    return redirect(
        url_for(
            "tenant_admin.dashboard",
            error="No se pudo registrar la empresa.",
        )
    )


@tenant_admin_bp.route("/<tenant_key>/users", methods=["POST"])
def create_tenant_user(tenant_key: str):
    tenant = tenants.get_tenant(tenant_key)
    if not tenant:
        abort(404)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    roles = request.form.getlist("roles")

    try:
        tenants.create_or_update_tenant_user(tenant, username, password, roles)
    except ValueError as exc:
        return redirect(
            url_for(
                "tenant_admin.dashboard",
                tenant=tenant_key,
                error=str(exc),
            )
        )

    return redirect(
        url_for(
            "tenant_admin.dashboard",
            tenant=tenant_key,
            msg="Usuario guardado y roles sincronizados.",
        )
    )


@tenant_admin_bp.route("/<tenant_key>/env", methods=["POST"])
def update_tenant_env(tenant_key: str):
    tenant = tenants.get_tenant(tenant_key)
    if not tenant:
        abort(404)

    env_payload = {key: request.form.get(key) for key in tenants.TENANT_ENV_KEYS}
    tenants.update_tenant_env(tenant_key, env_payload)
    updated_tenant = tenants.get_tenant(tenant_key, force_reload=True)
    if updated_tenant:
        _sync_page_selection_metadata(updated_tenant)

    return redirect(
        url_for(
            "tenant_admin.dashboard",
            tenant=tenant_key,
            msg="Variables de entorno actualizadas.",
        )
    )


@tenant_admin_bp.route("/<tenant_key>/signup", methods=["POST"])
def save_tenant_signup(tenant_key: str):
    tenant = tenants.get_tenant(tenant_key)
    if not tenant:
        abort(404)

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        logger.exception("Admin: error al parsear el payload JSON de signup", extra={"tenant_key": tenant_key})
        return {"ok": False, "error": "Payload inválido"}, 400

    logger.info(
        "Admin: procesando signup embebido",
        extra={"tenant_key": tenant_key, "payload_keys": sorted(list(payload.keys()))},
    )

    current_env = tenants.get_tenant_env(tenant)
    env_updates = {key: current_env.get(key) for key in tenants.TENANT_ENV_KEYS}
    env_updates.update(
        {
            "META_TOKEN": payload.get("access_token") or payload.get("token"),
            "LONG_LIVED_TOKEN": payload.get("access_token")
            or payload.get("long_lived_token"),
            "PHONE_NUMBER_ID": payload.get("phone_number_id")
            or payload.get("phone_id"),
            "WABA_ID": payload.get("waba_id"),
            "BUSINESS_ID": payload.get("business_id")
            or payload.get("business_manager_id"),
        }
    )

    logger.info(
        "Admin: actualizando entorno con datos de signup",
        extra={
            "tenant_key": tenant_key,
            "has_meta_token": bool(env_updates.get("META_TOKEN")),
            "has_long_lived": bool(env_updates.get("LONG_LIVED_TOKEN")),
            "has_phone_number_id": bool(env_updates.get("PHONE_NUMBER_ID")),
            "has_waba_id": bool(env_updates.get("WABA_ID")),
            "has_business_id": bool(env_updates.get("BUSINESS_ID")),
        },
    )

    business_info = payload.get("business") or payload.get("business_info")
    metadata_updates = {}
    if isinstance(business_info, dict) and business_info:
        metadata_updates["whatsapp_business"] = business_info

    if metadata_updates:
        logger.info(
            "Admin: guardando metadata de negocio desde signup",
            extra={
                "tenant_key": tenant_key,
                "metadata_fields": sorted(list(metadata_updates.keys())),
            },
        )
    else:
        logger.info(
            "Admin: payload de signup sin metadata de negocio",
            extra={"tenant_key": tenant_key},
        )

    tenants.update_tenant_env(tenant_key, env_updates)
    if metadata_updates:
        tenants.update_tenant_metadata(tenant_key, metadata_updates)

    return {
        "ok": True,
        "message": "Credenciales de WhatsApp actualizadas.",
        "env": tenants.get_tenant_env(tenants.get_tenant(tenant_key)),
    }


@tenant_admin_bp.route("/<tenant_key>/delete", methods=["POST"])
def delete_tenant(tenant_key: str):
    if Config.DEFAULT_TENANT and tenant_key == Config.DEFAULT_TENANT:
        return redirect(
            url_for(
                "tenant_admin.dashboard",
                error="No se puede eliminar el tenant por defecto configurado.",
            )
        )

    try:
        tenants.delete_tenant(tenant_key)
    except tenants.TenantNotFoundError as exc:
        return redirect(url_for("tenant_admin.dashboard", error=str(exc)))
    except ValueError as exc:
        return redirect(url_for("tenant_admin.dashboard", error=str(exc)))

    return redirect(
        url_for(
            "tenant_admin.dashboard",
            msg="Empresa eliminada correctamente.",
        )
    )
