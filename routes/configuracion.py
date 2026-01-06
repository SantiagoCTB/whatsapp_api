import importlib.util
import json
import logging
import os
import re
import uuid
from datetime import datetime
from urllib.parse import urlparse

import requests
from flask import Blueprint, render_template, request, redirect, session, url_for, jsonify
from openpyxl import load_workbook
from werkzeug.utils import secure_filename

if importlib.util.find_spec("mysql.connector"):
    from mysql.connector import Error as MySQLError
else:  # pragma: no cover - fallback cuando falta el conector
    class MySQLError(Exception):
        pass

from config import Config
from services import tenants
from services.catalog import ingest_catalog_pdf
from services.db import get_connection, get_chat_state_definitions

config_bp = Blueprint('configuracion', __name__)
logger = logging.getLogger(__name__)

# El comodín '*' en `input_text` permite avanzar al siguiente paso sin validar
# la respuesta del usuario. Si es la única regla de un paso se ejecuta
# automáticamente; si coexiste con otras, actúa como respuesta por defecto.


def _media_root():
    return tenants.get_media_root()

def _require_admin():
    # Debe haber usuario logueado y el rol 'admin' en la lista de roles
    return "user" in session and 'admin' in (session.get('roles') or [])


def _normalize_input(text):
    """Normaliza una lista separada por comas."""
    return ','.join(t.strip().lower() for t in (text or '').split(',') if t.strip())

def _url_ok(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=5)
        ok = r.status_code == 200
        mime = r.headers.get('Content-Type', '').split(';', 1)[0] if ok else None
        return ok, mime
    except requests.RequestException:
        return False, None


def _normalize_state_key(raw_key: str | None) -> str | None:
    if not raw_key:
        return None
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", raw_key.strip().lower()).strip("_")
    if not cleaned:
        return None
    return cleaned[:40]


def _coerce_hex_color(value: str | None, default: str) -> str:
    if not value:
        return default
    candidate = value.strip()
    if not candidate:
        return default
    if not candidate.startswith("#"):
        candidate = f"#{candidate}"
    if re.fullmatch(r"#[0-9a-fA-F]{6}", candidate):
        return candidate
    return default


def _ensure_ia_config_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ia_config (
            id INT AUTO_INCREMENT PRIMARY KEY,
            model_name VARCHAR(100) NOT NULL DEFAULT 'o4-mini',
            model_token TEXT NULL,
            enabled TINYINT(1) NOT NULL DEFAULT 1,
            pdf_filename VARCHAR(255) NULL,
            pdf_original_name VARCHAR(255) NULL,
            pdf_mime VARCHAR(100) NULL,
            pdf_size BIGINT NULL,
            pdf_uploaded_at DATETIME NULL,
            pdf_source_url TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
        """
    )

    cursor.execute("SHOW COLUMNS FROM ia_config LIKE 'enabled';")
    has_enabled = cursor.fetchone() is not None
    if not has_enabled:
        cursor.execute(
            "ALTER TABLE ia_config ADD COLUMN enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER model_token;"
        )

    cursor.execute("SHOW COLUMNS FROM ia_config LIKE 'pdf_source_url';")
    has_source_url = cursor.fetchone() is not None
    if not has_source_url:
        cursor.execute(
            "ALTER TABLE ia_config ADD COLUMN pdf_source_url TEXT NULL AFTER pdf_uploaded_at;"
        )


def _get_ia_config(cursor):
    try:
        cursor.execute(
            """
            SELECT id, model_name, model_token, enabled, pdf_filename, pdf_original_name,
                   pdf_mime, pdf_size, pdf_uploaded_at, pdf_source_url
              FROM ia_config
          ORDER BY id DESC
             LIMIT 1
            """
        )
        rows = cursor.fetchall()
    except Exception:
        cursor.execute(
            """
            SELECT id, model_name, model_token, pdf_filename, pdf_original_name,
                   pdf_mime, pdf_size, pdf_uploaded_at, pdf_source_url
              FROM ia_config
          ORDER BY id DESC
             LIMIT 1
            """
        )
        rows = cursor.fetchall()

    if not rows:
        return None

    row = rows[0]

    if len(row) == 8:
        row = (*row[:3], 1, *row[3:], None)
    elif len(row) == 9:
        row = (*row, None)

    keys = [
        "id",
        "model_name",
        "model_token",
        "enabled",
        "pdf_filename",
        "pdf_original_name",
        "pdf_mime",
        "pdf_size",
        "pdf_uploaded_at",
        "pdf_source_url",
    ]

    return {key: value for key, value in zip(keys, row)}


def _botones_opciones_column(c, conn):
    """Asegura que la columna ``opciones`` exista en ``botones``.

    Devuelve la expresión SQL a utilizar en el SELECT para soportar
    instalaciones antiguas donde aún no existe la columna. En esos casos
    se intentará crearla y, si no es posible, se regresa ``NULL`` como
    marcador para evitar errores ``Unknown column``.
    """

    has_opciones = True
    try:
        c.execute("SHOW COLUMNS FROM botones LIKE 'opciones';")
        has_opciones = c.fetchone() is not None
        if not has_opciones:
            try:
                c.execute("ALTER TABLE botones ADD COLUMN opciones TEXT NULL;")
                conn.commit()
                has_opciones = True
            except MySQLError:
                conn.rollback()
                has_opciones = False
    except MySQLError:
        has_opciones = False

    return "b.opciones" if has_opciones else "NULL AS opciones"


def _botones_categoria_column(c, conn):
    """Asegura que la columna ``categoria`` exista en ``botones``.

    Devuelve la expresión SQL a utilizar en el SELECT para soportar
    instalaciones antiguas donde aún no existe la columna.
    """

    has_categoria = True
    try:
        c.execute("SHOW COLUMNS FROM botones LIKE 'categoria';")
        has_categoria = c.fetchone() is not None
        if not has_categoria:
            try:
                c.execute("ALTER TABLE botones ADD COLUMN categoria VARCHAR(100) NULL;")
                conn.commit()
                has_categoria = True
            except MySQLError:
                conn.rollback()
                has_categoria = False
    except MySQLError:
        has_categoria = False

    return "b.categoria" if has_categoria else "NULL AS categoria"

def _reglas_view(template_name):
    """Renderiza las vistas de reglas.
    El comodín '*' en `input_text` avanza al siguiente paso sin validar
    la respuesta del usuario; si existen otras reglas, actúa como opción
    por defecto."""
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        # --- Migraciones defensivas de nuevas columnas ---
        c.execute("SHOW COLUMNS FROM reglas LIKE 'rol_keyword';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN rol_keyword VARCHAR(20) NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'calculo';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN calculo TEXT NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'handler';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN handler VARCHAR(50) NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'media_url';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN media_url TEXT NULL;")
            conn.commit()
        c.execute("SHOW COLUMNS FROM reglas LIKE 'media_tipo';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN media_tipo VARCHAR(20) NULL;")
            conn.commit()

        if request.method == 'POST':
            # Importar desde Excel
            if 'archivo' in request.files and request.files['archivo']:
                archivo = request.files['archivo']
                wb = load_workbook(archivo)
                hoja = wb.active
                for fila in hoja.iter_rows(min_row=2, values_only=True):
                    if not fila:
                        continue
                    # Permitir archivos con columnas opcionales
                    datos = list(fila) + [None] * 11
                    step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler = datos[:11]
                    url_ok = False
                    detected_type = None
                    if media_url:
                        url_ok, detected_type = _url_ok(str(media_url))
                        if not url_ok:
                            media_url = None
                            media_tipo = None
                        else:
                            media_tipo = media_tipo or detected_type
                    if media_tipo:
                        media_tipo = str(media_tipo).split(';', 1)[0]
                    # Normalizar campos clave
                    step = (step or '').strip().lower()
                    input_text = _normalize_input(input_text)
                    siguiente_step = _normalize_input(siguiente_step) or None

                    c.execute(
                        "SELECT id FROM reglas WHERE step = %s AND input_text = %s",
                        (step, input_text)
                    )
                    existente = c.fetchone()
                    if existente:
                        regla_id = existente[0]
                        c.execute(
                            """
                            UPDATE reglas
                               SET respuesta = %s,
                                   siguiente_step = %s,
                                   tipo = %s,
                                   media_url = %s,
                                   media_tipo = %s,
                                   opciones = %s,
                                   rol_keyword = %s,
                                   calculo = %s,
                                   handler = %s
                             WHERE id = %s
                            """,
                            (respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler, regla_id)
                        )
                        c.execute("DELETE FROM regla_medias WHERE regla_id=%s", (regla_id,))
                        if media_url and url_ok:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, media_url, media_tipo),
                            )
                    else:
                        c.execute(
                            "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler)
                        )
                        regla_id = c.lastrowid
                        if media_url and url_ok:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, media_url, media_tipo),
                            )
                conn.commit()
            else:
                # Entrada manual desde formulario
                step = (request.form['step'] or '').strip().lower() or None
                input_text = _normalize_input(request.form['input_text']) or None
                respuesta = request.form['respuesta']
                siguiente_step = _normalize_input(request.form.get('siguiente_step')) or None
                tipo = request.form.get('tipo', 'texto')
                media_files = request.files.getlist('media') or request.files.getlist('media[]')
                media_url_field = request.form.get('media_url')
                medias = []
                for media_file in media_files:
                    if media_file and media_file.filename:
                        filename = secure_filename(media_file.filename)
                        unique = f"{uuid.uuid4().hex}_{filename}"
                        path = os.path.join(_media_root(), unique)
                        media_file.save(path)
                        url = url_for(
                            'static',
                            filename=tenants.get_uploads_url_path(unique),
                            _external=True,
                        )
                        medias.append((url, media_file.mimetype.split(';', 1)[0]))
                if media_url_field:
                    for url in [u.strip() for u in re.split(r'[\n,]+', media_url_field) if u.strip()]:
                        ok, content_type = _url_ok(url)
                        if not ok:
                            return f"URL no válida: {url}", 400
                        medias.append((url, content_type))
                media_url = medias[0][0] if medias else None
                media_tipo = medias[0][1] if medias else None
                opciones = request.form['opciones']
                list_header = request.form.get('list_header')
                list_footer = request.form.get('list_footer')
                list_button = request.form.get('list_button')
                sections_raw = request.form.get('sections')
                if tipo == 'lista':
                    if not opciones:
                        try:
                            sections = json.loads(sections_raw) if sections_raw else []
                        except Exception:
                            sections = []
                        opts = {
                            'header': list_header,
                            'footer': list_footer,
                            'button': list_button,
                            'sections': sections
                        }
                        opciones = json.dumps(opts)
                elif tipo == 'flow':
                    opciones_raw = (request.form.get('opciones') or '').strip()
                    flow_payload = {}
                    flow_keys = [k for k in request.form.keys() if k.startswith('flow_')]
                    for key in flow_keys:
                        value = request.form.get(key)
                        if key in {'flow_payload', 'flow_data'} and value:
                            try:
                                flow_payload[key] = json.loads(value)
                            except Exception:
                                flow_payload[key] = value
                        else:
                            flow_payload[key] = value
                    if flow_payload:
                        try:
                            opciones = json.dumps(flow_payload, ensure_ascii=False)
                        except (TypeError, ValueError):
                            opciones = json.dumps({k: str(v) if v is not None else '' for k, v in flow_payload.items()}, ensure_ascii=False)
                    elif opciones_raw:
                        try:
                            opciones = json.dumps(json.loads(opciones_raw), ensure_ascii=False)
                        except Exception:
                            opciones = opciones_raw
                    else:
                        opciones = ''
                rol_keyword = request.form.get('rol_keyword')
                calculo = request.form.get('calculo')
                handler = request.form.get('handler')
                regla_id = request.form.get('regla_id')

                if regla_id:
                    c.execute(
                        """
                        UPDATE reglas
                           SET step = %s,
                               input_text = %s,
                               respuesta = %s,
                               siguiente_step = %s,
                               tipo = %s,
                               media_url = %s,
                               media_tipo = %s,
                               opciones = %s,
                               rol_keyword = %s,
                               calculo = %s,
                               handler = %s
                         WHERE id = %s
                        """,
                        (
                            step,
                            input_text,
                            respuesta,
                            siguiente_step,
                            tipo,
                            media_url,
                            media_tipo,
                            opciones,
                            rol_keyword,
                            calculo,
                            handler,
                            regla_id,
                        ),
                    )
                    c.execute("DELETE FROM regla_medias WHERE regla_id=%s", (regla_id,))
                    for url, tipo_media in medias:
                        c.execute(
                            "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                            (regla_id, url, tipo_media),
                        )
                else:
                    c.execute(
                        "SELECT id FROM reglas WHERE step = %s AND input_text = %s",
                        (step, input_text)
                    )
                    existente = c.fetchone()
                    if existente:
                        regla_id = existente[0]
                        c.execute(
                            """
                            UPDATE reglas
                               SET respuesta = %s,
                                   siguiente_step = %s,
                                   tipo = %s,
                                   media_url = %s,
                                   media_tipo = %s,
                                   opciones = %s,
                                   rol_keyword = %s,
                                   calculo = %s,
                                   handler = %s
                             WHERE id = %s
                            """,
                            (
                                respuesta,
                                siguiente_step,
                                tipo,
                                media_url,
                                media_tipo,
                                opciones,
                                rol_keyword,
                                calculo,
                                handler,
                                regla_id,
                            ),
                        )
                        c.execute("DELETE FROM regla_medias WHERE regla_id=%s", (regla_id,))
                        for url, tipo_media in medias:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, url, tipo_media),
                            )
                    else:
                        c.execute(
                            "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                step,
                                input_text,
                                respuesta,
                                siguiente_step,
                                tipo,
                                media_url,
                                media_tipo,
                                opciones,
                                rol_keyword,
                                calculo,
                                handler,
                            ),
                        )
                        regla_id = c.lastrowid
                        for url, tipo_media in medias:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, url, tipo_media),
                            )
                conn.commit()

        # Listar todas las reglas
        c.execute(
            """
            SELECT r.id, r.step, r.input_text, r.respuesta, r.siguiente_step, r.tipo,
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos,
                   r.opciones, r.rol_keyword, r.calculo, r.handler
              FROM reglas r
              LEFT JOIN regla_medias m ON r.id = m.regla_id
             GROUP BY r.id
             ORDER BY r.id DESC
            """
        )
        rows = c.fetchall()
        reglas = []
        for row in rows:
            d = {
                'id': row[0],
                'step': row[1],
                'input_text': row[2],
                'respuesta': row[3],
                'siguiente_step': row[4],
                'tipo': row[5],
                'media_urls': (row[6] or '').split('||') if row[6] else [],
                'media_tipos': (row[7] or '').split('||') if row[7] else [],
                'opciones': row[8] or '',
                'rol_keyword': row[9],
                'calculo': row[10],
                'handler': row[11],
                'header': None,
                'button': None,
                'footer': None,
                'flow': None,
                'opciones_pretty': None,
            }
            if d['opciones']:
                parsed_opts = None
                try:
                    parsed_opts = json.loads(d['opciones'])
                except Exception:
                    parsed_opts = None

                if d['tipo'] == 'lista' and isinstance(parsed_opts, dict):
                    d['header'] = parsed_opts.get('header')
                    d['button'] = parsed_opts.get('button')
                    d['footer'] = parsed_opts.get('footer')
                elif d['tipo'] == 'flow' and isinstance(parsed_opts, dict):
                    for key, value in parsed_opts.items():
                        if isinstance(value, (dict, list)):
                            try:
                                d[key] = json.dumps(value, ensure_ascii=False)
                            except (TypeError, ValueError):
                                d[key] = value
                        else:
                            d[key] = value
                    d['flow_options'] = parsed_opts
            reglas.append(d)
        chat_state_definitions = get_chat_state_definitions(include_hidden=True)
        return render_template(
            template_name,
            reglas=reglas,
            chat_state_definitions=chat_state_definitions,
        )
    finally:
        conn.close()


@config_bp.route('/chat_states', methods=['POST'])
def save_chat_state_definition():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    get_chat_state_definitions(include_hidden=True)
    raw_key = request.form.get('state_key')
    original_key = request.form.get('original_key')
    label = (request.form.get('label') or '').strip()
    color_hex = _coerce_hex_color(request.form.get('color_hex'), '#666666')
    text_color_hex = _coerce_hex_color(request.form.get('text_color_hex'), '#ffffff')
    priority_raw = request.form.get('priority')
    visible = 1 if request.form.get('visible') in {'1', 'true', 'on', 'yes'} else 0

    state_key = _normalize_state_key(raw_key)
    if not state_key:
        return redirect(url_for('configuracion.reglas'))

    if not label:
        label = state_key.replace("_", " ").title()

    try:
        priority = int(priority_raw) if priority_raw is not None else 0
    except (TypeError, ValueError):
        priority = 0

    conn = get_connection()
    c = conn.cursor()
    try:
        if original_key:
            original_key = _normalize_state_key(original_key)
        if original_key and original_key != state_key:
            c.execute(
                "DELETE FROM chat_state_definitions WHERE state_key = %s",
                (original_key,),
            )

        c.execute(
            """
            INSERT INTO chat_state_definitions
                (state_key, label, color_hex, text_color_hex, priority, visible)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                label = VALUES(label),
                color_hex = VALUES(color_hex),
                text_color_hex = VALUES(text_color_hex),
                priority = VALUES(priority),
                visible = VALUES(visible)
            """,
            (state_key, label, color_hex, text_color_hex, priority, visible),
        )
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for('configuracion.reglas'))


@config_bp.route('/chat_states/delete', methods=['POST'])
def delete_chat_state_definition():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    state_key = _normalize_state_key(request.form.get('state_key'))
    if not state_key:
        return redirect(url_for('configuracion.reglas'))

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute(
            "DELETE FROM chat_state_definitions WHERE state_key = %s",
            (state_key,),
        )
        conn.commit()
    finally:
        conn.close()

    return redirect(url_for('configuracion.reglas'))


@config_bp.route('/configuracion/ia', methods=['GET', 'POST'])
def configuracion_ia():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    status_message = None
    error_message = None
    try:
        _ensure_ia_config_table(c)
        conn.commit()

        ia_config = _get_ia_config(c)
        pdf_url = None
        if ia_config and ia_config.get('pdf_filename'):
            pdf_filename = ia_config['pdf_filename']
            preferred_path = os.path.join(_media_root(), pdf_filename)
            preferred_url_path = tenants.get_uploads_url_path(pdf_filename)
            if not os.path.exists(preferred_path):
                legacy_path = os.path.join(_media_root(), 'ia', pdf_filename)
                if os.path.exists(legacy_path):
                    preferred_url_path = tenants.get_uploads_url_path(f"ia/{pdf_filename}")
            pdf_url = url_for('static', filename=preferred_url_path)

        if request.method == 'POST':
            ia_model = (request.form.get('ia_model') or 'o4-mini').strip() or 'o4-mini'
            ia_token = (request.form.get('ia_token') or '').strip()
            ia_enabled = 1 if request.form.get('ia_enabled') in {'on', '1', 'true', 't'} else 0
            catalog_url = (request.form.get('catalogo_url') or '').strip()
            pdf_file = request.files.get('catalogo_pdf')
            pdf_dir = _media_root()
            os.makedirs(pdf_dir, exist_ok=True)
            stored_catalog_name = 'catalogo.pdf'

            new_pdf = None
            old_pdf_path = None
            ingest_error = None

            if not ia_token:
                error_message = 'El token del modelo es obligatorio.'

            if pdf_file and pdf_file.filename and catalog_url:
                error_message = 'Sube un PDF o indica una URL, pero no ambas opciones.'

            if pdf_file and pdf_file.filename and not error_message:
                filename = secure_filename(pdf_file.filename)
                mime = (pdf_file.mimetype or '').lower()
                if not filename.lower().endswith('.pdf'):
                    error_message = 'Solo se permiten archivos PDF.'
                elif mime and 'pdf' not in mime:
                    error_message = 'El archivo subido no parece ser un PDF válido.'
                else:
                    stored_name = stored_catalog_name
                    path = os.path.join(pdf_dir, stored_name)
                    pdf_file.save(path)
                    pdf_size = os.path.getsize(path)
                    new_pdf = {
                        'stored_name': stored_name,
                        'original_name': filename,
                        'mime': pdf_file.mimetype or 'application/pdf',
                        'size': pdf_size,
                        'source_url': None,
                    }
                    if ia_config and ia_config.get('pdf_filename'):
                        old_pdf_path = os.path.join(pdf_dir, ia_config['pdf_filename'])
                        if not os.path.exists(old_pdf_path):
                            legacy_path = os.path.join(pdf_dir, 'ia', ia_config['pdf_filename'])
                            if os.path.exists(legacy_path):
                                old_pdf_path = legacy_path

                    try:
                        ingest_catalog_pdf(path, stored_name)
                    except Exception as exc:  # pragma: no cover - depende de libs externas
                        logger.exception("Error al indexar catálogo PDF", exc_info=exc)
                        ingest_error = 'No se pudo procesar el catálogo PDF. Verifica que el archivo no esté dañado.'
                        try:
                            os.remove(path)
                        except OSError:
                            pass
                        new_pdf = None

            elif catalog_url and not error_message:
                ok, mime = _url_ok(catalog_url)
                if not ok:
                    error_message = 'La URL del catálogo no está disponible o respondió con error.'
                elif mime and 'pdf' not in mime.lower():
                    error_message = 'La URL no apunta a un PDF válido.'
                else:
                    parsed = urlparse(catalog_url)
                    base_name = os.path.basename(parsed.path) or 'catalogo.pdf'
                    filename = secure_filename(base_name) or 'catalogo.pdf'
                    stored_name = stored_catalog_name
                    path = os.path.join(pdf_dir, stored_name)
                    try:
                        with requests.get(catalog_url, stream=True, timeout=120) as resp:
                            if resp.status_code != 200:
                                error_message = 'No se pudo descargar el catálogo desde la URL proporcionada.'
                            else:
                                with open(path, 'wb') as fh:
                                    for chunk in resp.iter_content(chunk_size=8192):
                                        if not chunk:
                                            continue
                                        fh.write(chunk)

                                if not error_message:
                                    pdf_size = os.path.getsize(path)
                                    new_pdf = {
                                        'stored_name': stored_name,
                                        'original_name': filename,
                                        'mime': resp.headers.get('Content-Type', 'application/pdf'),
                                        'size': pdf_size,
                                        'source_url': catalog_url,
                                    }
                                    if ia_config and ia_config.get('pdf_filename'):
                                        old_pdf_path = os.path.join(pdf_dir, ia_config['pdf_filename'])
                                        if not os.path.exists(old_pdf_path):
                                            legacy_path = os.path.join(pdf_dir, 'ia', ia_config['pdf_filename'])
                                            if os.path.exists(legacy_path):
                                                old_pdf_path = legacy_path

                                    try:
                                        ingest_catalog_pdf(path, stored_name)
                                    except Exception as exc:  # pragma: no cover - depende de libs externas
                                        logger.exception("Error al indexar catálogo PDF", exc_info=exc)
                                        ingest_error = 'No se pudo procesar el catálogo PDF. Verifica que el archivo no esté dañado.'
                                        try:
                                            os.remove(path)
                                        except OSError:
                                            pass
                                        new_pdf = None
                    except requests.RequestException:
                        error_message = 'No se pudo descargar el catálogo desde la URL proporcionada.'
                    if error_message and os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass

            if not error_message and ingest_error:
                error_message = ingest_error

            if not error_message:
                if ia_config:
                    c.execute(
                        """
                        UPDATE ia_config
                           SET model_name = %s,
                               model_token = %s,
                               enabled = %s,
                               pdf_filename = %s,
                               pdf_original_name = %s,
                               pdf_mime = %s,
                               pdf_size = %s,
                               pdf_uploaded_at = %s,
                               pdf_source_url = %s
                         WHERE id = %s
                        """,
                        (
                            ia_model,
                            ia_token,
                            ia_enabled,
                            new_pdf['stored_name'] if new_pdf else ia_config.get('pdf_filename'),
                            new_pdf['original_name'] if new_pdf else ia_config.get('pdf_original_name'),
                            new_pdf['mime'] if new_pdf else ia_config.get('pdf_mime'),
                            new_pdf['size'] if new_pdf else ia_config.get('pdf_size'),
                            datetime.utcnow() if new_pdf else ia_config.get('pdf_uploaded_at'),
                            new_pdf['source_url'] if new_pdf else ia_config.get('pdf_source_url'),
                            ia_config['id'],
                        ),
                    )
                else:
                    c.execute(
                        """
                        INSERT INTO ia_config
                            (model_name, model_token, enabled, pdf_filename, pdf_original_name, pdf_mime, pdf_size, pdf_uploaded_at, pdf_source_url)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            ia_model,
                            ia_token,
                            ia_enabled,
                            new_pdf['stored_name'] if new_pdf else None,
                            new_pdf['original_name'] if new_pdf else None,
                            new_pdf['mime'] if new_pdf else None,
                            new_pdf['size'] if new_pdf else None,
                            datetime.utcnow() if new_pdf else None,
                            new_pdf['source_url'] if new_pdf else None,
                        ),
                    )

                conn.commit()
                ia_config = _get_ia_config(c)
                pdf_url = None
                if ia_config and ia_config.get('pdf_filename'):
                    pdf_url = url_for(
                        'static',
                        filename=tenants.get_uploads_url_path(ia_config['pdf_filename'])
                    )
                status_message = 'Configuración de IA actualizada correctamente.'

                if new_pdf and old_pdf_path and os.path.exists(old_pdf_path):
                    try:
                        os.remove(old_pdf_path)
                    except OSError:
                        pass

        return render_template(
            'configuracion_ia.html',
            ia_config=ia_config,
            pdf_url=pdf_url,
            status_message=status_message,
            error_message=error_message,
        )
    finally:
        conn.close()


@config_bp.route('/configuracion/signup', methods=['GET'])
def configuracion_signup():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    tenant = tenants.get_current_tenant()
    tenant_key = tenant.tenant_key if tenant else None

    return render_template(
        'configuracion_signup.html',
        signup_config_code=Config.SIGNUP_FACEBOOK,
        facebook_app_id=Config.FACEBOOK_APP_ID,
        tenant_key=tenant_key,
    )


@config_bp.route('/configuracion/signup', methods=['POST'])
def save_signup():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = tenants.get_current_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error": "Payload inválido"}, 400

    current_env = tenants.get_tenant_env(tenant)
    env_updates = {key: current_env.get(key) for key in tenants.TENANT_ENV_KEYS}
    env_updates.update(
        {
            "META_TOKEN": payload.get("access_token") or payload.get("token"),
            "LONG_LIVED_TOKEN": payload.get("access_token")
            or payload.get("long_lived_token"),
            "PHONE_NUMBER_ID": payload.get("phone_number_id")
            or payload.get("phone_id"),
            "WABA_ID": payload.get("waba_id"),
            "BUSINESS_ID": payload.get("business_id")
            or payload.get("business_manager_id"),
        }
    )

    business_info = payload.get("business") or payload.get("business_info")
    metadata_updates = {}
    if isinstance(business_info, dict) and business_info:
        metadata_updates["whatsapp_business"] = business_info

    tenants.update_tenant_env(tenant.tenant_key, env_updates)
    if metadata_updates:
        tenants.update_tenant_metadata(tenant.tenant_key, metadata_updates)

    return {
        "ok": True,
        "message": "Credenciales de WhatsApp actualizadas.",
        "env": tenants.get_tenant_env(tenant),
    }

@config_bp.route('/configuracion', methods=['GET', 'POST'])
def configuracion():
    return _reglas_view('configuracion.html')

@config_bp.route('/reglas', methods=['GET', 'POST'])
def reglas():
    return _reglas_view('reglas.html')

@config_bp.route('/eliminar_regla/<int:regla_id>', methods=['POST'])
def eliminar_regla(regla_id):
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM reglas WHERE id = %s", (regla_id,))
        conn.commit()
        return redirect(url_for('configuracion.reglas'))
    finally:
        conn.close()

@config_bp.route('/botones', methods=['GET', 'POST'])
def botones():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        opciones_expr = _botones_opciones_column(c, conn)
        categoria_expr = _botones_categoria_column(c, conn)
        if request.method == 'POST':
            # Importar botones desde Excel
            if 'archivo' in request.files and request.files['archivo']:
                archivo = request.files['archivo']
                wb = load_workbook(archivo)
                hoja = wb.active
                for fila in hoja.iter_rows(min_row=2, values_only=True):
                    if not fila:
                        continue
                    nombre = fila[0]
                    mensaje = fila[1] if len(fila) > 1 else None
                    tipo = fila[2] if len(fila) > 2 else None
                    media_url = fila[3] if len(fila) > 3 else None
                    opciones = fila[4] if len(fila) > 4 else None
                    categoria = fila[5] if len(fila) > 5 else None
                    if isinstance(opciones, (dict, list)):
                        opciones = json.dumps(opciones, ensure_ascii=False)
                    elif opciones is not None:
                        opciones = str(opciones).strip()
                        if not opciones:
                            opciones = None
                    medias = []
                    if media_url:
                        urls = [u.strip() for u in re.split(r'[\n,]+', str(media_url)) if u and u.strip()]
                        for url in urls:
                            ok, mime = _url_ok(url)
                            if ok:
                                medias.append((url, mime))
                    if mensaje:
                        c.execute(
                            "INSERT INTO botones (nombre, mensaje, tipo, opciones, categoria) VALUES (%s, %s, %s, %s, %s)",
                            (nombre, mensaje, tipo, opciones, categoria)
                        )
                        boton_id = c.lastrowid
                        for url, mime in medias:
                            c.execute(
                                "INSERT INTO boton_medias (boton_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (boton_id, url, mime)
                            )
                conn.commit()
            elif request.form.get('regla_id'):
                regla_id = request.form.get('regla_id')
                try:
                    regla_id = int(regla_id)
                except (TypeError, ValueError):
                    regla_id = None

                if regla_id:
                    c.execute(
                        """
                        SELECT r.respuesta,
                               r.tipo,
                               r.opciones,
                               GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                               GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos
                          FROM reglas r
                          LEFT JOIN regla_medias m ON r.id = m.regla_id
                         WHERE r.id = %s
                         GROUP BY r.id
                        """,
                        (regla_id,)
                    )
                    row = c.fetchone()
                else:
                    row = None

                if row and row[0]:
                    nombre = request.form.get('nombre')
                    respuesta = row[0]
                    tipo = row[1] or 'texto'
                    opciones_raw = row[2]
                    media_urls_raw = row[3].split('||') if row[3] else []
                    media_tipos_raw = row[4].split('||') if row[4] else []
                    medias = []
                    for idx, url in enumerate(media_urls_raw):
                        if not url:
                            continue
                        mime = media_tipos_raw[idx] if idx < len(media_tipos_raw) else None
                        medias.append((url, mime))

                    opciones_value = opciones_raw if opciones_raw else None
                    c.execute(
                        "INSERT INTO botones (nombre, mensaje, tipo, opciones, categoria) VALUES (%s, %s, %s, %s, %s)",
                        (nombre, respuesta, tipo, opciones_value, request.form.get('categoria'))
                    )
                    boton_id = c.lastrowid
                    for url, mime in medias:
                        c.execute(
                            "INSERT INTO boton_medias (boton_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                            (boton_id, url, mime)
                        )
                    conn.commit()
            # Agregar botón manual
            elif 'mensaje' in request.form:
                nombre = request.form.get('nombre')
                nuevo_mensaje = request.form['mensaje']
                tipo = request.form.get('tipo')
                media_files = request.files.getlist('media')
                medias = []
                for media_file in media_files:
                    if media_file and media_file.filename:
                        filename = secure_filename(media_file.filename)
                        unique = f"{uuid.uuid4().hex}_{filename}"
                        path = os.path.join(_media_root(), unique)
                        media_file.save(path)
                        url = url_for(
                            'static',
                            filename=tenants.get_uploads_url_path(unique),
                            _external=True,
                        )
                        medias.append((url, media_file.mimetype.split(';', 1)[0]))
                media_url = request.form.get('media_url', '')
                urls = [u.strip() for u in re.split(r'[\n,]+', media_url) if u and u.strip()]
                for url in urls:
                    ok, mime = _url_ok(url)
                    if ok:
                        medias.append((url, mime))
                if nuevo_mensaje:
                    c.execute(
                        "INSERT INTO botones (nombre, mensaje, tipo, opciones, categoria) VALUES (%s, %s, %s, %s, %s)",
                        (nombre, nuevo_mensaje, tipo, None, request.form.get('categoria'))
                    )
                    boton_id = c.lastrowid
                    for url, mime in medias:
                        c.execute(
                            "INSERT INTO boton_medias (boton_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                            (boton_id, url, mime)
                        )
                    conn.commit()

        c.execute(
            f"""
            SELECT b.id, b.mensaje, b.tipo, b.nombre, {opciones_expr}, {categoria_expr},
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos
              FROM botones b
              LEFT JOIN boton_medias m ON b.id = m.boton_id
             GROUP BY b.id
             ORDER BY b.id
            """
        )
        botones = []
        for row in c.fetchall():
            media_urls = row[6].split('||') if row[6] else []
            media_tipos = row[7].split('||') if row[7] else []
            if media_urls:
                items = []
                for idx, url in enumerate(media_urls):
                    mime = media_tipos[idx] if idx < len(media_tipos) else ''
                    texto = f"{url} ({mime})" if mime else url
                    items.append(f"<li>{texto}</li>")
                media_urls_display = f"<ul>{''.join(items)}</ul>"
            else:
                media_urls_display = ''
            botones.append({
                'id': row[0],
                'mensaje': row[1] or '',
                'tipo': row[2] or 'texto',
                'nombre': row[3],
                'opciones': row[4] or '',
                'categoria': row[5],
                'media_urls': media_urls,
                'media_tipos': media_tipos,
                'media_urls_display': media_urls_display,
            })
        c.execute(
            """
            SELECT r.id, r.step, r.input_text, r.respuesta, r.tipo,
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos
              FROM reglas r
              LEFT JOIN regla_medias m ON r.id = m.regla_id
             GROUP BY r.id
             ORDER BY r.step, r.id
            """
        )
        reglas = []
        for row in c.fetchall():
            reglas.append({
                'id': row[0],
                'step': row[1] or '',
                'input_text': row[2] or '',
                'respuesta': row[3] or '',
                'tipo': row[4] or '',
                'media_urls': row[5] or '',
                'media_tipos': row[6] or '',
            })
        return render_template('botones.html', botones=botones, reglas=reglas)
    finally:
        conn.close()

@config_bp.route('/eliminar_boton/<int:boton_id>', methods=['POST'])
def eliminar_boton(boton_id):
    if not _require_admin():
        return redirect(url_for("auth.login"))

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("DELETE FROM botones WHERE id = %s", (boton_id,))
        conn.commit()
        return redirect(url_for('configuracion.botones'))
    finally:
        conn.close()

@config_bp.route('/get_botones')
def get_botones():
    conn = get_connection()
    c = conn.cursor()
    try:
        opciones_expr = _botones_opciones_column(c, conn)
        categoria_expr = _botones_categoria_column(c, conn)
        c.execute(
            f"""
            SELECT b.id, b.mensaje, b.tipo, b.nombre, {opciones_expr}, {categoria_expr},
                   GROUP_CONCAT(m.media_url SEPARATOR '||') AS media_urls,
                   GROUP_CONCAT(m.media_tipo SEPARATOR '||') AS media_tipos
              FROM botones b
              LEFT JOIN boton_medias m ON b.id = m.boton_id
             GROUP BY b.id
             ORDER BY b.id
            """
        )
        rows = c.fetchall()
        return jsonify([
            {
                'id': r[0],
                'mensaje': r[1] or '',
                'tipo': r[2] or 'texto',
                'nombre': r[3],
                'opciones': r[4] or '',
                'categoria': r[5],
                'media_urls': r[6].split('||') if r[6] else [],
                'media_tipos': r[7].split('||') if r[7] else []
            }
            for r in rows
        ])
    finally:
        conn.close()
