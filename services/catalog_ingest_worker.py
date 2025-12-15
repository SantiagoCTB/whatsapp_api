"""Gestión de trabajos en segundo plano para ingesta de catálogos."""

import json
import logging
import os
import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from threading import Lock
from typing import Dict, Optional, Set

from services.ai_responder import CatalogResponder


ExecutorResult = Dict[str, object]


class _CatalogIngestState:
    """Estado compartido para la ingesta en segundo plano."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._future: Optional[Future[ExecutorResult]] = None
        self._status: Dict[str, object] = {
            "state": "idle",
            "source_name": None,
            "file_type": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "stats": None,
        }

    def get_status(self) -> Dict[str, object]:
        with self._lock:
            status = dict(self._status)
            future = self._future
        if future and future.done():
            # Garantiza que los errores se reflejen en el estado principal.
            try:
                future.result()
            except Exception:  # pragma: no cover - se registra en el callback
                pass
        return status

    def start_job(
        self,
        responder: "CatalogResponder",
        file_path: str,
        source_name: str,
        file_type: str,
    ) -> None:
        with self._lock:
            if self._future and not self._future.done():
                raise RuntimeError("Ya existe un proceso de ingesta en ejecución.")

            self._status.update(
                {
                    "state": "running",
                    "source_name": source_name,
                    "file_type": file_type,
                    "started_at": datetime.utcnow().isoformat(),
                    "finished_at": None,
                    "error": None,
                    "stats": None,
                }
            )

            future = self._executor.submit(
                self._run_ingest, responder, file_path, source_name, file_type
            )
            future.add_done_callback(
                lambda fut: self._on_done(fut, file_path, source_name, file_type)
            )
            self._future = future

    @staticmethod
    def _find_combo_descriptor(root: str) -> Optional[str]:
        candidate_names = (
            "descriptor.json",
            "combo.json",
            "catalog.json",
            "metadata.json",
        )
        for name in candidate_names:
            candidate = os.path.join(root, name)
            if os.path.isfile(candidate):
                return candidate

        try:
            for entry in os.listdir(root):
                if entry.lower().endswith(".json"):
                    candidate = os.path.join(root, entry)
                    if os.path.isfile(candidate):
                        return candidate
        except FileNotFoundError:
            return None
        return None

    @staticmethod
    def _resolve_combo_value(descriptor: Dict[str, object], keys: tuple) -> Optional[str]:
        for key in keys:
            value = descriptor.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _make_absolute(path: Optional[str], base_dir: str) -> Optional[str]:
        if not path:
            return None
        if os.path.isabs(path):
            return os.path.abspath(path)
        return os.path.abspath(os.path.join(base_dir, path))

    @staticmethod
    def _auto_detect_resource(base_dir: str, extensions: tuple) -> Optional[str]:
        try:
            matches = [
                os.path.join(base_dir, entry)
                for entry in os.listdir(base_dir)
                if any(entry.lower().endswith(ext) for ext in extensions)
            ]
        except FileNotFoundError:
            return None
        if len(matches) == 1:
            return os.path.abspath(matches[0])
        return None

    def _prepare_combo_payload(
        self, file_path: str, source_name: str
    ) -> Dict[str, str]:
        descriptor_path = file_path
        descriptor_dir = file_path
        if os.path.isdir(file_path):
            descriptor_dir = file_path
            descriptor_path = self._find_combo_descriptor(file_path)
            if not descriptor_path:
                raise ValueError(
                    "El paquete combo no contiene un descriptor JSON reconocible."
                )
        else:
            descriptor_dir = os.path.dirname(file_path) or "."

        descriptor_path = os.path.abspath(descriptor_path)
        descriptor_dir = os.path.abspath(descriptor_dir)

        try:
            with open(descriptor_path, "r", encoding="utf-8") as fh:
                descriptor_data = json.load(fh)
        except Exception as exc:
            raise ValueError("No se pudo leer el descriptor del paquete combo.") from exc

        if not isinstance(descriptor_data, dict):
            raise ValueError("El descriptor de combo debe ser un objeto JSON.")

        pdf_value = self._resolve_combo_value(
            descriptor_data, ("pdf_path", "pdf", "pdfFile", "pdf_file")
        )
        text_value = self._resolve_combo_value(
            descriptor_data,
            ("text_path", "text", "txt", "textFile", "text_file"),
        )

        pdf_path = self._make_absolute(pdf_value, descriptor_dir)
        text_path = self._make_absolute(text_value, descriptor_dir)

        if (not pdf_path or not os.path.isfile(pdf_path)) and os.path.isdir(descriptor_dir):
            detected_pdf = self._auto_detect_resource(descriptor_dir, (".pdf",))
            if detected_pdf:
                pdf_path = detected_pdf
        if (not text_path or not os.path.isfile(text_path)) and os.path.isdir(descriptor_dir):
            detected_text = self._auto_detect_resource(descriptor_dir, (".txt", ".text"))
            if detected_text:
                text_path = detected_text

        if not pdf_path or not os.path.isfile(pdf_path):
            raise ValueError("No se encontró el archivo PDF referenciado en el paquete combo.")
        if not text_path or not os.path.isfile(text_path):
            raise ValueError(
                "No se encontró el archivo de texto referenciado en el paquete combo."
            )

        descriptor_source = self._resolve_combo_value(
            descriptor_data, ("source_name", "name", "title")
        )
        resolved_source = descriptor_source or source_name

        return {
            "descriptor_path": descriptor_path,
            "pdf_path": pdf_path,
            "text_path": text_path,
            "source_name": resolved_source,
        }

    def _cleanup_combo_resources(self, file_path: str) -> None:
        if os.path.isdir(file_path):
            try:
                shutil.rmtree(file_path, ignore_errors=True)
            except Exception:
                logging.warning(
                    "No se pudo eliminar el directorio temporal %s", file_path, exc_info=True
                )
            return

        descriptor_path = os.path.abspath(file_path)
        descriptor_dir = os.path.dirname(descriptor_path) or "."
        descriptor_dir = os.path.abspath(descriptor_dir)

        try:
            with open(descriptor_path, "r", encoding="utf-8") as fh:
                descriptor_data = json.load(fh)
        except Exception:
            descriptor_data = {}

        candidates = [descriptor_path]
        if isinstance(descriptor_data, dict):
            pdf_value = self._resolve_combo_value(
                descriptor_data, ("pdf_path", "pdf", "pdfFile", "pdf_file")
            )
            text_value = self._resolve_combo_value(
                descriptor_data,
                ("text_path", "text", "txt", "textFile", "text_file"),
            )
            for raw_value in (pdf_value, text_value):
                resolved = self._make_absolute(raw_value, descriptor_dir)
                if not resolved:
                    continue
                try:
                    common = os.path.commonpath([descriptor_dir, resolved])
                except ValueError:
                    common = None
                if common and common == descriptor_dir:
                    candidates.append(resolved)

        cleaned: Set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in cleaned:
                continue
            cleaned.add(candidate)
            try:
                if os.path.isdir(candidate):
                    shutil.rmtree(candidate, ignore_errors=True)
                elif os.path.exists(candidate):
                    os.remove(candidate)
            except Exception:
                logging.warning(
                    "No se pudo eliminar el recurso temporal %s", candidate, exc_info=True
                )

    def _run_ingest(
        self,
        responder: "CatalogResponder",
        file_path: str,
        source_name: str,
        file_type: str,
    ) -> ExecutorResult:
        normalized_type = (file_type or "").strip().lower()
        if normalized_type == "combo":
            payload = self._prepare_combo_payload(file_path, source_name)
            text_path = payload["text_path"]
            pdf_path = payload["pdf_path"]
            resolved_source = payload.get("source_name") or source_name
            stats = responder.ingest_text_with_pdf_images(
                text_path,
                pdf_path,
                source_name=resolved_source,
            )
            return {
                "stats": stats,
            }

        stats = responder.ingest_document(
            file_path, source_name=source_name, file_type=file_type
        )
        return {
            "stats": stats,
        }

    def _on_done(
        self,
        future: Future[ExecutorResult],
        file_path: str,
        source_name: str,
        file_type: str,
    ) -> None:
        error: Optional[str] = None
        stats: Optional[Dict[str, object]] = None
        try:
            result = future.result()
            stats = result.get("stats") if isinstance(result, dict) else None
        except Exception as exc:  # pragma: no cover - logging defensivo
            logging.exception("Error al procesar el catálogo %s", source_name)
            error = str(exc)
        finally:
            try:
                if (file_type or "").strip().lower() == "combo":
                    self._cleanup_combo_resources(file_path)
                elif os.path.exists(file_path):
                    os.remove(file_path)
            except Exception:
                logging.warning(
                    "No se pudo eliminar el archivo temporal %s", file_path, exc_info=True
                )

        with self._lock:
            self._status.update(
                {
                    "state": "failed" if error else "succeeded",
                    "finished_at": datetime.utcnow().isoformat(),
                    "error": error,
                    "stats": stats,
                    "file_type": file_type,
                }
            )


_state = _CatalogIngestState()


def start_catalog_ingest(
    responder: "CatalogResponder",
    file_path: str,
    source_name: str,
    file_type: str,
) -> None:
    """Encola la ingesta del catálogo en un hilo de fondo."""
    _state.start_job(responder, file_path, source_name, file_type)


def get_catalog_ingest_status() -> Dict[str, object]:
    """Devuelve una copia del estado actual del trabajo de ingesta."""
    return _state.get_status()
