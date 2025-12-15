"""Utilidades para procesar catálogos en PDF y consultarlos desde la IA."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

if importlib.util.find_spec("fitz"):
    import fitz  # type: ignore  # PyMuPDF
else:  # pragma: no cover - dependencia opcional en tests
    fitz = None  # type: ignore

from config import Config
from services import tenants
from services.db import replace_catalog_pages, search_catalog_pages

logger = logging.getLogger(__name__)


@dataclass
class CatalogPage:
    page_number: int
    text_content: str
    image_filename: str
    pdf_filename: str | None = None


def _media_root() -> str:
    root = tenants.get_runtime_setting("MEDIA_ROOT", default=Config.MEDIA_ROOT)
    os.makedirs(root, exist_ok=True)
    return root


def _pages_root() -> str:
    root = os.path.join(_media_root(), "ia_pages")
    os.makedirs(root, exist_ok=True)
    return root


def _sanitize_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _page_zoom_matrix(page: "fitz.Page", max_dim: int = 2000) -> "fitz.Matrix":
    """Calcula una matriz de zoom que evita imágenes gigantes.

    Para catálogos con páginas muy grandes el factor de zoom fijo podía generar
    bitmaps de varios cientos de MB y agotar la memoria. Se limita la dimensión
    máxima a ``max_dim`` píxeles manteniendo la proporción.
    """

    # Evita división por cero en documentos malformados.
    width = max(page.rect.width, 1)
    height = max(page.rect.height, 1)
    scale = min(max_dim / width, max_dim / height, 2.0)
    # Si el PDF es pequeño se mantiene la resolución original o se sube hasta
    # un factor de 2x; en páginas gigantes se reduce para proteger la memoria.
    scale = max(scale, 0.2)
    return fitz.Matrix(scale, scale)


def ingest_catalog_pdf(pdf_path: str, stored_pdf_name: str) -> list[CatalogPage]:
    """Procesa el PDF y devuelve una lista de páginas con texto e imagen.

    - Extrae el texto de cada página usando PyMuPDF (que puede realizar OCR
      embebido en la librería si el PDF no tiene texto reconocible).
    - Genera una imagen PNG por página y la guarda en ``static/uploads/ia_pages``.
    - Inserta las páginas de forma incremental para evitar desbordes de memoria
      con catálogos muy grandes. Devuelve una lista vacía para mantener la firma
      pública sin acumular todas las páginas en memoria.
    """

    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) no está instalado.")

    pages_dir = _pages_root()
    pdf_path_obj = Path(pdf_path)
    base_name = pdf_path_obj.stem or stored_pdf_name

    logger.info("Procesando catálogo PDF para IA", extra={"pdf": pdf_path})

    doc = fitz.open(pdf_path)
    page_counter = {"total": 0}

    def _page_iterator():
        for page in doc:
            number = page.number + 1
            text = _sanitize_text(page.get_text("text") or "")
            if not text:
                text = _sanitize_text(page.get_text("blocks") or "")

            zoom_matrix = _page_zoom_matrix(page)
            pix = page.get_pixmap(matrix=zoom_matrix, alpha=False)
            image_name = f"{base_name}_p{number}.png"
            image_path = os.path.join(pages_dir, image_name)
            pix.save(image_path)

            page_counter["total"] += 1
            yield CatalogPage(
                page_number=number,
                text_content=text,
                image_filename=image_name,
                pdf_filename=stored_pdf_name,
            )

    try:
        replace_catalog_pages(stored_pdf_name, _page_iterator(), media_root=_media_root())
    finally:
        doc.close()

    logger.info(
        "Catálogo indexado: %d páginas almacenadas",
        page_counter["total"],
        extra={"pdf": stored_pdf_name},
    )
    return []


def find_relevant_pages(query: str, limit: int = 3) -> List[CatalogPage]:
    """Busca páginas relevantes en el catálogo para un prompt dado."""

    results = search_catalog_pages(query, limit=limit)
    return [
        CatalogPage(
            page_number=row["page_number"],
            text_content=row.get("text_content") or "",
            image_filename=row.get("image_filename") or "",
            pdf_filename=row.get("pdf_filename"),
        )
        for row in results
    ]
