"""Tenant management helpers for multi-tenant isolation."""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from typing import Dict
from flask import Request

from config import Config
from services import db


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


def set_current_tenant(tenant: TenantInfo | None):
    _CURRENT_TENANT.set(tenant)
    if tenant:
        db.set_tenant_db_settings(tenant.as_db_settings())
    else:
        db.clear_tenant_db_settings()


def clear_current_tenant():
    set_current_tenant(None)


def get_current_tenant() -> TenantInfo | None:
    return _CURRENT_TENANT.get()


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


__all__ = [
    "TenantInfo",
    "TenantNotFoundError",
    "TenantResolutionError",
    "bootstrap_tenant_registry",
    "clear_current_tenant",
    "ensure_default_tenant_registered",
    "ensure_default_tenant_schema",
    "ensure_tenant_schema",
    "get_current_tenant",
    "get_tenant",
    "resolve_tenant_from_request",
    "set_current_tenant",
]
