"""CLI para registrar nuevas empresas (tenants) y crear su esquema aislado."""

from __future__ import annotations

import argparse
import json

from config import Config
from services import db
from services import tenants


def parse_args():
    parser = argparse.ArgumentParser(
        description="Registra una nueva empresa en la tabla tenants y opcionalmente inicializa su esquema.",
    )
    parser.add_argument("tenant_key", help="Identificador único de la empresa (se usará en el header X-Tenant-ID)")
    parser.add_argument("db_name", help="Nombre de la base de datos exclusiva de la empresa")
    parser.add_argument(
        "--name",
        default=None,
        help="Nombre legible de la empresa (por defecto se usa tenant_key)",
    )
    parser.add_argument(
        "--db-host",
        default=Config.DB_HOST,
        help=f"Host de la base de datos (default: {Config.DB_HOST})",
    )
    parser.add_argument(
        "--metadata",
        default="{}",
        help="JSON opcional con metadatos (branding, región, plan, etc.)",
    )
    parser.add_argument(
        "--skip-init-schema",
        action="store_true",
        help=(
            "Registrar la empresa sin crear/actualizar el esquema aislado. "
            "Por defecto siempre se generan las tablas."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    tenants.bootstrap_tenant_registry()

    metadata = json.loads(args.metadata) if args.metadata else {}
    info = tenants.TenantInfo(
        tenant_key=args.tenant_key,
        name=args.name or args.tenant_key,
        db_name=args.db_name,
        db_host=args.db_host,
        db_port=Config.DB_PORT,
        db_user=Config.DB_USER,
        db_password=Config.DB_PASSWORD,
        metadata=metadata,
    )

    created = tenants.register_tenant(info, ensure_schema=not args.skip_init_schema)
    db.set_tenant_db_settings(None)

    if created:
        print("Empresa registrada exitosamente:")
        print(json.dumps(created.__dict__, indent=2))
        if not args.skip_init_schema:
            print("Esquema inicializado en la base aislada.")
    else:
        print("No se pudo registrar la empresa.")


if __name__ == "__main__":
    main()
