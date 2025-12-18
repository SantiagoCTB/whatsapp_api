"""Utilidades para procesar catálogos en PDF y consultarlos desde la IA."""

from __future__ import annotations

import importlib.util
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

try:  # pragma: no cover - dependencia opcional
    import pytesseract
    from PIL import Image
except Exception:  # pragma: no cover - se usa sólo si está disponible
    pytesseract = None  # type: ignore
    Image = None  # type: ignore

if importlib.util.find_spec("fitz"):
    try:
        import fitz  # type: ignore  # PyMuPDF
    except Exception as exc:  # pragma: no cover - dependencia opcional en tests
        # En Windows puede faltar la DLL subyacente de PyMuPDF; evitamos que
        # la importación falle al arrancar la aplicación.
        logging.getLogger(__name__).warning(
            "No se pudo cargar PyMuPDF (fitz). Funcionalidades de catálogo deshabilitadas: %s",
            exc,
        )
        fitz = None  # type: ignore
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
    keywords: list[str] | None = None
    pdf_filename: str | None = None


def _media_root() -> str:
    return tenants.get_media_root()


def _pages_root() -> str:
    root = os.path.join(_media_root(), "ia_pages")
    os.makedirs(root, exist_ok=True)
    return root


def _sanitize_text(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _extract_keywords(text: str, *, max_keywords: int = 20) -> list[str]:
    """Genera una lista de palabras clave simples a partir del texto plano."""

    stopwords = {
        "para",
        "con",
        "los",
        "las",
        "del",
        "por",
        "una",
        "uno",
        "que",
        "sin",
        "sus",
        "este",
        "esta",
        "estos",
        "estas",
        "muy",
    }
    tokens = [
        t
        for t in re.split(r"\W+", (text or "").lower())
        if len(t) > 3 and t not in stopwords
    ]
    freq: dict[str, int] = {}
    for token in tokens:
        freq[token] = freq.get(token, 0) + 1
    sorted_tokens = sorted(freq.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _ in sorted_tokens[:max_keywords]]


def _perform_ocr(pix: "fitz.Pixmap") -> str:
    """Ejecuta OCR sobre la imagen de la página si pytesseract está disponible."""

    if not pytesseract or not Image:
        logger.warning(
            "OCR no disponible: instala tesseract-ocr y pytesseract en el despliegue de Linux"
        )
        return ""

    try:
        mode = "RGB" if pix.n < 4 else "RGBA"
        img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        text = pytesseract.image_to_string(img, lang="spa+eng")
        return _sanitize_text(text)
    except Exception as exc:  # pragma: no cover - depende del runtime
        logger.warning("Fallo al ejecutar OCR de catálogo", exc_info=exc)
        return ""


def _perform_ocr_from_image(path: str) -> str:
    """Ejecuta OCR sobre una imagen almacenada en disco si es posible."""

    if not pytesseract or not Image:
        logger.warning(
            "OCR no disponible: instala tesseract-ocr y pytesseract en el despliegue de Linux"
        )
        return ""

    if not os.path.exists(path):
        logger.warning("No se encontró la imagen del catálogo para OCR", extra={"path": path})
        return ""

    try:
        with Image.open(path) as img:
            text = pytesseract.image_to_string(img, lang="spa+eng")
            return _sanitize_text(text)
    except Exception as exc:  # pragma: no cover - depende del runtime
        logger.warning("Fallo al ejecutar OCR de imagen de catálogo", exc_info=exc)
        return ""


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

            if not text:
                text = _perform_ocr(pix)

            keywords = _extract_keywords(text)

            page_counter["total"] += 1
            yield CatalogPage(
                page_number=number,
                text_content=text,
                image_filename=image_name,
                keywords=keywords,
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
    """Busca páginas relevantes en el catálogo para un prompt dado.

    Prioriza el catálogo del tenant activo, pero si no hay coincidencias
    intenta un segundo pase contra el catálogo sin tenant (o el default)
    para evitar respuestas vacías cuando el PDF se indexó fuera de contexto.
    """

    active_tenant = tenants.get_active_tenant_key(include_default=False)
    default_tenant = (Config.DEFAULT_TENANT or "").strip() or None

    def _hydrate(rows: list[dict]) -> list[CatalogPage]:
        hydrated: list[CatalogPage] = []
        for row in rows:
            text = row.get("text_content") or ""
            keywords = row.get("keywords") if isinstance(row, dict) else None
            if (not text or not keywords) and row.get("image_filename"):
                image_path = os.path.join(_pages_root(), row["image_filename"])
                ocr_text = _perform_ocr_from_image(image_path)
                if ocr_text:
                    text = text or ocr_text
                    keywords = keywords or _extract_keywords(ocr_text)

            hydrated.append(
                CatalogPage(
                    page_number=row["page_number"],
                    text_content=text,
                    image_filename=row.get("image_filename") or "",
                    keywords=keywords,
                    pdf_filename=row.get("pdf_filename"),
                )
            )

        return hydrated

    # 1) Tenant activo (o default si no hay activo)
    target_tenant = active_tenant or default_tenant
    results = search_catalog_pages(
        query,
        limit=limit,
        tenant_key=target_tenant,
        fallback_to_default=False,
    )

    if results:
        logger.debug(
            "Páginas de catálogo encontradas para IA",
            extra={
                "tenant": active_tenant,
                "page_numbers": [r.get("page_number") for r in results],
                "pdfs": [r.get("pdf_filename") for r in results],
                "source_tenants": [r.get("tenant_key") for r in results],
            },
        )
        return _hydrate(results)

    logger.warning(
        "Sin coincidencias en catálogo para el prompt dado",
        extra={"tenant": active_tenant, "query": query[:120]},
    )

    # 2) Fallback controlado: entradas globales o del DEFAULT_TENANT
    fallback_results = search_catalog_pages(
        query,
        limit=limit,
        tenant_key=None,
        fallback_to_default=True,
    )

    if fallback_results:
        logger.info(
            "Catálogo IA: usando fallback sin tenant para evitar respuestas vacías",
            extra={
                "tenant": active_tenant,
                "tokens": query[:120],
                "source_tenants": [r.get("tenant_key") for r in fallback_results],
            },
        )
        return _hydrate(fallback_results)

    return []
