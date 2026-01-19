"""Procesa catálogos PDF en segundo plano y actualiza su estado en BD."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from threading import Event
from datetime import datetime

from services import db, tenants
from services.catalog import CatalogIngestCancelled, ingest_catalog_pdf

logger = logging.getLogger(__name__)

_lock = threading.Lock()


@dataclass
class CatalogIngestTask:
    stop_event: Event
    config_id: int


_running_tasks: dict[str, CatalogIngestTask] = {}


def _update_ingest_status(
    config_id: int,
    *,
    state: str,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    error: str | None = None,
) -> None:
    conn = db.get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            UPDATE ia_config
               SET pdf_ingest_state = %s,
                   pdf_ingest_started_at = %s,
                   pdf_ingest_finished_at = %s,
                   pdf_ingest_error = %s
             WHERE id = %s
            """,
            (
                state,
                started_at,
                finished_at,
                error,
                config_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _normalize_key(tenant: tenants.TenantInfo | None) -> str:
    if tenant:
        return tenant.tenant_key or "default"
    return "default"


def enqueue_catalog_pdf_ingest(
    *,
    config_id: int,
    pdf_path: str,
    stored_name: str,
    tenant: tenants.TenantInfo | None,
) -> bool:
    """Lanza la ingesta del PDF en un hilo de fondo.

    Si ya existía un proceso para el tenant, se cancela el anterior y se
    inicia la nueva ingesta.
    """

    key = _normalize_key(tenant)
    stop_event = Event()
    task = CatalogIngestTask(stop_event=stop_event, config_id=config_id)
    with _lock:
        previous = _running_tasks.get(key)
        if previous:
            previous.stop_event.set()
        _running_tasks[key] = task

    def _runner() -> None:
        try:
            if tenant:
                tenants.set_current_tenant(tenant)
            else:
                tenants.clear_current_tenant()

            started_at = datetime.utcnow()
            _update_ingest_status(
                config_id,
                state="running",
                started_at=started_at,
                finished_at=None,
                error=None,
            )
            ingest_catalog_pdf(pdf_path, stored_name, stop_event=stop_event)
            _update_ingest_status(
                config_id,
                state="succeeded",
                started_at=started_at,
                finished_at=datetime.utcnow(),
                error=None,
            )
        except CatalogIngestCancelled as exc:
            logger.info("Ingesta de catálogo cancelada", extra={"reason": str(exc)})
            _update_ingest_status(
                config_id,
                state="cancelled",
                started_at=None,
                finished_at=datetime.utcnow(),
                error=str(exc),
            )
        except Exception as exc:  # pragma: no cover - depende del runtime
            logger.exception("Error al indexar catálogo PDF", exc_info=exc)
            _update_ingest_status(
                config_id,
                state="failed",
                started_at=None,
                finished_at=datetime.utcnow(),
                error=str(exc),
            )
        finally:
            tenants.clear_current_tenant()
            with _lock:
                current = _running_tasks.get(key)
                if current is task:
                    _running_tasks.pop(key, None)

    thread = threading.Thread(
        target=_runner, name=f"catalog-pdf-{key}", daemon=True
    )
    thread.start()
    return True


def is_catalog_pdf_ingest_running(tenant: tenants.TenantInfo | None) -> bool:
    """Indica si existe una ingesta de catálogo en ejecución para el tenant."""

    key = _normalize_key(tenant)
    with _lock:
        return key in _running_tasks
