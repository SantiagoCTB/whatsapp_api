"""Genera imagen PNG top-down de un bus y la lista interactiva de asientos disponibles.

La imagen muestra: capó redondeado, parabrisas, ventanas laterales, conductor y
asientos en layout 2+2 con pasillo central. Optimizada para visualización en celular.

El PNG se sube a S3 si AWS_S3_BUCKET está configurado; de lo contrario se guarda
en static/uploads/<tenant>/ y se sirve vía la URL pública de la app.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Paleta (tema claro, alto contraste para móvil) ───────────────────────────
BG_IMG      = (248, 250, 253)  # fondo principal
CARD_BG     = (255, 255, 255)  # fondo del bus
CARD_BORDER = (203, 213, 225)  # borde del bus
HEADER_BG   = ( 30,  64, 175)  # azul fuerte header
AISLE_BG    = (241, 245, 249)  # franja del pasillo
DRIVER_BG   = ( 51,  65,  85)  # zona conductor
ROW_LABEL   = (148, 163, 184)  # número de fila
TEXT_DARK   = ( 17,  24,  39)  # texto oscuro
TEXT_LIGHT  = (255, 255, 255)
TEXT_MUTED  = (100, 116, 139)

C_LIBRE     = ( 22, 163,  74)  # verde libre
C_LIBRE_B   = ( 15, 118,  53)
C_PREF      = (126,  34, 206)  # violeta preferencial
C_PREF_B    = ( 88,  28, 135)
C_VIP       = (217, 119,   6)  # ámbar VIP
C_VIP_B     = (146,  64,  14)
C_OCUPADO   = (220,  38,  38)  # rojo ocupado
C_OCUPADO_B = (153,  27,  27)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

SEAT_MAP_MAX_AGE = 7_200  # 2 horas

# ── Dimensiones (optimizadas para celular) ────────────────────────────────────
SEAT_W      = 78        # silla
SEAT_H      = 78
SEAT_GAP    = 10        # entre sillas del mismo lado
AISLE_W     = 56        # ancho del pasillo
ROW_GAP     = 14        # entre filas
ROW_LABEL_W = 38        # columna de número de fila
PAD_X       = 22        # padding horizontal del card
PAD_TOP     = 22        # padding superior interno
PAD_BOT     = 28        # padding inferior interno
DRIVER_H    = 56
CORNER_R    = 14
CARD_R      = 18
SEAT_R      = 12

IMG_W       = 560  # fallback


# ── Helpers de dibujo ─────────────────────────────────────────────────────────

def _fonts():
    from PIL import ImageFont
    try:
        return {
            "title":    ImageFont.truetype(FONT_BOLD, 24),
            "sub":      ImageFont.truetype(FONT_REG,  16),
            "tag":      ImageFont.truetype(FONT_BOLD, 13),
            "seat":     ImageFont.truetype(FONT_BOLD, 30),
            "occupied": ImageFont.truetype(FONT_BOLD, 32),
            "row":      ImageFont.truetype(FONT_BOLD, 16),
            "legend":   ImageFont.truetype(FONT_BOLD, 14),
            "label":    ImageFont.truetype(FONT_BOLD, 13),
        }
    except Exception:
        d = ImageFont.load_default()
        return {k: d for k in ("title","sub","tag","seat","occupied","row","legend","label")}


def _rrect(draw, x0, y0, x1, y1, r, fill=None, outline=None, width=1):
    """Rectángulo redondeado usando draw.rounded_rectangle (Pillow >= 8)."""
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill,
                           outline=outline, width=width)


def _ctext(draw, cx, cy, text, font, fill):
    bb = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (bb[2] - bb[0]) // 2, cy - (bb[3] - bb[1]) // 2 - bb[1]),
              text, font=font, fill=fill)


def _draw_seat(draw, x0, y0, x1, y1, color, border, num: str, occupied: bool, fonts):
    """Dibuja una silla con sombra sutil, número grande y X si está ocupada."""
    # Sombra
    _rrect(draw, x0 + 2, y0 + 3, x1 + 2, y1 + 3, SEAT_R, fill=(0, 0, 0, 30) if False else (220, 220, 230))
    # Cuerpo
    _rrect(draw, x0, y0, x1, y1, SEAT_R, fill=color, outline=border, width=2)
    # Respaldo (banda superior más oscura)
    _rrect(draw, x0 + 6, y0 + 5, x1 - 6, y0 + 14, 4, fill=border)
    # Texto
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2 + 4
    if occupied:
        _ctext(draw, cx, cy, "✕", fonts["occupied"], TEXT_LIGHT)
    else:
        _ctext(draw, cx, cy, num, fonts["seat"], TEXT_LIGHT)


# ── Datos de asiento ─────────────────────────────────────────────────────────

def _seat_color(seat: dict) -> tuple:
    """Retorna (color_relleno, color_borde) para una silla."""
    tipo  = (seat.get("type",  "") or "").lower()
    state = (seat.get("state", "") or "").lower()
    libre = "disponible" in state
    if not libre:
        return C_OCUPADO, C_OCUPADO_B
    if tipo == "preferencial":
        return C_PREF, C_PREF_B
    if tipo == "vip":
        return C_VIP, C_VIP_B
    return C_LIBRE, C_LIBRE_B


def _split_groups(seats: list):
    """Devuelve (left_cols, right_cols) excluyendo columnas Pasillo/Conductor.

    Soporta layouts 1+1, 2+2 y minibuses con columna del medio mixta
    (e.g. Silla en algunas filas y Pasillo en otras).
    """
    if not seats:
        return [], []
    n_cols = max(len(r) for r in seats)
    if n_cols == 0:
        return [], []

    # Caso 1: existe alguna columna que es SIEMPRE pasillo/conductor → aisle clásico
    aisle = {
        c for c in range(n_cols)
        if all(
            (c >= len(row)) or row[c].get("type", "").lower() in ("pasillo", "conductor")
            for row in seats
        )
    }
    if aisle:
        first_a = min(aisle)
        last_a  = max(aisle)
        left  = [c for c in range(first_a)           if c not in aisle]
        right = [c for c in range(last_a + 1, n_cols) if c not in aisle]
        return left, right

    # Caso 2: no hay columna 100% pasillo (ej. minibús donde la columna del lado
    # tiene asiento en la fila delantera/trasera pero pasillo en el resto).
    # Votamos: la columna con más apariciones de pasillo es el límite del pasillo.
    pasillo_count = [0] * n_cols
    for row in seats:
        for c, seat in enumerate(row):
            if seat.get("type", "").lower() in ("pasillo", "conductor"):
                pasillo_count[c] += 1

    best_col = max(range(n_cols), key=lambda c: pasillo_count[c])
    if pasillo_count[best_col] > 0:
        # ¿Esa columna tiene también asientos reales? → incluirla en el grupo derecho
        has_real_seat = any(
            best_col < len(row)
            and row[best_col].get("type", "").lower() not in ("pasillo", "conductor")
            and str(row[best_col].get("number", 0)) not in ("0", "", "None")
            for row in seats
        )
        left  = list(range(best_col))
        right = ([best_col] if has_real_seat else []) + list(range(best_col + 1, n_cols))
        return left, right

    # Caso 3: sin información de pasillo → split en el medio
    mid = n_cols // 2
    return list(range(mid)), list(range(mid, n_cols))


def _compute_img_width(left_cols: list, right_cols: list) -> tuple[int, int, int]:
    """Calcula ancho de imagen y posiciones X iniciales según el layout real.

    Layout horizontal (de izq a der):
      PAD_X | ROW_LABEL_W | left_seats | AISLE_W | right_seats | PAD_X

    Retorna (img_w, left_x0, right_x0).
    """
    n_left  = max(len(left_cols),  1)
    n_right = max(len(right_cols), 1)
    left_w  = n_left  * SEAT_W + max(0, n_left  - 1) * SEAT_GAP
    right_w = n_right * SEAT_W + max(0, n_right - 1) * SEAT_GAP
    left_x0  = PAD_X + ROW_LABEL_W
    right_x0 = left_x0 + left_w + AISLE_W
    img_w    = right_x0 + right_w + PAD_X
    # Mínimo razonable para móvil
    if img_w < 480:
        img_w = 480
    return img_w, left_x0, right_x0


# ── Generador principal ───────────────────────────────────────────────────────

def generate_seat_map_image(
    seats: list[list[dict]],
    bearing_id: int | str = "",
    route_name: str = "",
    departure: str = "",
) -> bytes:
    """Genera PNG limpio del mapa de sillas, optimizado para WhatsApp/celular.

    Diseño:
      1. Header azul con ruta y hora de salida
      2. Tarjeta blanca con FRENTE arriba, sillas con número de fila al lado,
         pasillo central marcado, y TRASERA abajo
      3. Leyenda de colores fuera de la tarjeta
    """
    from PIL import Image, ImageDraw

    fonts = _fonts()
    left_cols, right_cols = _split_groups(seats)
    img_w, left_x0, right_x0 = _compute_img_width(left_cols, right_cols)

    # ── Filas de tipo conductor (solo pasillo/conductor) y filas de sillas ─────
    def _is_driver_row(row: list[dict]) -> bool:
        return all(s.get("type", "").lower() in ("conductor", "pasillo") for s in row)

    seat_rows = [row for row in seats if not _is_driver_row(row)]
    has_driver_row = any(_is_driver_row(row) for row in seats)
    n_seat_rows = len(seat_rows)

    # Disponibles (para chip en header)
    n_disponibles = sum(
        1 for row in seat_rows for s in row
        if (s.get("type", "").lower() == "silla"
            or s.get("type", "").lower() in ("preferencial", "vip"))
        and "disponible" in (s.get("state", "") or "").lower()
        and str(s.get("number", "0")) not in ("0", "", "None")
    )
    n_total = sum(
        1 for row in seat_rows for s in row
        if (s.get("type", "").lower() in ("silla", "preferencial", "vip"))
        and str(s.get("number", "0")) not in ("0", "", "None")
    )

    # ── Cálculo de alturas ────────────────────────────────────────────────────
    has_header = bool(route_name or departure)
    header_h   = 96 if has_header else 0

    front_h    = 38
    driver_h   = (DRIVER_H + 14) if has_driver_row else 0
    grid_h     = max(0, n_seat_rows * (SEAT_H + ROW_GAP) - ROW_GAP)
    rear_h     = 32
    card_h     = front_h + PAD_TOP + driver_h + grid_h + PAD_BOT + rear_h

    legend_h   = 64
    margin_top_card = 10
    margin_bot_card = 14
    total_h = header_h + margin_top_card + card_h + margin_bot_card + legend_h

    # ── Canvas ────────────────────────────────────────────────────────────────
    img  = Image.new("RGB", (img_w, total_h), BG_IMG)
    draw = ImageDraw.Draw(img)

    # ── 1. Header ─────────────────────────────────────────────────────────────
    if has_header:
        draw.rectangle([0, 0, img_w, header_h], fill=HEADER_BG)
        hy = 18
        if route_name:
            _ctext(draw, img_w // 2, hy + 12, route_name, fonts["title"], TEXT_LIGHT)
            hy += 38
        if departure:
            _ctext(draw, img_w // 2, hy + 10, f"Salida {departure}",
                   fonts["sub"], (200, 215, 240))
            hy += 26

        # Chip de disponibilidad
        if n_total > 0:
            chip_txt = f"  {n_disponibles} de {n_total} sillas disponibles  "
            bb = draw.textbbox((0, 0), chip_txt, font=fonts["tag"])
            cw = bb[2] - bb[0]
            cx = img_w // 2
            chip_y = header_h - 22
            _rrect(draw, cx - cw//2 - 2, chip_y - 2,
                   cx + cw//2 + 2, chip_y + 18, 10, fill=(255, 255, 255))
            _ctext(draw, cx, chip_y + 8, chip_txt.strip(), fonts["tag"], HEADER_BG)

    # ── 2. Tarjeta del bus ────────────────────────────────────────────────────
    card_x0 = 14
    card_x1 = img_w - 14
    card_y0 = header_h + margin_top_card
    card_y1 = card_y0 + card_h
    _rrect(draw, card_x0, card_y0, card_x1, card_y1, CARD_R,
           fill=CARD_BG, outline=CARD_BORDER, width=2)

    # Etiqueta FRENTE (con flecha)
    _ctext(draw, img_w // 2, card_y0 + 18, "▲  FRENTE  ▲",
           fonts["label"], TEXT_MUTED)

    # Línea separadora bajo "FRENTE"
    sep_y = card_y0 + front_h - 2
    draw.line([(card_x0 + 24, sep_y), (card_x1 - 24, sep_y)],
              fill=CARD_BORDER, width=1)

    # Zona conductor (si hay fila 100% conductor/pasillo)
    cur_y = card_y0 + front_h + 8
    if has_driver_row:
        dy0 = cur_y
        dy1 = dy0 + DRIVER_H
        _rrect(draw, card_x0 + 18, dy0, card_x1 - 18, dy1, 12, fill=DRIVER_BG)
        _ctext(draw, img_w // 2, (dy0 + dy1) // 2, "Conductor",
               fonts["sub"], TEXT_LIGHT)
        cur_y = dy1 + 12

    grid_y0 = cur_y + (PAD_TOP - 8 if not has_driver_row else 0)

    # Franja del pasillo (gris claro vertical)
    n_left  = max(len(left_cols), 1)
    aisle_x0 = left_x0 + n_left * SEAT_W + max(0, n_left - 1) * SEAT_GAP + 4
    aisle_x1 = aisle_x0 + AISLE_W - 8
    if right_cols:  # solo dibujar si hay sillas al otro lado
        _rrect(draw, aisle_x0, grid_y0 - 6,
               aisle_x1, grid_y0 + grid_h + 6, 10, fill=AISLE_BG)
        # Texto vertical "PASILLO" sutil
        if grid_h >= 120:
            _ctext(draw, (aisle_x0 + aisle_x1) // 2,
                   grid_y0 + grid_h // 2, "↕", fonts["row"], (180, 195, 215))

    # Render de las filas de sillas
    for row_idx, row in enumerate(seat_rows):
        y0 = grid_y0 + row_idx * (SEAT_H + ROW_GAP)
        y1 = y0 + SEAT_H

        # Número de fila a la izquierda
        _ctext(draw, card_x0 + 18 + ROW_LABEL_W // 2 - 4,
               (y0 + y1) // 2,
               str(row_idx + 1), fonts["row"], ROW_LABEL)

        # Sillas izquierda
        for i, col in enumerate(left_cols):
            seat = row[col] if col < len(row) else {}
            if not seat or seat.get("type", "").lower() in ("pasillo", "conductor"):
                continue
            num = str(seat.get("number", "") or "")
            if not num or num == "0":
                continue
            sx0 = left_x0 + i * (SEAT_W + SEAT_GAP)
            sx1 = sx0 + SEAT_W
            fill, border = _seat_color(seat)
            is_occ = "disponible" not in (seat.get("state", "") or "").lower()
            _draw_seat(draw, sx0, y0, sx1, y1, fill, border, num, is_occ, fonts)

        # Sillas derecha
        for i, col in enumerate(right_cols):
            seat = row[col] if col < len(row) else {}
            if not seat or seat.get("type", "").lower() in ("pasillo", "conductor"):
                continue
            num = str(seat.get("number", "") or "")
            if not num or num == "0":
                continue
            sx0 = right_x0 + i * (SEAT_W + SEAT_GAP)
            sx1 = sx0 + SEAT_W
            fill, border = _seat_color(seat)
            is_occ = "disponible" not in (seat.get("state", "") or "").lower()
            _draw_seat(draw, sx0, y0, sx1, y1, fill, border, num, is_occ, fonts)

    # Línea separadora antes de TRASERA
    sep2_y = card_y1 - rear_h + 4
    draw.line([(card_x0 + 24, sep2_y), (card_x1 - 24, sep2_y)],
              fill=CARD_BORDER, width=1)
    _ctext(draw, img_w // 2, card_y1 - 16, "▼  TRASERA  ▼",
           fonts["label"], TEXT_MUTED)

    # ── 3. Leyenda ────────────────────────────────────────────────────────────
    leg_items = [
        (C_LIBRE,   C_LIBRE_B,   "Libre"),
        (C_OCUPADO, C_OCUPADO_B, "Ocupado"),
        (C_PREF,    C_PREF_B,    "Preferencial"),
        (C_VIP,     C_VIP_B,     "VIP"),
    ]
    leg_y0 = card_y1 + margin_bot_card
    # Calcular ancho total para centrar
    box = 22
    gap_item = 18
    items_w = []
    for _, _, lbl in leg_items:
        bb = draw.textbbox((0, 0), lbl, font=fonts["legend"])
        items_w.append(box + 8 + (bb[2] - bb[0]))
    total_leg_w = sum(items_w) + gap_item * (len(leg_items) - 1)

    # Si no caben en una línea, distribuir en dos
    if total_leg_w > img_w - 24:
        # 2 + 2
        line1 = leg_items[:2]
        line2 = leg_items[2:]
        for line_idx, line in enumerate((line1, line2)):
            line_w = sum(items_w[i + line_idx*2] for i in range(len(line))) + gap_item * (len(line) - 1)
            lx = (img_w - line_w) // 2
            ly = leg_y0 + 8 + line_idx * 28
            for i, (fill, border, lbl) in enumerate(line):
                _rrect(draw, lx, ly, lx + box, ly + box, 5, fill=fill, outline=border, width=1)
                bb = draw.textbbox((0, 0), lbl, font=fonts["legend"])
                draw.text((lx + box + 8, ly + 3), lbl, font=fonts["legend"], fill=TEXT_DARK)
                lx += box + 8 + (bb[2] - bb[0]) + gap_item
    else:
        lx = (img_w - total_leg_w) // 2
        ly = leg_y0 + 20
        for i, (fill, border, lbl) in enumerate(leg_items):
            _rrect(draw, lx, ly, lx + box, ly + box, 5, fill=fill, outline=border, width=1)
            bb = draw.textbbox((0, 0), lbl, font=fonts["legend"])
            draw.text((lx + box + 8, ly + 3), lbl, font=fonts["legend"], fill=TEXT_DARK)
            lx += box + 8 + (bb[2] - bb[0]) + gap_item

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Lista interactiva de asientos disponibles ─────────────────────────────────

def build_available_seats_opciones(seats: list[list[dict]]) -> str | None:
    """
    Construye el JSON de opciones para un mensaje tipo 'lista' de WhatsApp
    con únicamente los asientos disponibles, agrupados por tipo.
    Retorna None si no hay ninguno disponible.
    """
    pref: list[dict]    = []
    vip: list[dict]     = []
    regular: list[dict] = []

    for row in seats:
        for seat in row:
            tipo  = (seat.get("type",  "") or "").lower()
            state = (seat.get("state", "") or "").lower()
            num   = seat.get("number", 0)

            if tipo in ("pasillo", "conductor") or not num or str(num) == "0":
                continue
            if "disponible" not in state:
                continue

            sid = str(num)
            entry = {"id": sid, "title": f"Silla {sid}"}
            if tipo == "preferencial":
                entry["description"] = "Preferencial"
                pref.append(entry)
            elif tipo == "vip":
                entry["description"] = "VIP"
                vip.append(entry)
            else:
                regular.append(entry)

    if not pref and not vip and not regular:
        return None

    def _num_key(entry: dict) -> int:
        return int(entry["id"]) if entry["id"].isdigit() else 0

    pref.sort(key=_num_key)
    vip.sort(key=_num_key)
    regular.sort(key=_num_key)

    # Una sola sección con todas las sillas disponibles ordenadas (pref → vip → regular)
    # WhatsApp acepta de forma confiable solo 1 sección; mostrar las primeras 10.
    all_seats = (pref + vip + regular)[:10]
    if not all_seats:
        return None

    sections: list[dict] = [{"title": "Sillas disponibles", "rows": all_seats}]

    return json.dumps({
        "sections": sections[:10],          # WhatsApp: máx 10 secciones
        "header":   "Selecciona tu silla",
        "footer":   "Solo sillas disponibles",
        "button":   "Ver sillas",
    }, ensure_ascii=False)


# ── Subida S3 (opcional) ──────────────────────────────────────────────────────

def _upload_to_s3(png_bytes: bytes, filename: str) -> str | None:
    """
    Sube el PNG a S3 y retorna la URL pública.
    Requiere env vars: AWS_S3_BUCKET, AWS_REGION (o AWS_DEFAULT_REGION).
    Opcional: CDN_DOMAIN para URL personalizada.
    Retorna None si S3 no está configurado o falla.
    """
    bucket = os.getenv("AWS_S3_BUCKET") or os.getenv("S3_BUCKET")
    if not bucket:
        return None
    try:
        import boto3  # type: ignore
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        s3 = boto3.client("s3", region_name=region)
        key = f"seat_maps/{filename}"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=png_bytes,
            ContentType="image/png",
            CacheControl="max-age=3600",
        )
        cdn = os.getenv("CDN_DOMAIN")
        url = f"https://{cdn}/{key}" if cdn else f"https://{bucket}.s3.{region}.amazonaws.com/{key}"
        logger.info("seat_map: subida a S3 → %s", url)
        return url
    except Exception as exc:
        logger.warning("seat_map: S3 no disponible (%s), usando almacenamiento local", exc)
        return None


# ── Almacenamiento y limpieza ─────────────────────────────────────────────────

def cleanup_old_seat_maps(uploads_dir: str, max_age: int = SEAT_MAP_MAX_AGE) -> int:
    """Elimina seat_map_*.png con más de `max_age` segundos en uploads_dir y subdirectorios."""
    now, removed = time.time(), 0
    try:
        dirs = [uploads_dir] + [e.path for e in os.scandir(uploads_dir) if e.is_dir()]
    except FileNotFoundError:
        return 0
    for d in dirs:
        try:
            for f in os.scandir(d):
                if f.is_file() and f.name.startswith("seat_map_") and f.name.endswith(".png"):
                    if now - f.stat().st_mtime > max_age:
                        os.remove(f.path)
                        removed += 1
        except (PermissionError, FileNotFoundError):
            pass
    if removed:
        logger.info("seat_map: %d imagen(es) eliminada(s)", removed)
    return removed


def save_seat_map(png_bytes: bytes, bearing_id: int | str) -> tuple[str, str | None]:
    """
    Intenta subir a S3; si no está configurado guarda localmente.
    Retorna (filename, public_url_or_None).
    public_url es la URL de S3/CDN si se subió, o None para usar la URL local.
    """
    from config import Config
    from services import tenants

    filename = f"seat_map_{bearing_id}_{int(time.time())}.png"

    # Intentar S3 primero
    s3_url = _upload_to_s3(png_bytes, filename)
    if s3_url:
        return filename, s3_url

    # Fallback: almacenamiento local
    cleanup_old_seat_maps(Config.MEDIA_ROOT)
    tenant_key = tenants.get_active_tenant_key() or "default"
    save_dir   = os.path.join(Config.MEDIA_ROOT, tenant_key)
    os.makedirs(save_dir, exist_ok=True)
    with open(os.path.join(save_dir, filename), "wb") as f:
        f.write(png_bytes)
    logger.info("seat_map: guardada localmente → %s/%s", tenant_key, filename)
    return filename, None


# ============================================================================
# GENERADOR DE MAPA DE BUS – vista aérea con secciones, precios y leyenda
# Uso:
#   buf = generar_mapa_bus(data)          → BytesIO listo para WhatsApp API
#   generar_mapa_bus(data, "mapa.png")    → guarda el archivo y retorna el path
#
# Formato de `data`:
#   { "viaje": {...}, "secciones": [...], "leyenda": {...} }
# Formato de `distribucion` en cada sección:
#   "2-2" → 2 sillas + pasillo + 2 sillas
#   "1-2" → 1 silla  + pasillo + 2 sillas (típico VIP/Cama)
#   "2-1" → 2 sillas + pasillo + 1 silla
#   "1-1" → 1 silla  + pasillo + 1 silla
# ============================================================================

# ── Configuración visual ──────────────────────────────────────────────────────
_MB_ANCHO_IMG     = 900
_MB_PADDING       = 40
_MB_TAM_SILLA     = 54
_MB_GAP_SILLA     = 10
_MB_GAP_FILA      = 12
_MB_AISLE_WIDTH   = 52
_MB_BUS_PAD_X     = 36
_MB_BUS_PAD_TOP   = 175
_MB_BUS_PAD_BOT   = 55
_MB_BUS_RADIUS    = 52

_MB_PALETA = {
    "fondo":            "#FFFFFF",
    "fondo_suave":      "#F8F9FA",
    "texto_principal":  "#2C3E50",
    "texto_secundario": "#7F8C8D",
    "borde":            "#E8ECEF",
    "acento":           "#3498DB",
    "bus_borde":        "#34495E",
    "bus_interior":     "#FDFDFD",
    "parabrisas":       "#BDD7ED",
    "parabrisas_borde": "#7FB3D5",
    "divisor":          "#D5DBDB",
    "sombra":           "#D0D0D0",
    "reflejo":          "#EAF4FB",
}


# ── Utilidades ────────────────────────────────────────────────────────────────

def _mb_fuente(tam, bold=False):
    candidatos = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for ruta in candidatos:
        try:
            return ImageFont.truetype(ruta, tam)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _mb_centrar(draw, texto, fuente, caja):
    x1, y1, x2, y2 = caja
    bbox = draw.textbbox((0, 0), texto, font=fuente)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return x1 + (x2 - x1 - w) / 2, y1 + (y2 - y1 - h) / 2 - bbox[1]


def _mb_aclarar(hex_color, factor=0.85):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return "#{:02X}{:02X}{:02X}".format(
        int(r + (255 - r) * factor),
        int(g + (255 - g) * factor),
        int(b + (255 - b) * factor),
    )


def _mb_parse_dist(dist):
    a, b = dist.split("-")
    return int(a), int(b)


def _mb_ancho_fila(dist):
    izq, der = _mb_parse_dist(dist)
    gaps = max(0, izq - 1) + max(0, der - 1)
    return (izq + der) * _MB_TAM_SILLA + gaps * _MB_GAP_SILLA + _MB_AISLE_WIDTH


def _mb_inferir_dist(seccion):
    if "distribucion" in seccion:
        return seccion["distribucion"]
    n = len(seccion["filas"][0]["sillas"])
    return {2: "1-1", 3: "2-1", 4: "2-2", 5: "2-3"}.get(n, f"{n//2}-{n-n//2}")


def _mb_formato_precio(precio, moneda):
    if moneda == "COP":
        return f"{moneda} ${precio:,.0f}".replace(",", ".")
    return f"{moneda} ${precio:,.2f}"


# ── Dibujo de sillas ──────────────────────────────────────────────────────────

def _mb_dibujar_silla(draw, x, y, silla, leyenda):
    color = leyenda[silla["estado"]]["color"]
    draw.rounded_rectangle(
        [(x + 1, y + 2), (x + _MB_TAM_SILLA + 1, y + _MB_TAM_SILLA + 2)],
        radius=11, fill=_MB_PALETA["sombra"],
    )
    draw.rounded_rectangle(
        [(x, y), (x + _MB_TAM_SILLA, y + _MB_TAM_SILLA)],
        radius=11, fill=color,
    )
    draw.rounded_rectangle(
        [(x + 7, y + 5), (x + _MB_TAM_SILLA - 7, y + 11)],
        radius=3, fill=_mb_aclarar(color, 0.4),
    )
    fuente = _mb_fuente(17, bold=True)
    num = str(silla["numero"])
    tx, ty = _mb_centrar(draw, num, fuente,
                         (x, y + 3, x + _MB_TAM_SILLA, y + _MB_TAM_SILLA + 3))
    draw.text((tx, ty), num, font=fuente, fill="#FFFFFF")


def _mb_dibujar_fila(draw, sillas, dist, x_bus, ancho_bus, y, leyenda):
    izq, der = _mb_parse_dist(dist)
    aw = _mb_ancho_fila(dist)
    x = x_bus + (ancho_bus - aw) / 2

    for i in range(izq):
        _mb_dibujar_silla(draw, x, y, sillas[i], leyenda)
        x += _MB_TAM_SILLA + (_MB_GAP_SILLA if i < izq - 1 else 0)

    pasillo_x = x + _MB_AISLE_WIDTH / 2
    for py in range(int(y + 8), int(y + _MB_TAM_SILLA - 8), 6):
        draw.line([(pasillo_x, py), (pasillo_x, py + 3)],
                  fill=_MB_PALETA["borde"], width=1)

    x += _MB_AISLE_WIDTH
    for i in range(der):
        _mb_dibujar_silla(draw, x, y, sillas[izq + i], leyenda)
        x += _MB_TAM_SILLA + (_MB_GAP_SILLA if i < der - 1 else 0)


# ── Dibujo del bus ────────────────────────────────────────────────────────────

def _mb_carroceria(draw, x, y, ancho, alto):
    draw.rounded_rectangle(
        [(x, y), (x + ancho, y + alto)],
        radius=_MB_BUS_RADIUS,
        outline=_MB_PALETA["bus_borde"], width=5,
        fill=_MB_PALETA["bus_interior"],
    )
    for fx in [x + 20, x + ancho - 28]:
        draw.ellipse([(fx, y + 6), (fx + 8, y + 14)],
                     fill="#F4D03F", outline="#B7950B", width=1)


def _mb_cabina(draw, x, y, ancho):
    margin = 28
    ws_y1 = y + 22
    ws_y2 = ws_y1 + 52
    draw.rounded_rectangle(
        [(x + margin, ws_y1), (x + ancho - margin, ws_y2)],
        radius=22, fill=_MB_PALETA["parabrisas"],
        outline=_MB_PALETA["parabrisas_borde"], width=2,
    )
    draw.polygon(
        [(x + margin + 14, ws_y1 + 8),
         (x + margin + 85, ws_y1 + 8),
         (x + margin + 55, ws_y2 - 8),
         (x + margin + 14, ws_y2 - 8)],
        fill=_MB_PALETA["reflejo"],
    )
    mid_x = x + ancho / 2
    draw.line([(mid_x, ws_y1 + 4), (mid_x, ws_y2 - 4)],
              fill=_MB_PALETA["parabrisas_borde"], width=2)

    # Volante (lado izquierdo – estándar Colombia)
    wsize = 38
    wx = x + margin + 10
    wy = ws_y2 + 14
    draw.ellipse([(wx, wy), (wx + wsize, wy + wsize)],
                 fill="#2C3E50", outline="#1A252F", width=2)
    inner = 12
    draw.ellipse([(wx + inner, wy + inner), (wx + wsize - inner, wy + wsize - inner)],
                 fill="#4A5F7A")
    cx, cy = wx + wsize / 2, wy + wsize / 2
    draw.line([(cx - 15, cy), (cx + 15, cy)], fill="#1A252F", width=2)
    draw.line([(cx, cy - 15), (cx, cy + 15)], fill="#1A252F", width=2)
    fuente = _mb_fuente(10, bold=True)
    draw.text((wx - 3, wy + wsize + 5), "CONDUCTOR",
              font=fuente, fill=_MB_PALETA["texto_secundario"])

    # Asiento copiloto (derecha)
    copi_size = 34
    copi_x = x + ancho - margin - copi_size - 6
    copi_y = ws_y2 + 16
    draw.rounded_rectangle(
        [(copi_x, copi_y), (copi_x + copi_size, copi_y + copi_size)],
        radius=7, fill="#E8ECEF", outline="#BDC3C7", width=1,
    )
    draw.rounded_rectangle(
        [(copi_x + 5, copi_y + 4), (copi_x + copi_size - 5, copi_y + 9)],
        radius=2, fill="#BDC3C7",
    )

    # Divisor cabina/pasajeros
    sep_y = ws_y2 + 88
    for sx in range(int(x + 32), int(x + ancho - 32), 10):
        draw.line([(sx, sep_y), (sx + 5, sep_y)], fill=_MB_PALETA["divisor"], width=2)


def _mb_trasera(draw, x, y, ancho):
    for sx in range(int(x + 32), int(x + ancho - 32), 10):
        draw.line([(sx, y), (sx + 5, y)], fill=_MB_PALETA["divisor"], width=2)
    fuente = _mb_fuente(10, bold=True)
    texto = "PARTE TRASERA"
    bbox = draw.textbbox((0, 0), texto, font=fuente)
    w = bbox[2] - bbox[0]
    tx = x + (ancho - w) / 2
    draw.rectangle([(tx - 8, y - 7), (tx + w + 8, y + 9)],
                   fill=_MB_PALETA["bus_interior"])
    draw.text((tx, y - 6), texto, font=fuente, fill=_MB_PALETA["texto_secundario"])


def _mb_puerta_lateral(draw, x, y, ancho):
    door_y1 = y + 95
    door_y2 = y + 145
    draw.rectangle([(x + ancho - 6, door_y1), (x + ancho + 2, door_y2)],
                   fill=_MB_PALETA["parabrisas"],
                   outline=_MB_PALETA["bus_borde"], width=2)
    draw.rectangle([(x + ancho - 4, door_y1 + 22), (x + ancho - 1, door_y1 + 28)],
                   fill=_MB_PALETA["bus_borde"])


def _mb_seccion_label(draw, seccion, x_bus, ancho_bus, y):
    color    = seccion["color"]
    color_bg = _mb_aclarar(color, 0.80)
    chip_margin = 34
    chip_x1 = x_bus + chip_margin
    chip_x2 = x_bus + ancho_bus - chip_margin
    chip_y1, chip_y2 = y, y + 30
    draw.rounded_rectangle([(chip_x1, chip_y1), (chip_x2, chip_y2)],
                            radius=8, fill=color_bg)
    draw.rounded_rectangle([(chip_x1, chip_y1), (chip_x1 + 5, chip_y2)],
                            radius=2, fill=color)

    nombre     = seccion["nombre"].upper()
    precio_txt = _mb_formato_precio(seccion["precio"], seccion.get("moneda", "USD"))
    ancho_disp = (chip_x2 - chip_x1) - 27

    tam = 13
    while tam >= 10:
        fn = _mb_fuente(tam, bold=True)
        w_n = draw.textbbox((0, 0), nombre,     font=fn)[2]
        w_p = draw.textbbox((0, 0), precio_txt, font=fn)[2]
        if w_n + w_p + 16 <= ancho_disp:
            break
        tam -= 1

    fuente  = _mb_fuente(tam, bold=True)
    pad_y   = (30 - tam) / 2 - 1
    draw.text((chip_x1 + 15, chip_y1 + pad_y), nombre,
              font=fuente, fill=_MB_PALETA["texto_principal"])
    w_p = draw.textbbox((0, 0), precio_txt, font=fuente)[2]
    draw.text((chip_x2 - w_p - 12, chip_y1 + pad_y), precio_txt,
              font=fuente, fill=color)
    return chip_y2 + 14


# ── Header, leyenda y footer ──────────────────────────────────────────────────

def _mb_header(draw, viaje, y):
    draw.rectangle([(0, 0), (_MB_ANCHO_IMG, 8)], fill=_MB_PALETA["acento"])

    fuente_titulo = _mb_fuente(30, bold=True)
    titulo = viaje.get("ruta") or viaje.get("nombre") or "Viaje"
    draw.text((_MB_PADDING, y), titulo,
              font=fuente_titulo, fill=_MB_PALETA["texto_principal"])
    y += 44

    partes = []
    if viaje.get("fecha"):
        partes.append(viaje["fecha"])
    hora = viaje.get("hora_salida") or viaje.get("hora")
    if hora:
        partes.append(f"Salida {hora}")
    empresa = viaje.get("empresa") or viaje.get("venue")
    if empresa:
        partes.append(empresa)
    info = "   •   ".join(partes)
    draw.text((_MB_PADDING, y), info,
              font=_mb_fuente(16), fill=_MB_PALETA["texto_secundario"])
    y += 32

    disp  = viaje.get("disponibles", 0)
    total = viaje.get("capacidad_total", 0)
    chip_text = f"{disp} sillas disponibles de {total}"
    fuente_c  = _mb_fuente(14, bold=True)
    bbox      = draw.textbbox((0, 0), chip_text, font=fuente_c)
    w_chip    = bbox[2] - bbox[0] + 24
    draw.rounded_rectangle(
        [(_MB_PADDING, y), (_MB_PADDING + w_chip, y + 30)],
        radius=15, fill="#E8F8F0",
    )
    draw.text((_MB_PADDING + 12, y + 7), chip_text, font=fuente_c, fill="#27AE60")
    return y + 38


def _mb_leyenda(draw, leyenda, y):
    draw.line([(_MB_PADDING, y), (_MB_ANCHO_IMG - _MB_PADDING, y)],
              fill=_MB_PALETA["borde"], width=1)
    y += 22
    fuente  = _mb_fuente(14, bold=True)
    items   = list(leyenda.items())
    col_w   = (_MB_ANCHO_IMG - 2 * _MB_PADDING) / len(items)
    for i, (_, info) in enumerate(items):
        cx = _MB_PADDING + i * col_w + 30
        draw.rounded_rectangle([(cx, y), (cx + 25, y + 25)], radius=6, fill=info["color"])
        draw.text((cx + 35, y + 4), info["label"], font=fuente,
                  fill=_MB_PALETA["texto_principal"])
    return y + 46


def _mb_footer(draw, y):
    alto = 52
    draw.rounded_rectangle(
        [(_MB_PADDING, y), (_MB_ANCHO_IMG - _MB_PADDING, y + alto)],
        radius=12, fill=_MB_PALETA["fondo_suave"],
    )
    fuente = _mb_fuente(13)
    texto  = "Responde con el número de la silla que deseas reservar. Ej: 07, 15"
    tx, ty = _mb_centrar(draw, texto, fuente,
                         (_MB_PADDING, y, _MB_ANCHO_IMG - _MB_PADDING, y + alto))
    draw.text((tx, ty), texto, font=fuente, fill=_MB_PALETA["texto_secundario"])


# ── Función principal ─────────────────────────────────────────────────────────

def generar_mapa_bus(data: dict, output_path: str | None = None):
    """
    Genera la imagen del bus con secciones, precios y leyenda.

    Args:
        data: dict con claves ``viaje`` (o ``evento``), ``secciones`` y ``leyenda``.
        output_path: path donde guardar el PNG. Si es None retorna un BytesIO.

    Returns:
        str con el path guardado, o io.BytesIO con el PNG.
    """
    if not _PIL_AVAILABLE:
        raise RuntimeError("Pillow no está instalado. Ejecuta: pip install Pillow")

    viaje    = data.get("viaje") or data.get("evento")
    secciones = data["secciones"]
    leyenda   = data["leyenda"]

    for s in secciones:
        s["_dist"] = _mb_inferir_dist(s)

    ancho_max = max(_mb_ancho_fila(s["_dist"]) for s in secciones)
    ancho_bus = int(ancho_max + 2 * _MB_BUS_PAD_X)

    alto_sillas = 0
    for s in secciones:
        alto_sillas += 44
        alto_sillas += len(s["filas"]) * (_MB_TAM_SILLA + _MB_GAP_FILA)
        alto_sillas += 10
    alto_bus = _MB_BUS_PAD_TOP + alto_sillas + _MB_BUS_PAD_BOT

    header_h  = 160
    legend_h  = 68
    footer_h  = 62
    alto_total = _MB_PADDING + header_h + 12 + alto_bus + 28 + legend_h + footer_h + _MB_PADDING

    img  = Image.new("RGB", (_MB_ANCHO_IMG, int(alto_total)), _MB_PALETA["fondo"])
    draw = ImageDraw.Draw(img)

    y = _MB_PADDING
    y = _mb_header(draw, viaje, y)
    y += 12

    x_bus = (_MB_ANCHO_IMG - ancho_bus) / 2
    _mb_carroceria(draw, x_bus, y, ancho_bus, alto_bus)
    _mb_cabina(draw, x_bus, y, ancho_bus)
    _mb_puerta_lateral(draw, x_bus, y, ancho_bus)

    y_sillas = y + _MB_BUS_PAD_TOP
    for s in secciones:
        y_sillas = _mb_seccion_label(draw, s, x_bus, ancho_bus, y_sillas)
        for fila in s["filas"]:
            _mb_dibujar_fila(draw, fila["sillas"], s["_dist"],
                             x_bus, ancho_bus, y_sillas, leyenda)
            y_sillas += _MB_TAM_SILLA + _MB_GAP_FILA
        y_sillas += 10

    _mb_trasera(draw, x_bus, y + alto_bus - 28, ancho_bus)

    y = y + alto_bus + 28
    y = _mb_leyenda(draw, leyenda, y)
    _mb_footer(draw, y + 4)

    if output_path:
        img.save(output_path, "PNG", optimize=True)
        return output_path

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf
