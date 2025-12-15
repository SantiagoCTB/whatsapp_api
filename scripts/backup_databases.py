"""Herramienta CLI para generar copias de seguridad de todas las bases de datos.

- Permite definir la carpeta destino vía la variable de entorno ``BACKUP_ROOT``
  (por ejemplo en el .env) o con ``--output-dir``.
- Si no se configura ninguna ruta, usa la carpeta superior al proyecto como
  ubicación por defecto.
- Recorre la base central y cada tenant registrado para crear volcados
  independientes ordenados por base y fecha.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BACKUP_ROOT = PROJECT_ROOT.parent

DatabaseSource = Tuple[str, "db.DatabaseSettings"]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genera copias de seguridad de todas las bases de datos configuradas.")
    parser.add_argument(
        "--output-dir",
        help=(
            "Ruta donde se guardarán los respaldos. Si se omite, se usa BACKUP_ROOT"
            " del entorno o la carpeta padre del proyecto."
        ),
    )
    parser.add_argument("--env-file", help="Ruta al archivo .env a cargar antes de leer la configuración.")
    parser.add_argument("--tag", help="Texto opcional para identificar el contexto del respaldo en los logs.")
    return parser.parse_args()


def _ensure_tools_available() -> str:
    """Return the path to ``mysqldump`` or raise an informative error.

    The lookup order is:
    1. A user-provided override via ``MYSQLDUMP_PATH`` (accepts file or folder).
    2. ``PATH`` discovery (works for Linux packages like ``mysql-client``).
    3. Common Windows/MariaDB install locations.
    4. Common Linux tarball locations (``/usr/local/mysql``).
    """

    def _expand_candidate(path: str) -> Path:
        candidate = Path(path)
        if candidate.is_dir():
            # Allow pointing to the MySQL/MariaDB "bin" folder directly.
            exe_name = "mysqldump.exe" if os.name == "nt" else "mysqldump"
            candidate = candidate / exe_name
        return candidate

    candidates = []

    # Highest priority: explicit override
    override = os.getenv("MYSQLDUMP_PATH")
    if override:
        candidates.append(_expand_candidate(override))

    # PATH lookup
    found = shutil.which("mysqldump")
    if found:
        candidates.append(Path(found))

    # User-provided install roots (allows matching mysqld location when PATH is clean)
    for env_key in ("MYSQL_HOME", "MYSQL_BASE", "MYSQL_ROOT"):
        custom_root = os.getenv(env_key)
        if custom_root:
            candidates.append(_expand_candidate(str(Path(custom_root) / "bin")))

    # Typical Windows installation folders (different versions and vendors)
    windows_roots = [os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)"), os.getenv("ProgramW6432")]
    for base in filter(None, windows_roots):
        for vendor in ("MySQL", "MariaDB"):
            root = Path(base) / vendor
            for exe in glob.glob(str(root / "*Server*" / "bin" / "mysqldump.exe")):
                candidates.append(Path(exe))

    # Common Linux tarball location
    candidates.append(Path("/usr/local/mysql/bin/mysqldump"))

    for path in candidates:
        if path.is_file():
            return str(path)

    raise RuntimeError(
        "No se encontró 'mysqldump'. Instala el cliente de MySQL (p. ej. "
        "'sudo apt install mysql-client' en Linux) o define la variable "
        "MYSQLDUMP_PATH apuntando al ejecutable o a la carpeta 'bin'."
    )


def _resolve_backup_root(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir)

    env_dir = os.getenv("BACKUP_ROOT")
    if env_dir:
        return Path(env_dir)

    return DEFAULT_BACKUP_ROOT


def _load_dependencies(env_file: str | None):
    # Cargar variables antes de importar la configuración para que tome el valor correcto.
    load_dotenv(env_file)
    global Config, db, tenants
    from config import Config  # type: ignore
    from services import db, tenants  # type: ignore

    return Config, db, tenants


def _collect_database_sources(Config, tenants) -> Iterable[DatabaseSource]:
    required = {
        "DB_HOST": Config.DB_HOST,
        "DB_PORT": Config.DB_PORT,
        "DB_USER": Config.DB_USER,
        "DB_PASSWORD": Config.DB_PASSWORD,
        "DB_NAME": Config.DB_NAME,
    }

    missing = [key for key, value in required.items() if value in (None, "")]
    if missing:
        raise RuntimeError(
            "Faltan variables obligatorias de conexión para la base central: "
            + ", ".join(sorted(missing))
        )

    base_settings = db.DatabaseSettings(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        name=Config.DB_NAME,
    )

    seen: Dict[str, DatabaseSource] = {}
    seen[base_settings.name] = (Config.DEFAULT_TENANT or "control", base_settings)

    for tenant in tenants.list_tenants(force_reload=True):
        settings = tenant.as_db_settings()
        seen[settings.name] = (tenant.tenant_key, settings)

    return seen.values()


def _dump_database(label: str, settings, backup_root: Path, mysqldump_exe: str) -> Path:
    if not all([settings.host, settings.user, settings.name]):
        raise RuntimeError(f"Credenciales incompletas para la base '{label}'.")

    timestamp = datetime.now()
    dated_folder = backup_root / settings.name / timestamp.strftime("%Y-%m-%d")
    dated_folder.mkdir(parents=True, exist_ok=True)

    output_file = dated_folder / f"{settings.name}_{timestamp.strftime('%Y%m%d_%H%M%S')}.sql"

    env = os.environ.copy()
    if settings.password:
        env["MYSQL_PWD"] = str(settings.password)

    cmd = [
        mysqldump_exe,
        f"-h{settings.host}",
        f"-P{settings.port}",
        f"-u{settings.user}",
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        "--events",
        settings.name,
    ]

    with output_file.open("wb") as fh:
        result = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, env=env)

    if result.returncode != 0:
        output_file.unlink(missing_ok=True)
        stderr = result.stderr.decode() if result.stderr else ""
        raise RuntimeError(f"mysqldump falló para '{settings.name}': {stderr.strip()}")

    return output_file


def main():
    args = _parse_args()
    mysqldump_exe = _ensure_tools_available()

    Config, db, tenants = _load_dependencies(args.env_file)

    backup_root = _resolve_backup_root(args)
    backup_root.mkdir(parents=True, exist_ok=True)

    tag = f"[{args.tag}] " if args.tag else ""
    print(f"{tag}Guardando respaldos en: {backup_root}")

    sources = _collect_database_sources(Config, tenants)
    for label, settings in sources:
        print(f"{tag}Respaldando base '{settings.name}' (origen: {label})...")
        path = _dump_database(label, settings, backup_root, mysqldump_exe)
        print(f"{tag}✔ Respaldo creado en {path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CLI
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
