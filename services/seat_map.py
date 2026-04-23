"""Genera imagen PNG top-down de un bus y la lista interactiva de asientos disponibles.

La imagen muestra: capó redondeado, parabrisas, ventanas laterales, conductor y
asientos en layout 2+2 con pasillo central.

El PNG se sube a S3 si AWS_S3_BUCKET está configurado; de lo contrario se guarda
en static/uploads/<tenant>/ y se sirve vía la URL pública de la app.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# ── Paleta ────────────────────────────────────────────────────────────────────
BUS_EXT     = (15,  23,  42)   # carrocería exterior
BUS_INT     = (10,  16,  30)   # interior oscuro
WINDSHIELD  = (147, 197, 253)  # parabrisas azul claro
WINDSHIELD2 = (186, 222, 251)  # reflejo parabrisas
WINDOW_C    = ( 59, 130, 246)  # ventanas laterales
COND_BG     = ( 30,  41,  59)  # zona conductor
AISLE_C     = (  8,  12,  24)  # pasillo central

C_LIBRE     = ( 29,  78, 216)  # #1d4ed8  disponible
C_PREF      = (109,  40, 217)  # #6d28d9  preferencial
C_VIP       = (180,  83,   9)  # #b45309  VIP
C_OCUPADO   = (155,  28,  28)  # #9b1c1c  ocupado

TEXT_W      = (255, 255, 255)
TEXT_DIM    = (120, 150, 200)
TEXT_LABEL  = (190, 210, 245)

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

SEAT_MAP_MAX_AGE = 7_200  # 2 horas

# ── Dimensiones ───────────────────────────────────────────────────────────────
BUS_WALL  = 14
SEAT_PAD  =  8
SEAT_W    = 52
SEAT_H    = 44
PAIR_GAP  =  4
AISLE_W   = 26
ROW_GAP   =  6
DRIVER_H  = 28
HOOD_H    = 58
CORNER_R  =  8

# Posiciones X calculadas
_LEFT_X   = BUS_WALL + SEAT_PAD                         # borde izq del primer asiento
_R2_X     = _LEFT_X + SEAT_W + PAIR_GAP                 # borde izq del segundo asiento izq
_L3_X     = _R2_X + SEAT_W + AISLE_W                   # borde izq del primer asiento der
_L4_X     = _L3_X + SEAT_W + PAIR_GAP                  # borde izq del segundo asiento der
IMG_W     = _L4_X + SEAT_W + SEAT_PAD + BUS_WALL        # ancho total de imagen


# ── Helpers de dibujo ─────────────────────────────────────────────────────────

def _fonts():
    from PIL import ImageFont
    try:
        return (
            ImageFont.truetype(FONT_BOLD, 13),  # título
            ImageFont.truetype(FONT_REG,  10),  # subtítulo
            ImageFont.truetype(FONT_BOLD, 14),  # número de silla
            ImageFont.truetype(FONT_BOLD, 15),  # ✕ ocupado
            ImageFont.truetype(FONT_REG,   9),  # leyenda
        )
    except Exception:
        d = ImageFont.load_default()
        return d, d, d, d, d


def _rrect(draw, x0, y0, x1, y1, r, fill):
    r = min(r, (x1 - x0) // 2, (y1 - y0) // 2)
    draw.rectangle([x0 + r, y0, x1 - r, y1], fill=fill)
    draw.rectangle([x0, y0 + r, x1, y1 - r], fill=fill)
    for cx, cy in [(x0, y0), (x1 - 2*r, y0), (x0, y1 - 2*r), (x1 - 2*r, y1 - 2*r)]:
        draw.ellipse([cx, cy, cx + 2*r, cy + 2*r], fill=fill)


def _ctext(draw, cx, cy, text, font, fill):
    bb = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (bb[2] - bb[0]) // 2, cy - (bb[3] - bb[1]) // 2), text, font=font, fill=fill)


# ── Dibujo del chasis ─────────────────────────────────────────────────────────

def _draw_bus_shell(draw, img_h: int):
    """Carrocería exterior, interior, capó y parabrisas."""
    # Cuerpo exterior principal
    _rrect(draw, 0, 20, IMG_W, img_h, r=20, fill=BUS_EXT)

    # Capó redondeado (frente del bus)
    _rrect(draw, 10, 0, IMG_W - 10, HOOD_H, r=30, fill=BUS_EXT)

    # Interior del bus
    draw.rectangle([BUS_WALL, HOOD_H, IMG_W - BUS_WALL, img_h - 8], fill=BUS_INT)

    # Parabrisas (trapecio: más ancho abajo que arriba, da perspectiva)
    ws_margin_top = 18
    ws_margin_bot = 8
    ws_top = 5
    ws_bot = HOOD_H - 3
    draw.polygon([
        (ws_margin_top, ws_top),
        (IMG_W - ws_margin_top, ws_top),
        (IMG_W - ws_margin_bot, ws_bot),
        (ws_margin_bot, ws_bot),
    ], fill=WINDSHIELD)

    # Reflejo / división central del parabrisas
    cx = IMG_W // 2
    draw.line([(cx, ws_top + 4), (cx, ws_bot - 4)], fill=WINDSHIELD2, width=2)

    # Marco del parabrisas (borde oscuro)
    draw.polygon([
        (ws_margin_top, ws_top),
        (IMG_W - ws_margin_top, ws_top),
        (IMG_W - ws_margin_bot, ws_bot),
        (ws_margin_bot, ws_bot),
    ], outline=BUS_EXT)


def _draw_side_windows(draw, first_y: int, n_rows: int):
    """Ventanillas laterales a lo largo de las filas de asientos."""
    win_x0_L = 3
    win_x1_L = BUS_WALL - 2
    win_x0_R = IMG_W - BUS_WALL + 2
    win_x1_R = IMG_W - 3
    for i in range(n_rows):
        wy0 = first_y + i * (SEAT_H + ROW_GAP) + 3
        wy1 = wy0 + SEAT_H - 6
        _rrect(draw, win_x0_L, wy0, win_x1_L, wy1, r=2, fill=WINDOW_C)
        _rrect(draw, win_x0_R, wy0, win_x1_R, wy1, r=2, fill=WINDOW_C)


# ── Datos de asiento ─────────────────────────────────────────────────────────

def _seat_color(seat: dict):
    tipo  = (seat.get("type",  "") or "").lower()
    state = (seat.get("state", "") or "").lower()
    libre = "disponible" in state
    if tipo == "preferencial": return C_PREF  if libre else C_OCUPADO
    if tipo == "vip":          return C_VIP   if libre else C_OCUPADO
    return C_LIBRE if libre else C_OCUPADO


def _split_groups(seats: list):
    """Devuelve (left_cols, right_cols) excluyendo columnas Pasillo/Conductor."""
    if not seats:
        return [], []
    n_cols = max(len(r) for r in seats)
    aisle = {
        c for c in range(n_cols)
        if all(seats[r][c].get("type", "").lower() in ("pasillo", "conductor")
               for r in range(len(seats)) if c < len(seats[r]))
    }
    first_a = min(aisle) if aisle else n_cols // 2
    last_a  = max(aisle) if aisle else n_cols // 2
    left  = [c for c in range(first_a) if c not in aisle]
    right = [c for c in range(last_a + 1, n_cols) if c not in aisle]
    return left, right


def _x_for(col_index: int, group: str) -> int:
    """X del borde izquierdo de un asiento dado su índice dentro del grupo."""
    if group == "left":
        return _LEFT_X + col_index * (SEAT_W + PAIR_GAP)
    return _L3_X + col_index * (SEAT_W + PAIR_GAP)


# ── Generador principal ───────────────────────────────────────────────────────

def generate_seat_map_image(
    seats: list[list[dict]],
    bearing_id: int | str = "",
    route_name: str = "",
    departure: str = "",
) -> bytes:
    """Genera PNG top-down del bus y lo retorna como bytes."""
    from PIL import Image, ImageDraw

    f_title, f_sub, f_num, f_x, f_tiny = _fonts()
    left_cols, right_cols = _split_groups(seats)

    # Calcular altura
    n_driver = sum(
        1 for row in seats
        if all(s.get("type", "").lower() in ("conductor", "pasillo") for s in row)
    )
    n_seat_rows = len(seats) - n_driver
    cond_y      = HOOD_H + 5
    first_seat_y = cond_y + (DRIVER_H + 6 if n_driver else 0)
    grid_h      = n_seat_rows * (SEAT_H + ROW_GAP) - ROW_GAP
    img_h       = first_seat_y + grid_h + 14

    img  = Image.new("RGB", (IMG_W, img_h), BUS_EXT)
    draw = ImageDraw.Draw(img)

    _draw_bus_shell(draw, img_h)
    _draw_side_windows(draw, first_seat_y, n_seat_rows)

    # ── Conductor ──────────────────────────────────────────────────────────────
    if n_driver:
        _rrect(draw, BUS_WALL + 2, cond_y, IMG_W - BUS_WALL - 2,
               cond_y + DRIVER_H, r=5, fill=COND_BG)
        _ctext(draw, IMG_W // 2, cond_y + DRIVER_H // 2,
               "Conductor", f_sub, TEXT_DIM)

    # ── Asientos ──────────────────────────────────────────────────────────────
    seat_row_idx = 0
    for row in seats:
        if all(s.get("type", "").lower() in ("conductor", "pasillo") for s in row):
            continue

        y0 = first_seat_y + seat_row_idx * (SEAT_H + ROW_GAP)
        y1 = y0 + SEAT_H
        yc = (y0 + y1) // 2

        for i, col in enumerate(left_cols):
            seat = row[col] if col < len(row) else {}
            if not seat or seat.get("type", "").lower() == "pasillo":
                continue
            x0 = _x_for(i, "left")
            x1 = x0 + SEAT_W
            color = _seat_color(seat)
            _rrect(draw, x0, y0, x1, y1, CORNER_R, color)
            num = str(seat.get("number", "") or "")
            if num and num != "0":
                is_occ = "disponible" not in (seat.get("state", "") or "").lower()
                if is_occ:
                    _ctext(draw, (x0 + x1) // 2, yc, "X", f_x, TEXT_W)
                else:
                    _ctext(draw, (x0 + x1) // 2, yc, num, f_num, TEXT_W)

        for i, col in enumerate(right_cols):
            seat = row[col] if col < len(row) else {}
            if not seat or seat.get("type", "").lower() == "pasillo":
                continue
            x0 = _x_for(i, "right")
            x1 = x0 + SEAT_W
            color = _seat_color(seat)
            _rrect(draw, x0, y0, x1, y1, CORNER_R, color)
            num = str(seat.get("number", "") or "")
            if num and num != "0":
                is_occ = "disponible" not in (seat.get("state", "") or "").lower()
                if is_occ:
                    _ctext(draw, (x0 + x1) // 2, yc, "X", f_x, TEXT_W)
                else:
                    _ctext(draw, (x0 + x1) // 2, yc, num, f_num, TEXT_W)

        seat_row_idx += 1

    # ── Leyenda ────────────────────────────────────────────────────────────────
    leg_items = [
        (C_LIBRE,   "Libre"),
        (C_OCUPADO, "Ocupado"),
        (C_PREF,    "Preferencial"),
        (C_VIP,     "VIP"),
    ]
    box = 10
    lx = BUS_WALL + 4
    ly = img_h - 26
    for color, label in leg_items:
        _rrect(draw, lx, ly + 2, lx + box, ly + 2 + box, 2, color)
        draw.text((lx + box + 3, ly + 1), label, font=f_tiny, fill=TEXT_LABEL)
        bb = draw.textbbox((0, 0), label, font=f_tiny)
        lx += box + 3 + (bb[2] - bb[0]) + 10
        if lx > IMG_W - 60:
            lx, ly = BUS_WALL + 4, ly + 16

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

    sections: list[dict] = []
    if pref:
        sections.append({"title": "Preferencial", "rows": pref[:10]})
    if vip:
        sections.append({"title": "VIP", "rows": vip[:10]})

    # Dividir regulares en bloques de 10 (límite de WhatsApp por sección)
    for i in range(0, len(regular), 10):
        chunk = regular[i:i + 10]
        first, last = chunk[0]["id"], chunk[-1]["id"]
        title = f"Sillas {first}-{last}" if len(chunk) > 1 else f"Silla {first}"
        sections.append({"title": title[:24], "rows": chunk})

    if not sections:
        return None

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
