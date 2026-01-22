"""Tenant management helpers for multi-tenant isolation."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import posixpath
from dataclasses import dataclass
from typing import Dict, List, Mapping
from flask import Request
from werkzeug.security import generate_password_hash

from config import Config
from services import db


logger = logging.getLogger(__name__)

@dataclass
class TenantInfo:
    tenant_key: str
    name: str
    db_name: str
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    metadata: dict

    def as_db_settings(self) -> db.DatabaseSettings:
        return db.DatabaseSettings(
            host=self.db_host,
            port=self.db_port,
            user=self.db_user,
            password=self.db_password,
            name=self.db_name,
        )


_tenant_cache: Dict[str, TenantInfo] = {}
_CURRENT_TENANT = contextvars.ContextVar("current_tenant", default=None)
_CURRENT_TENANT_ENV = contextvars.ContextVar("current_tenant_env", default=None)

TENANT_ENV_KEYS = {
    "META_TOKEN",
    "MESSENGER_TOKEN",
    "INSTAGRAM_TOKEN",
    "INSTAGRAM_ACCOUNT_ID",
    "INSTAGRAM_PAGE_ID",
    "PAGE_ID",
    "PAGE_ACCESS_TOKEN",
    "PLATFORM",
    "MESSENGER_PAGE_ID",
    "MESSENGER_PAGE_ACCESS_TOKEN",
    "PHONE_NUMBER_ID",
    "LONG_LIVED_TOKEN",
    "WABA_ID",
    "BUSINESS_ID",
    "SECRET_KEY",
    "VERIFY_TOKEN",
    "MEDIA_ROOT",
    "SESSION_TIMEOUT",
    "SESSION_TIMEOUT_MESSAGE",
    "IA_API_TOKEN",
    "IA_MODEL",
    "IA_SYSTEM_MESSAGE",
    "IA_HISTORY_LIMIT",
    "IA_CATALOG_USE_OPENAI",
    "IA_CATALOG_MAX_FILE_MB",
    "IA_CATALOG_REQUEST_DELAY_SECONDS",
}


class TenantNotFoundError(Exception):
    """Raised when a tenant cannot be resolved."""


class TenantResolutionError(Exception):
    """Raised when tenant resolution fails due to bad input."""


def _deserialize_metadata(raw):
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _default_tenant_env(*, include_legacy_credentials: bool = False) -> dict:
    env = {
        "META_TOKEN": None,
        "MESSENGER_TOKEN": None,
        "INSTAGRAM_TOKEN": None,
        "INSTAGRAM_ACCOUNT_ID": None,
        "INSTAGRAM_PAGE_ID": None,
        "PAGE_ID": None,
        "PAGE_ACCESS_TOKEN": None,
        "PLATFORM": None,
        "MESSENGER_PAGE_ID": None,
        "MESSENGER_PAGE_ACCESS_TOKEN": None,
        "PHONE_NUMBER_ID": None,
        "LONG_LIVED_TOKEN": None,
        "WABA_ID": None,
        "BUSINESS_ID": None,
        "SECRET_KEY": Config.SECRET_KEY,
        "VERIFY_TOKEN": Config.VERIFY_TOKEN,
        "MEDIA_ROOT": Config.MEDIA_ROOT,
        "SESSION_TIMEOUT": Config.SESSION_TIMEOUT,
        "SESSION_TIMEOUT_MESSAGE": Config.SESSION_TIMEOUT_MESSAGE,
        "IA_API_TOKEN": Config.IA_API_TOKEN,
        "IA_MODEL": Config.IA_MODEL,
        "IA_SYSTEM_MESSAGE": Config.IA_SYSTEM_MESSAGE,
        "IA_HISTORY_LIMIT": Config.IA_HISTORY_LIMIT,
        "IA_CATALOG_USE_OPENAI": Config.IA_CATALOG_USE_OPENAI,
        "IA_CATALOG_MAX_FILE_MB": Config.IA_CATALOG_MAX_FILE_MB,
        "IA_CATALOG_REQUEST_DELAY_SECONDS": Config.IA_CATALOG_REQUEST_DELAY_SECONDS,
    }

    if include_legacy_credentials:
        env.update({
            "META_TOKEN": Config.META_TOKEN,
            "MESSENGER_TOKEN": Config.MESSENGER_TOKEN,
            "INSTAGRAM_TOKEN": Config.INSTAGRAM_TOKEN,
            "INSTAGRAM_ACCOUNT_ID": os.getenv("INSTAGRAM_ACCOUNT_ID"),
            "INSTAGRAM_PAGE_ID": os.getenv("INSTAGRAM_PAGE_ID"),
            "PAGE_ID": Config.PAGE_ID,
            "PAGE_ACCESS_TOKEN": Config.PAGE_ACCESS_TOKEN,
            "PLATFORM": Config.PLATFORM,
            "MESSENGER_PAGE_ID": Config.MESSENGER_PAGE_ID,
            "MESSENGER_PAGE_ACCESS_TOKEN": Config.MESSENGER_PAGE_ACCESS_TOKEN,
            "PHONE_NUMBER_ID": Config.PHONE_NUMBER_ID,
        })

    return env


def _coerce_env_value(key: str, value):
    if value in (None, ""):
        return None

    if key == "SESSION_TIMEOUT":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return value


def _merge_env(defaults: Mapping, overrides: Mapping | None):
    merged = dict(defaults)
    if not overrides:
        return merged

    for key, value in overrides.items():
        if key not in TENANT_ENV_KEYS:
            continue
        coerced = _coerce_env_value(key, value)
        if coerced is None:
            continue
        merged[key] = coerced
    return merged


def _resolve_media_root(base_root: str | None) -> str:
    """Normaliza la ruta base de medios para garantizar que esté bajo ``static``.

    Si el ``MEDIA_ROOT`` configurado queda fuera del directorio ``static`` de la
    aplicación, se fuerza el valor por defecto para evitar que los archivos no
    puedan servirse mediante ``url_for('static', ...)``.
    """

    root = base_root or Config.MEDIA_ROOT
    if not root:
        root = Config.MEDIA_ROOT

    root = os.path.abspath(root)
    static_root = os.path.abspath(os.path.join(Config.BASEDIR, "static"))

    if os.path.commonpath([root, static_root]) != static_root:
        root = Config.MEDIA_ROOT

    return root


def _row_to_tenant(row) -> TenantInfo | None:
    if not row:
        return None
    return TenantInfo(
        tenant_key=row.get("tenant_key"),
        name=row.get("name") or row.get("tenant_key"),
        db_name=row.get("db_name"),
        db_host=row.get("db_host"),
        db_port=row.get("db_port") or Config.DB_PORT,
        db_user=row.get("db_user"),
        db_password=row.get("db_password"),
        metadata=_deserialize_metadata(row.get("metadata")),
    )


def _tenant_has_env_value(tenant: TenantInfo, key: str, expected: str | None) -> bool:
    if not expected:
        return False

    env = get_tenant_env(tenant)
    value = env.get(key)
    return str(value).strip() == str(expected).strip()


def get_tenant_env(
    tenant: TenantInfo | None = None, *, include_legacy_credentials: bool | None = None
) -> dict:
    """Devuelve el entorno efectivo de un tenant.

    - Para tenants explícitos no se incluyen credenciales globales (META_TOKEN,
      PHONE_NUMBER_ID) para evitar que distintas empresas compartan tokens por
      accidente.
    - Cuando ``tenant`` es ``None`` (modo legacy single-tenant), se habilita la
      inclusión opcional de las credenciales globales si ``include_legacy_credentials``
      es ``True`` o se deja en ``None`` (valor por defecto).
    """

    if include_legacy_credentials is None:
        include_legacy_credentials = tenant is None

    base_env = _default_tenant_env(include_legacy_credentials=include_legacy_credentials)
    if not tenant:
        return base_env

    metadata = tenant.metadata or {}
    env_overrides = {}
    if isinstance(metadata, dict):
        raw_env = metadata.get("env") if "env" in metadata else metadata
        if isinstance(raw_env, dict):
            env_overrides = raw_env

    env = _merge_env(base_env, env_overrides)
    return env


def set_current_tenant_env(env: dict | None):
    _CURRENT_TENANT_ENV.set(env)


def get_current_tenant_env() -> dict:
    env = _CURRENT_TENANT_ENV.get()
    if env is not None:
        return env
    return get_tenant_env(get_current_tenant())


def get_tenant(tenant_key: str, *, force_reload: bool = False) -> TenantInfo | None:
    if not tenant_key:
        return None
    if tenant_key in _tenant_cache and not force_reload:
        return _tenant_cache[tenant_key]

    conn = db.get_master_connection()
    try:
        c = conn.cursor(dictionary=True)
        c.execute(
            """
            SELECT tenant_key, name, db_name, db_host, db_port, db_user, db_password, metadata
              FROM tenants
             WHERE tenant_key=%s
            """,
            (tenant_key,),
        )
        tenant = _row_to_tenant(c.fetchone())
        if tenant:
            _tenant_cache[tenant_key] = tenant
        return tenant
    finally:
        conn.close()


def list_tenants(*, force_reload: bool = False) -> List[TenantInfo]:
    if not force_reload and _tenant_cache:
        return list(_tenant_cache.values())

    conn = db.get_master_connection()
    tenants_list: List[TenantInfo] = []
    try:
        c = conn.cursor(dictionary=True)
        c.execute(
            """
            SELECT tenant_key, name, db_name, db_host, db_port, db_user, db_password, metadata
              FROM tenants
             ORDER BY created_at DESC, tenant_key
            """
        )
        for row in c.fetchall() or []:
            tenant = _row_to_tenant(row)
            if tenant:
                tenants_list.append(tenant)
                _tenant_cache[tenant.tenant_key] = tenant
    finally:
        conn.close()

    return tenants_list


def find_tenant_by_phone_number_id(phone_number_id: str | None) -> TenantInfo | None:
    """Busca el tenant cuyo ``PHONE_NUMBER_ID`` coincida con el valor dado."""

    if not phone_number_id:
        return None

    for tenant in list_tenants(force_reload=True):
        # Forzamos las credenciales específicas del tenant para evitar matches
        # por valores globales heredados.
        if _tenant_has_env_value(
            tenant,
            "PHONE_NUMBER_ID",
            phone_number_id,
        ):
            return tenant

    return None


def find_tenant_by_page_id(page_id: str | None) -> TenantInfo | None:
    """Busca el tenant cuyo ``PAGE_ID`` coincida con el valor dado."""

    if not page_id:
        return None

    for tenant in list_tenants(force_reload=True):
        for key in ("MESSENGER_PAGE_ID", "PAGE_ID"):
            if _tenant_has_env_value(tenant, key, page_id):
                return tenant
        for key in ("INSTAGRAM_ACCOUNT_ID", "INSTAGRAM_PAGE_ID"):
            if _tenant_has_env_value(tenant, key, page_id):
                return tenant
        metadata = tenant.metadata or {}
        if isinstance(metadata, dict):
            instagram_account = metadata.get("instagram_account") or {}
            instagram_id = (
                instagram_account.get("id") if isinstance(instagram_account, dict) else None
            )
            if instagram_id and str(instagram_id).strip() == str(page_id).strip():
                return tenant
            page_selection = metadata.get("page_selection") or {}
            if isinstance(page_selection, dict):
                instagram_selection = page_selection.get("instagram") or {}
                instagram_page_id = (
                    instagram_selection.get("page_id")
                    if isinstance(instagram_selection, dict)
                    else None
                )
                if instagram_page_id and str(instagram_page_id).strip() == str(page_id).strip():
                    return tenant

    return None


def auto_select_single_tenant(*, force_reload: bool = False) -> TenantInfo | None:
    """Devuelve el único tenant registrado si solo existe uno.

    Esto facilita escenarios en los que la app se despliega en modo multiempresa
    pero aún no se configuró ``DEFAULT_TENANT`` ni se envía el encabezado
    ``X-Tenant-ID`` desde el frontend. Si no hay tenants o hay más de uno,
    retorna ``None`` para evitar ambigüedades.
    """

    tenants_list = list_tenants(force_reload=force_reload)
    if len(tenants_list) == 1:
        return tenants_list[0]
    return None


def update_tenant_metadata(tenant_key: str, metadata: Mapping | None) -> TenantInfo:
    tenant = get_tenant(tenant_key)
    if not tenant:
        raise TenantNotFoundError(f"No se encontró la empresa '{tenant_key}'.")

    merged = {}
    if isinstance(tenant.metadata, dict):
        merged.update(tenant.metadata)
    if metadata:
        merged.update(metadata)

    conn = db.get_master_connection(ensure_database=True)
    try:
        c = conn.cursor()
        c.execute(
            "UPDATE tenants SET metadata=%s WHERE tenant_key=%s",
            (json.dumps(merged or {}), tenant_key),
        )
        conn.commit()
    finally:
        conn.close()

    _tenant_cache.pop(tenant_key, None)
    return get_tenant(tenant_key, force_reload=True)


def update_tenant_env(tenant_key: str, env_updates: Mapping | None) -> TenantInfo:
    tenant = get_tenant(tenant_key)
    if not tenant:
        raise TenantNotFoundError(f"No se encontró la empresa '{tenant_key}'.")

    previous_env = get_tenant_env(tenant)

    metadata = dict(tenant.metadata or {})
    env_section = metadata.get("env") if isinstance(metadata.get("env"), dict) else {}
    env_section = dict(env_section)

    for key in TENANT_ENV_KEYS:
        raw_value = (env_updates or {}).get(key) if env_updates else None
        coerced = _coerce_env_value(key, raw_value)
        if coerced is None:
            env_section.pop(key, None)
        else:
            env_section[key] = coerced

    metadata["env"] = env_section
    updated_tenant = update_tenant_metadata(tenant_key, metadata)
    _trigger_page_backfill_if_needed(previous_env, updated_tenant)
    return updated_tenant


def _normalize_platform(value: str | None) -> str | None:
    if not value:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"messenger", "instagram"}:
        return normalized
    return None


def _resolve_page_credentials(env: dict, platform: str) -> tuple[str, str]:
    normalized = _normalize_platform(platform)
    if normalized == "instagram":
        instagram_token = (env.get("INSTAGRAM_TOKEN") or "").strip()
        page_id = "me" if instagram_token else ""
        page_token = instagram_token
    else:
        page_id = (env.get("MESSENGER_PAGE_ID") or "").strip()
        page_token = (env.get("MESSENGER_PAGE_ACCESS_TOKEN") or "").strip()

    if page_id and page_token:
        return page_id, page_token

    legacy_platform = _normalize_platform(env.get("PLATFORM"))
    if legacy_platform != normalized:
        return page_id, page_token

    page_id = page_id or (env.get("PAGE_ID") or "").strip()
    page_token = page_token or (env.get("PAGE_ACCESS_TOKEN") or "").strip()

    return page_id, page_token


def _should_trigger_page_backfill(previous_env: dict, new_env: dict, platform: str) -> bool:
    prev_id, prev_token = _resolve_page_credentials(previous_env, platform)
    new_id, new_token = _resolve_page_credentials(new_env, platform)

    changed = prev_id != new_id or prev_token != new_token
    if not changed:
        return False

    return bool(new_id and new_token)


def _trigger_page_backfill_if_needed(previous_env: dict, tenant: TenantInfo | None):
    if not tenant:
        return
    new_env = get_tenant_env(tenant)
    platforms = ("messenger", "instagram")
    should_run = any(
        _should_trigger_page_backfill(previous_env, new_env, platform)
        for platform in platforms
    )
    if not should_run:
        return

    try:
        ensure_tenant_schema(tenant)
    except Exception:
        logger.exception(
            "No se pudo preparar el esquema del tenant para backfill",
            extra={"tenant_key": tenant.tenant_key},
        )
        return

    try:
        from services import page_backfill
    except Exception:
        logger.exception(
            "No se pudo importar el módulo de backfill",
            extra={"tenant_key": tenant.tenant_key},
        )
        return

    for platform in platforms:
        if not _should_trigger_page_backfill(previous_env, new_env, platform):
            continue
        page_id, page_token = _resolve_page_credentials(new_env, platform)
        logger.info(
            "Encolando backfill de conversaciones",
            extra={
                "tenant_key": tenant.tenant_key,
                "platform": platform,
                "page_id": page_id,
            },
        )
        page_backfill.enqueue_page_backfill(
            tenant_key=tenant.tenant_key,
            db_settings=tenant.as_db_settings(),
            page_id=page_id,
            access_token=page_token,
            platform=platform,
        )


def trigger_page_backfill_for_platform(tenant: TenantInfo | None, platform: str) -> None:
    if not tenant:
        return
    normalized = _normalize_platform(platform)
    if not normalized:
        return

    try:
        ensure_tenant_schema(tenant)
    except Exception:
        logger.exception(
            "No se pudo preparar el esquema del tenant para backfill",
            extra={"tenant_key": tenant.tenant_key},
        )
        return

    try:
        from services import page_backfill
    except Exception:
        logger.exception(
            "No se pudo importar el módulo de backfill",
            extra={"tenant_key": tenant.tenant_key},
        )
        return

    page_id, page_token = _resolve_page_credentials(get_tenant_env(tenant), normalized)
    if not page_id or not page_token:
        logger.info(
            "Backfill no encolado por credenciales incompletas",
            extra={"tenant_key": tenant.tenant_key, "platform": normalized},
        )
        return

    logger.info(
        "Encolando backfill de conversaciones (manual)",
        extra={
            "tenant_key": tenant.tenant_key,
            "platform": normalized,
            "page_id": page_id,
        },
    )
    page_backfill.enqueue_page_backfill(
        tenant_key=tenant.tenant_key,
        db_settings=tenant.as_db_settings(),
        page_id=page_id,
        access_token=page_token,
        platform=normalized,
    )


def set_current_tenant(tenant: TenantInfo | None):
    _CURRENT_TENANT.set(tenant)
    if tenant:
        db.set_tenant_db_settings(tenant.as_db_settings())
        db.set_current_tenant_key(tenant.tenant_key)
        set_current_tenant_env(get_tenant_env(tenant))
    else:
        db.clear_tenant_db_settings()
        db.clear_current_tenant_key()
        set_current_tenant_env(None)


def clear_current_tenant():
    set_current_tenant(None)


def get_current_tenant() -> TenantInfo | None:
    return _CURRENT_TENANT.get()


def get_runtime_setting(key: str, default=None, *, cast=None):
    env = get_current_tenant_env()
    if key == "MEDIA_ROOT":
        value = get_media_root()
    else:
        value = env.get(key, default)
    if cast and value is not None:
        try:
            value = cast(value)
        except (TypeError, ValueError):
            value = default
    return value


def get_active_tenant_key(*, include_default: bool = True) -> str | None:
    """Devuelve la clave del tenant activo o el tenant por defecto.

    Si no hay un tenant en contexto y ``include_default`` es ``True`` se
    utilizará ``Config.DEFAULT_TENANT`` cuando esté configurado.
    """

    tenant = get_current_tenant()
    if tenant:
        return tenant.tenant_key

    if include_default:
        default_key = (Config.DEFAULT_TENANT or "").strip()
        return default_key or None

    return None


def get_media_root(*, tenant_key: str | None = None) -> str:
    """Obtiene la ruta de medios incluyendo el subdirectorio del tenant.

    - Usa ``MEDIA_ROOT`` del entorno del tenant actual (o valores por defecto).
    - Si existe un tenant activo, agrega un subdirectorio con su ``tenant_key``.
    - Si no hay tenant en contexto, pero se configuró ``DEFAULT_TENANT``, usa
      ese subdirectorio.
    """

    env = get_current_tenant_env()
    base_root = _resolve_media_root(env.get("MEDIA_ROOT") or Config.MEDIA_ROOT)

    key = tenant_key or get_active_tenant_key()
    if key:
        normalized = os.path.normpath(base_root)
        if os.path.basename(normalized) != key:
            base_root = os.path.join(base_root, key)

    os.makedirs(base_root, exist_ok=True)
    return base_root


def get_uploads_url_path(filename: str, *, tenant_key: str | None = None) -> str:
    """Devuelve la ruta relativa bajo ``static`` para un archivo en uploads.

    Si hay un tenant activo (o un tenant por defecto configurado), agrega un
    subdirectorio con su clave para que las URLs apunten al espacio correcto.
    """

    clean_filename = str(filename).lstrip("/ ")
    key = tenant_key or get_active_tenant_key()

    segments = ["uploads"]
    if key:
        segments.append(key)
    segments.append(clean_filename)

    return posixpath.join(*segments)


def ensure_default_tenant_registered() -> TenantInfo | None:
    default_key = (Config.DEFAULT_TENANT or "").strip()
    if not default_key:
        return None

    existing = get_tenant(default_key)
    if existing:
        return existing

    conn = db.get_master_connection(ensure_database=True)
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO tenants (
                tenant_key, name, db_name, db_host, db_port, db_user, db_password, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name=VALUES(name),
                db_name=VALUES(db_name),
                db_host=VALUES(db_host),
                db_port=VALUES(db_port),
                db_user=VALUES(db_user),
                db_password=VALUES(db_password),
                metadata=VALUES(metadata)
            """,
            (
                default_key,
                Config.DEFAULT_TENANT_NAME or default_key,
                Config.DB_NAME,
                Config.DB_HOST,
                Config.DB_PORT,
                Config.DB_USER,
                Config.DB_PASSWORD,
                json.dumps({"source": "default"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _tenant_cache.pop(default_key, None)
    return get_tenant(default_key)


def ensure_tenant_schema(tenant: TenantInfo):
    db.init_db(db_settings=tenant.as_db_settings())


def ensure_registered_tenants_schema(*, skip: set[str] | None = None):
    """Crea bases y tablas para todos los tenants registrados en DB_NAME."""

    skip_keys = {key for key in (skip or set()) if key}
    for tenant in list_tenants(force_reload=True):
        if tenant.tenant_key in skip_keys:
            continue
        ensure_tenant_schema(tenant)


def get_tenant_roles(tenant: TenantInfo) -> list[dict]:
    ensure_tenant_schema(tenant)
    conn = db.get_connection(db_settings=tenant.as_db_settings(), ensure_database=True)
    try:
        c = conn.cursor(dictionary=True)
        c.execute("SELECT id, name, keyword FROM roles ORDER BY name")
        return c.fetchall() or []
    finally:
        conn.close()


def list_tenant_users(tenant: TenantInfo) -> list[dict]:
    ensure_tenant_schema(tenant)
    conn = db.get_connection(db_settings=tenant.as_db_settings(), ensure_database=True)
    try:
        c = conn.cursor(dictionary=True)
        c.execute(
            """
            SELECT u.id,
                   u.username,
                   GROUP_CONCAT(r.keyword ORDER BY r.keyword SEPARATOR ',') AS roles
              FROM usuarios u
              LEFT JOIN user_roles ur ON ur.user_id = u.id
              LEFT JOIN roles r ON r.id = ur.role_id
             GROUP BY u.id, u.username
             ORDER BY u.username
            """
        )
        return c.fetchall() or []
    finally:
        conn.close()


def bootstrap_tenant_registry():
    db.init_master_db()


def resolve_tenant_from_request(request: Request) -> TenantInfo:
    tenant_key = (
        request.headers.get(Config.TENANT_HEADER)
        or request.args.get("tenant")
        or Config.DEFAULT_TENANT
    )

    if not tenant_key:
        raise TenantResolutionError(
            "No se proporcionó la empresa objetivo y no existe un tenant por defecto."
        )

    tenant = get_tenant(tenant_key)
    if tenant is None:
        raise TenantNotFoundError(f"No se encontró la empresa '{tenant_key}'.")

    return tenant


def ensure_default_tenant_schema():
    tenant = get_tenant(Config.DEFAULT_TENANT) if Config.DEFAULT_TENANT else None
    if tenant:
        ensure_tenant_schema(tenant)


def register_tenant(tenant: TenantInfo, *, ensure_schema: bool = True):
    conn = db.get_master_connection(ensure_database=True)
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO tenants (
                tenant_key, name, db_name, db_host, db_port, db_user, db_password, metadata
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                name=VALUES(name),
                db_name=VALUES(db_name),
                db_host=VALUES(db_host),
                db_port=VALUES(db_port),
                db_user=VALUES(db_user),
                db_password=VALUES(db_password),
                metadata=VALUES(metadata)
            """,
            (
                tenant.tenant_key,
                tenant.name,
                tenant.db_name,
                tenant.db_host,
                tenant.db_port,
                tenant.db_user,
                tenant.db_password,
                json.dumps(tenant.metadata or {}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    _tenant_cache.pop(tenant.tenant_key, None)
    created = get_tenant(tenant.tenant_key, force_reload=True)
    if ensure_schema and created:
        ensure_tenant_schema(created)
    return created


def delete_tenant(tenant_key: str):
    tenant = get_tenant(tenant_key)
    if not tenant:
        raise TenantNotFoundError(f"No se encontró la empresa '{tenant_key}'.")

    if Config.DEFAULT_TENANT and tenant_key == Config.DEFAULT_TENANT:
        raise ValueError("No se puede eliminar el tenant por defecto configurado.")

    conn = db.get_master_connection(ensure_database=True)
    try:
        c = conn.cursor()
        c.execute("DELETE FROM tenants WHERE tenant_key=%s", (tenant_key,))
        conn.commit()
    finally:
        conn.close()

    _tenant_cache.pop(tenant_key, None)
    if get_current_tenant() and get_current_tenant().tenant_key == tenant_key:
        clear_current_tenant()


def create_or_update_tenant_user(
    tenant: TenantInfo, username: str, password: str, role_keywords: list[str]
):
    if not username or not password:
        raise ValueError("El usuario y la contraseña son obligatorios.")

    ensure_tenant_schema(tenant)
    conn = db.get_connection(db_settings=tenant.as_db_settings(), ensure_database=True)
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM usuarios WHERE username=%s", (username,))
        row = c.fetchone()
        hashed = generate_password_hash(password)
        if row:
            user_id = row[0]
            c.execute("UPDATE usuarios SET password=%s WHERE id=%s", (hashed, user_id))
        else:
            c.execute(
                "INSERT INTO usuarios (username, password) VALUES (%s, %s)",
                (username, hashed),
            )
            user_id = c.lastrowid
        conn.commit()

        current_roles = set(
            db.get_roles_by_user(user_id, db_settings=tenant.as_db_settings()) or []
        )
        requested_roles = set(role_keywords or [])

        # Remover roles que ya no se desean
        for keyword in current_roles - requested_roles:
            c.execute(
                "DELETE ur FROM user_roles ur JOIN roles r ON ur.role_id=r.id "
                "WHERE ur.user_id=%s AND r.keyword=%s",
                (user_id, keyword),
            )

        # Asignar roles solicitados
        for keyword in requested_roles:
            db.assign_role_to_user(
                user_id,
                keyword,
                db_settings=tenant.as_db_settings(),
            )
        conn.commit()
    finally:
        conn.close()

    return user_id


__all__ = [
    "TenantInfo",
    "TenantNotFoundError",
    "TenantResolutionError",
    "bootstrap_tenant_registry",
    "clear_current_tenant",
    "ensure_default_tenant_registered",
    "ensure_default_tenant_schema",
    "ensure_registered_tenants_schema",
    "get_current_tenant_env",
    "ensure_tenant_schema",
    "get_tenant_roles",
    "get_tenant_env",
    "get_current_tenant",
    "get_tenant", 
    "list_tenant_users", 
    "list_tenants", 
    "get_active_tenant_key",
    "get_media_root",
    "register_tenant",
    "resolve_tenant_from_request",
    "find_tenant_by_phone_number_id",
    "find_tenant_by_page_id",
    "auto_select_single_tenant",
    "set_current_tenant",
    "set_current_tenant_env",
    "update_tenant_env",
    "update_tenant_metadata",
    "create_or_update_tenant_user",
    "get_runtime_setting",
    "TENANT_ENV_KEYS",
    "delete_tenant",
]
