"""Utilidades para procesar catálogos en PDF y consultarlos desde la IA."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from threading import Event
from pathlib import Path
from typing import List

try:  # pragma: no cover - dependencia opcional
    import pytesseract
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - se usa sólo si está disponible
    pytesseract = None  # type: ignore
    Image = None  # type: ignore
    ImageOps = None  # type: ignore

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
from services import ia_client
from services.db import replace_catalog_pages, search_catalog_pages

logger = logging.getLogger(__name__)


@dataclass
class CatalogPage:
    page_number: int
    text_content: str
    image_filename: str
    keywords: list[str] | None = None
    pdf_filename: str | None = None


class CatalogIngestCancelled(Exception):
    """Señala que la ingesta del catálogo fue cancelada."""


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


def _normalize_catalog_text(text: str) -> str:
    """Normaliza y corrige posibles errores de OCR en textos del catálogo."""

    return _fix_ocr_confusions(_sanitize_text(text))


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _catalog_use_openai() -> bool:
    return _coerce_bool(
        tenants.get_runtime_setting(
            "IA_CATALOG_USE_OPENAI",
            default=Config.IA_CATALOG_USE_OPENAI,
        )
    )


def _catalog_max_bytes() -> int:
    raw = tenants.get_runtime_setting(
        "IA_CATALOG_MAX_FILE_MB",
        default=Config.IA_CATALOG_MAX_FILE_MB,
    )
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = Config.IA_CATALOG_MAX_FILE_MB
    return max(mb, 1) * 1024 * 1024


def _catalog_request_delay_seconds() -> float:
    raw = tenants.get_runtime_setting(
        "IA_CATALOG_REQUEST_DELAY_SECONDS",
        default=Config.IA_CATALOG_REQUEST_DELAY_SECONDS,
    )
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = Config.IA_CATALOG_REQUEST_DELAY_SECONDS
    return max(delay, 0.0)


def _sleep_catalog_delay(delay_seconds: float) -> None:
    if delay_seconds > 0:
        time.sleep(delay_seconds)


def _extract_json_payload(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _split_pdf_by_size(
    pdf_path: str, *, max_bytes: int, output_dir: str, max_pages: int = 5
) -> list[tuple[str, int, int]]:
    doc = fitz.open(pdf_path)
    chunks: list[tuple[str, int, int]] = []
    current_doc = fitz.open()
    current_start = 1
    check_path = os.path.join(output_dir, "__check.pdf")

    try:
        for page_index in range(doc.page_count):
            if current_doc.page_count == 0:
                current_start = page_index + 1
            current_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
            current_doc.save(check_path)
            if (
                os.path.getsize(check_path) <= max_bytes
                and (max_pages <= 0 or current_doc.page_count <= max_pages)
            ):
                continue

            if current_doc.page_count == 1:
                raise ValueError(
                    f"La página {page_index + 1} excede el tamaño máximo permitido."
                )

            current_doc.delete_page(current_doc.page_count - 1)
            chunk_path = os.path.join(
                output_dir, f"catalog_part_{len(chunks) + 1}.pdf"
            )
            current_doc.save(chunk_path)
            chunks.append(
                (chunk_path, current_start, current_start + current_doc.page_count - 1)
            )
            current_doc.close()
            current_doc = fitz.open()
            current_start = page_index + 1
            current_doc.insert_pdf(doc, from_page=page_index, to_page=page_index)
            current_doc.save(check_path)
            if (
                os.path.getsize(check_path) > max_bytes
                or (max_pages > 0 and current_doc.page_count > max_pages)
            ):
                raise ValueError(
                    f"La página {page_index + 1} excede el tamaño máximo permitido."
                )

        if current_doc.page_count:
            chunk_path = os.path.join(
                output_dir, f"catalog_part_{len(chunks) + 1}.pdf"
            )
            current_doc.save(chunk_path)
            chunks.append(
                (chunk_path, current_start, current_start + current_doc.page_count - 1)
            )
    finally:
        current_doc.close()
        doc.close()
        if os.path.exists(check_path):
            os.remove(check_path)

    return chunks


def _prompt_for_catalog_range(start_page: int, end_page: int) -> str:
    return (
        f"Extrae el texto de las páginas {start_page} a {end_page} del archivo.\n"
        "Corrige errores ortográficos detectados.\n"
        "Devuelve exactamente lo detectado por página.\n"
        "Formato JSON:\n"
        '{"pages":[{"page":1,"content":"..."}]}'
    )


def _ingest_catalog_with_openai(pdf_path: str) -> dict[int, str]:
    if fitz is None:
        return {}
    if not _catalog_use_openai():
        return {}

    try:
        ia_client.get_api_key()
    except RuntimeError:
        return {}

    max_bytes = _catalog_max_bytes()
    text_by_page: dict[int, str] = {}

    delay_seconds = _catalog_request_delay_seconds()

    with tempfile.TemporaryDirectory(prefix="catalog_chunks_") as temp_dir:
        try:
            chunks = _split_pdf_by_size(pdf_path, max_bytes=max_bytes, output_dir=temp_dir)
        except Exception as exc:
            logger.warning("No se pudo dividir el PDF para OpenAI", exc_info=exc)
            return {}

        for chunk_path, start_page, end_page in chunks:
            file_id = ia_client.upload_file(chunk_path, purpose="user_data")
            _sleep_catalog_delay(delay_seconds)
            if not file_id:
                logger.warning(
                    "No se pudo subir un chunk del catálogo",
                    extra={"pdf": pdf_path, "range": f"{start_page}-{end_page}"},
                )
                continue
            prompt = _prompt_for_catalog_range(start_page, end_page)
            response_text = ia_client.create_response_with_file(file_id, prompt)
            _sleep_catalog_delay(delay_seconds)
            if not response_text:
                logger.warning(
                    "Respuesta vacía para chunk del catálogo",
                    extra={"range": f"{start_page}-{end_page}"},
                )
                continue

            payload = _extract_json_payload(response_text)
            if not payload:
                logger.warning(
                    "Respuesta de catálogo sin JSON válido",
                    extra={"range": f"{start_page}-{end_page}"},
                )
                continue

            pages = payload.get("pages") if isinstance(payload, dict) else None
            if not isinstance(pages, list):
                logger.warning(
                    "Respuesta de catálogo sin lista de páginas",
                    extra={"range": f"{start_page}-{end_page}"},
                )
                continue

            for page_entry in pages:
                if not isinstance(page_entry, dict):
                    continue
                page_number = page_entry.get("page")
                content = page_entry.get("content")
                if not isinstance(page_number, int):
                    continue
                if not isinstance(content, str):
                    content = ""
                if page_number < start_page or page_number > end_page:
                    continue
                text_by_page[page_number] = _sanitize_text(content)

    return text_by_page


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


def _fix_ocr_confusions(text: str) -> str:
    """Corrige errores comunes de OCR en textos de catálogos."""

    if not text:
        return ""

    letter_pattern = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]")
    parts = re.split(r"(\s+)", text)
    fixed_parts: list[str] = []

    for part in parts:
        if part.isspace() or "1" not in part:
            fixed_parts.append(part)
            continue

        if part.isalnum():
            first_digit_index = next(
                (idx for idx, char in enumerate(part) if char.isdigit()),
                None,
            )
            if first_digit_index is not None:
                prefix = part[:first_digit_index]
                suffix = part[first_digit_index:]
                if (
                    len(prefix) >= 2
                    and prefix.isalpha()
                    and suffix.isdigit()
                    and suffix.startswith("1")
                ):
                    part = f"{prefix}I{suffix[1:]}"

        letters = letter_pattern.findall(part)
        if len(letters) < 2:
            fixed_parts.append(part)
            continue

        part_without_ones = part.replace("1", "")
        if re.search(r"\d", part_without_ones):
            fixed_parts.append(part)
            continue

        fixed_parts.append(part.replace("1", "I"))

    return "".join(fixed_parts)


def _prepare_image_for_ocr(img: "Image.Image") -> "Image.Image":
    """Normaliza la imagen para mejorar el OCR con Tesseract."""

    prepared = img.convert("L")
    if ImageOps:
        prepared = ImageOps.autocontrast(prepared)
    return prepared


def _ensure_min_height(img: "Image.Image", min_height: int) -> "Image.Image":
    """Escala la imagen si es muy pequeña para mejorar la lectura OCR."""

    if min_height <= 0:
        return img

    width, height = img.size
    if height >= min_height:
        return img

    scale = min_height / max(height, 1)
    target_width = max(int(width * scale), 1)
    return img.resize((target_width, min_height), resample=Image.LANCZOS)


def _build_tesseract_config() -> str:
    """Construye la configuración de Tesseract desde variables de entorno."""

    parts = []
    psm = str(Config.OCR_PSM).strip()
    if psm:
        parts.append(f"--psm {psm}")
    oem = str(Config.OCR_OEM).strip()
    if oem:
        parts.append(f"--oem {oem}")
    return " ".join(parts)


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
        prepared = _prepare_image_for_ocr(img)
        prepared = _ensure_min_height(prepared, Config.OCR_MIN_HEIGHT)
        return pytesseract.image_to_string(
            prepared,
            lang=Config.OCR_LANG,
            config=_build_tesseract_config(),
        )
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
            prepared = _prepare_image_for_ocr(img)
            prepared = _ensure_min_height(prepared, Config.OCR_MIN_HEIGHT)
            return pytesseract.image_to_string(
                prepared,
                lang=Config.OCR_LANG,
                config=_build_tesseract_config(),
            )
    except Exception as exc:  # pragma: no cover - depende del runtime
        logger.warning("Fallo al ejecutar OCR de imagen de catálogo", exc_info=exc)
        return ""


def extract_text_from_image(path: str) -> str:
    """Extrae texto OCR desde una imagen del usuario si hay soporte disponible."""

    return _perform_ocr_from_image(path)


def _page_zoom_matrix(page: "fitz.Page", max_dim: int = 3000) -> "fitz.Matrix":
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


def ingest_catalog_pdf(
    pdf_path: str, stored_pdf_name: str, *, stop_event: Event | None = None
) -> list[CatalogPage]:
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

    openai_pages: dict[int, str] = {}
    if _catalog_use_openai():
        openai_pages = _ingest_catalog_with_openai(pdf_path)
        if openai_pages:
            logger.info(
                "Texto del catálogo obtenido vía OpenAI",
                extra={"pages": len(openai_pages)},
            )

    if stop_event and stop_event.is_set():
        raise CatalogIngestCancelled("Cancelado antes de abrir el PDF.")

    doc = fitz.open(pdf_path)
    page_counter = {"total": 0}

    def _page_iterator():
        if stop_event and stop_event.is_set():
            raise CatalogIngestCancelled("Cancelado antes de procesar páginas.")
        for page in doc:
            if stop_event and stop_event.is_set():
                raise CatalogIngestCancelled("Cancelado durante la ingesta del catálogo.")
            number = page.number + 1
            text = openai_pages.get(number, "")
            if not text:
                text = page.get_text("text") or ""
            if not text:
                text = page.get_text("blocks") or ""

            zoom_matrix = _page_zoom_matrix(page, max_dim=Config.OCR_MAX_DIM)
            pix = page.get_pixmap(matrix=zoom_matrix, alpha=False)
            image_name = f"{base_name}_p{number}.png"
            image_path = os.path.join(pages_dir, image_name)
            pix.save(image_path)

            if not text:
                text = _perform_ocr(pix)
            if text:
                text = _normalize_catalog_text(text)

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
