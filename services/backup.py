import datetime
import logging
import os
import subprocess
import threading
from typing import Optional

from config import Config

_BACKUP_THREAD: Optional[threading.Thread] = None
_STOP_EVENT: Optional[threading.Event] = None
_BACKUP_INTERVAL_SECONDS = 24 * 60 * 60


def _get_backup_directory() -> str:
    backup_dir = os.path.expanduser(Config.DB_BACKUP_DIR)
    os.makedirs(backup_dir, exist_ok=True)
    return backup_dir


def _seconds_until_next_midnight() -> float:
    """Return the number of seconds remaining until the next midnight."""

    now = datetime.datetime.now()
    tomorrow_midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow_midnight - now).total_seconds()


def backup_database() -> str:
    """Create a SQL dump of the configured database and return the file path."""
    if not Config.DB_NAME:
        raise RuntimeError("DB_NAME no está configurado; no se puede crear la copia de seguridad.")

    backup_dir = _get_backup_directory()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{timestamp}_{Config.DB_NAME}.sql"
    backup_path = os.path.join(backup_dir, backup_filename)

    command = [
        "mysqldump",
        f"--host={Config.DB_HOST}",
        f"--port={Config.DB_PORT}",
        f"--user={Config.DB_USER}",
        f"--password={Config.DB_PASSWORD}",
        Config.DB_NAME,
    ]

    try:
        with open(backup_path, "wb") as backup_file:
            result = subprocess.run(command, stdout=backup_file, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError("El comando 'mysqldump' no está disponible en el sistema.") from exc

    if result.returncode != 0:
        if os.path.exists(backup_path):
            os.remove(backup_path)
        stderr = result.stderr.decode("utf-8", errors="ignore")
        raise RuntimeError(f"mysqldump devolvió un error: {stderr}")

    logging.getLogger(__name__).info("Copia de seguridad creada en %s", backup_path)
    return backup_path


def _backup_loop(stop_event: threading.Event):
    logger = logging.getLogger(__name__)

    wait_seconds = _seconds_until_next_midnight()
    logger.info("El respaldo diario se ejecutará en %.0f segundos (a la medianoche).", wait_seconds)
    if stop_event.wait(wait_seconds):
        return

    while not stop_event.is_set():
        try:
            backup_database()
        except Exception:  # noqa: BLE001 - Queremos registrar cualquier error inesperado
            logger.exception("Error al crear la copia de seguridad de la base de datos")

        if stop_event.wait(_BACKUP_INTERVAL_SECONDS):
            break


def start_daily_backup_scheduler():
    """Start the background thread that runs the database backup once per day."""
    global _BACKUP_THREAD, _STOP_EVENT

    if _BACKUP_THREAD and _BACKUP_THREAD.is_alive():
        return

    _STOP_EVENT = threading.Event()
    _BACKUP_THREAD = threading.Thread(target=_backup_loop, args=(_STOP_EVENT,), daemon=True)
    _BACKUP_THREAD.start()


def stop_daily_backup_scheduler():
    global _STOP_EVENT

    if _STOP_EVENT:
        _STOP_EVENT.set()
