from __future__ import annotations

import json
from flask import Blueprint, abort, redirect, render_template, request, session, url_for

from config import Config
from services import tenants


tenant_admin_bp = Blueprint("tenant_admin", __name__, url_prefix="/admin/tenants")


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

    return redirect(
        url_for(
            "tenant_admin.dashboard",
            tenant=tenant_key,
            msg="Variables de entorno actualizadas.",
        )
    )
