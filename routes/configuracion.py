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
from services.whatsapp_api import list_phone_numbers
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


def _resolve_signup_tenant():
    """Obtiene el tenant activo para el flujo de Embedded Signup.

    - Usa el tenant en contexto cuando existe.
    - Si no hay tenant en contexto, intenta usar el tenant activo (incluye
      DEFAULT_TENANT) y lo recupera del registro.
    """

    tenant = tenants.get_current_tenant()
    if tenant:
        return tenant

    fallback_key = tenants.get_active_tenant_key()
    if not fallback_key:
        return None

    tenant = tenants.get_tenant(fallback_key)
    if tenant:
        return tenant

    # En ambientes donde el DEFAULT_TENANT existe pero aún no se ha
    # materializado en la base, intentamos registrarlo para evitar que el
    # flujo se bloquee por no encontrar el tenant.
    try:
        return tenants.ensure_default_tenant_registered()
    except Exception:
        logger.exception(
            "No se pudo resolver el tenant para Embedded Signup",
            extra={"tenant_key": fallback_key},
        )
        return None


def _fetch_instagram_backfill_counts(tenant):
    if not tenant:
        return None, None

    try:
        tenants.ensure_tenant_schema(tenant)
        conn = get_connection(db_settings=tenant.as_db_settings(), ensure_database=True)
    except MySQLError:
        logger.exception(
            "No se pudo abrir la conexión para el conteo de backfill Instagram",
            extra={"tenant_key": tenant.tenant_key},
        )
        return None, None

    try:
        c = conn.cursor()
        c.execute(
            """
            SELECT COUNT(DISTINCT conversation_id)
              FROM page_messages
             WHERE tenant_key = %s
               AND platform = %s
            """,
            (tenant.tenant_key, "instagram"),
        )
        conversation_count = (c.fetchone() or [0])[0] or 0

        c.execute(
            """
            SELECT COUNT(*)
              FROM page_messages
             WHERE tenant_key = %s
               AND platform = %s
            """,
            (tenant.tenant_key, "instagram"),
        )
        message_count = (c.fetchone() or [0])[0] or 0
    except MySQLError:
        logger.exception(
            "No se pudo obtener el conteo de backfill Instagram",
            extra={"tenant_key": tenant.tenant_key},
        )
        return None, None
    finally:
        conn.close()

    return conversation_count, message_count


def _normalize_input(text):
    """Normaliza una lista separada por comas."""
    return ','.join(t.strip().lower() for t in (text or '').split(',') if t.strip())


def _normalize_platform(value):
    normalized = (value or '').strip().lower()
    if normalized in {'whatsapp', 'messenger', 'instagram'}:
        return normalized
    return None

def _url_ok(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=5)
        ok = r.status_code == 200
        mime = r.headers.get('Content-Type', '').split(';', 1)[0] if ok else None
        return ok, mime
    except requests.RequestException:
        return False, None


def _fetch_page_accounts(user_token: str):
    if not user_token:
        return {"ok": False, "error": "Falta el token de usuario para consultar páginas."}

    url = f"https://graph.facebook.com/{Config.FACEBOOK_GRAPH_API_VERSION}/me/accounts"
    params = {
        "fields": "id,name,access_token",
        "access_token": user_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException:
        logger.warning("No se pudo conectar con Graph API para listar páginas.")
        return {"ok": False, "error": "No se pudo conectar con la API de Meta."}

    payload = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        details = payload.get("error") if isinstance(payload, dict) else None
        return {
            "ok": False,
            "error": "No se pudieron obtener las páginas.",
            "details": details,
        }

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {"ok": False, "error": "No se encontraron páginas disponibles."}

    pages = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        page_id = entry.get("id")
        if not page_id:
            continue
        pages.append(
            {
                "id": page_id,
                "name": entry.get("name"),
                "access_token": entry.get("access_token"),
            }
        )

    return {"ok": True, "pages": pages}


def _fetch_page_from_token(page_token: str):
    if not page_token:
        return {"ok": False, "error": "Falta el token de página para continuar."}

    url = f"https://graph.facebook.com/{Config.FACEBOOK_GRAPH_API_VERSION}/me"
    params = {
        "fields": "id,name",
        "access_token": page_token,
    }

    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException:
        logger.warning("No se pudo conectar con Graph API para consultar la página.")
        return {"ok": False, "error": "No se pudo conectar con la API de Meta."}

    payload = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        details = payload.get("error") if isinstance(payload, dict) else None
        return {
            "ok": False,
            "error": "No se pudo validar el token de página.",
            "details": details,
        }

    page_id = payload.get("id") if isinstance(payload, dict) else None
    if not page_id:
        return {"ok": False, "error": "No se encontró la página asociada al token."}

    return {
        "ok": True,
        "page": {
            "id": page_id,
            "name": payload.get("name"),
            "access_token": page_token,
        },
    }


def _fetch_instagram_user(user_token: str):
    if not user_token:
        return {"ok": False, "error": "Falta el token de usuario para consultar Instagram."}

    url = f"https://graph.instagram.com/{Config.FACEBOOK_GRAPH_API_VERSION}/me"
    params = {"fields": "user_id,id,username,account_type"}
    headers = {"Authorization": f"Bearer {user_token}"}

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
    except requests.RequestException:
        logger.warning("No se pudo conectar con Graph API para consultar Instagram.")
        return {"ok": False, "error": "No se pudo conectar con la API de Meta."}

    payload = {}
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code >= 400:
        details = payload.get("error") if isinstance(payload, dict) else None
        return {
            "ok": False,
            "error": "No se pudo obtener la cuenta de Instagram.",
            "details": details,
        }

    if not isinstance(payload, dict) or not payload.get("id"):
        return {"ok": False, "error": "No se encontró una cuenta de Instagram asociada."}

    return {"ok": True, "account": payload}


def _exchange_instagram_code_for_token(code: str, redirect_uri: str) -> dict:
    if not code:
        return {"ok": False, "error": "Código de autorización vacío."}
    if not Config.FACEBOOK_APP_ID or not Config.FACEBOOK_APP_SECRET:
        return {
            "ok": False,
            "error": "Falta configurar FACEBOOK_APP_ID o FACEBOOK_APP_SECRET.",
        }

    payload = {
        "client_id": Config.FACEBOOK_APP_ID,
        "client_secret": Config.FACEBOOK_APP_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    }

    try:
        response = requests.post(
            "https://api.instagram.com/oauth/access_token",
            data=payload,
            timeout=15,
        )
    except requests.RequestException:
        logger.warning("No se pudo conectar al endpoint de Instagram OAuth.")
        return {"ok": False, "error": "No se pudo conectar con la API de Instagram."}

    try:
        data = response.json()
    except ValueError:
        data = {}

    if response.status_code >= 400:
        return {
            "ok": False,
            "error": "No se pudo intercambiar el código de Instagram.",
            "details": data,
        }

    access_token = data.get("access_token")
    if not access_token:
        return {"ok": False, "error": "Instagram no devolvió un access_token."}

    try:
        long_response = requests.get(
            "https://graph.instagram.com/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": Config.FACEBOOK_APP_SECRET,
                "access_token": access_token,
            },
            timeout=15,
        )
    except requests.RequestException:
        logger.warning("No se pudo conectar al endpoint de token largo de Instagram.")
        return {"ok": True, "access_token": access_token, "is_long_lived": False}

    try:
        long_data = long_response.json()
    except ValueError:
        long_data = {}

    if long_response.status_code >= 400:
        logger.warning(
            "No se pudo obtener el token largo de Instagram.",
            extra={"details": long_data},
        )
        return {"ok": True, "access_token": access_token, "is_long_lived": False}

    long_token = long_data.get("access_token") or access_token
    return {"ok": True, "access_token": long_token, "is_long_lived": True}


def _handle_instagram_oauth_code(code: str, redirect_uri: str) -> dict:
    tenant = _resolve_signup_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}

    token_response = _exchange_instagram_code_for_token(code, redirect_uri)
    if not token_response.get("ok"):
        return token_response

    access_token = token_response.get("access_token")
    if not access_token:
        return {"ok": False, "error": "No se obtuvo un token de Instagram válido."}

    account_response = _fetch_instagram_user(access_token)
    if not account_response.get("ok"):
        return account_response

    account = account_response.get("account") or {}
    tenant_env = tenants.get_tenant_env(tenant)
    env_updates = {key: tenant_env.get(key) for key in tenants.TENANT_ENV_KEYS}
    env_updates["INSTAGRAM_TOKEN"] = access_token
    if account.get("id"):
        env_updates["INSTAGRAM_ACCOUNT_ID"] = account.get("id")
    if account.get("user_id") or account.get("id"):
        env_updates["INSTAGRAM_PAGE_ID"] = account.get("user_id") or account.get("id")
    tenants.update_tenant_env(tenant.tenant_key, env_updates)
    tenants.update_tenant_metadata(
        tenant.tenant_key,
        {"instagram_account": account},
    )
    tenants.trigger_page_backfill_for_platform(tenant, "instagram")

    logger.info(
        "Token de Instagram actualizado desde OAuth",
        extra={
            "tenant_key": tenant.tenant_key,
            "instagram_account_id": account.get("id"),
            "instagram_username": account.get("username"),
            "is_long_lived": token_response.get("is_long_lived"),
        },
    )
    return {"ok": True, "account": account}


def _resolve_instagram_redirect_uri(fallback: str) -> str:
    signup_url = (Config.SIGNUP_INSTRAGRAM or "").strip()
    if not signup_url:
        return fallback
    try:
        parsed = urlparse(signup_url)
    except ValueError:
        return fallback
    if not parsed.query:
        return fallback
    for entry in parsed.query.split("&"):
        if not entry:
            continue
        key, _, value = entry.partition("=")
        if key == "redirect_uri" and value:
            return value
    return fallback


def _resolve_page_user_token(platform: str | None, tenant_env: dict, provided_token: str) -> str:
    normalized = (platform or "").strip().lower()
    if normalized == "instagram":
        return (
            provided_token
            or (tenant_env.get("INSTAGRAM_TOKEN") or "").strip()
            or (tenant_env.get("MESSENGER_TOKEN") or "").strip()
        )
    return provided_token or (tenant_env.get("MESSENGER_TOKEN") or "").strip()


def _resolve_user_token_key(platform: str | None) -> str:
    normalized = (platform or "").strip().lower()
    if normalized == "instagram":
        return "INSTAGRAM_TOKEN"
    return "MESSENGER_TOKEN"


def _resolve_page_env_key(platform: str, key: str) -> str:
    normalized = (platform or "").strip().lower()
    if normalized == "instagram":
        return f"INSTAGRAM_{key}"
    return f"MESSENGER_{key}"


def _resolve_page_env_value(platform: str, tenant_env: dict) -> str:
    normalized = (platform or "").strip().lower()
    page_id = (tenant_env.get(_resolve_page_env_key(normalized, "PAGE_ID")) or "").strip()
    if page_id:
        return page_id
    legacy_platform = (tenant_env.get("PLATFORM") or "").strip().lower()
    if legacy_platform == normalized:
        return (tenant_env.get("PAGE_ID") or "").strip()
    return ""


def _normalize_page_selection(metadata: dict | None) -> dict:
    selection = {}
    if not isinstance(metadata, dict):
        return selection

    raw = metadata.get("page_selection")
    if not isinstance(raw, dict):
        return selection

    if "messenger" in raw or "instagram" in raw:
        for platform in ("messenger", "instagram"):
            entry = raw.get(platform)
            if isinstance(entry, dict) and entry.get("page_id"):
                selection[platform] = {
                    "page_id": entry.get("page_id"),
                    "page_name": entry.get("page_name"),
                }
        return selection

    platform = (raw.get("platform") or "").strip().lower()
    if platform in {"messenger", "instagram"} and raw.get("page_id"):
        selection[platform] = {
            "page_id": raw.get("page_id"),
            "page_name": raw.get("page_name"),
        }

    return selection


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
        c.execute("SHOW COLUMNS FROM reglas LIKE 'platform';")
        if not c.fetchone():
            c.execute("ALTER TABLE reglas ADD COLUMN platform VARCHAR(20) NULL;")
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
                    datos = list(fila) + [None] * 12
                    step, input_text, respuesta, siguiente_step, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler, platform_raw = datos[:12]
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
                    platform = _normalize_platform(platform_raw)

                    c.execute(
                        """
                        SELECT id FROM reglas
                         WHERE step = %s AND input_text = %s
                           AND (
                               platform = %s
                               OR (%s IS NULL AND (platform IS NULL OR platform = ''))
                           )
                        """,
                        (step, input_text, platform, platform),
                    )
                    existente = c.fetchone()
                    if existente:
                        regla_id = existente[0]
                        c.execute(
                            """
                            UPDATE reglas
                               SET respuesta = %s,
                                   siguiente_step = %s,
                                   platform = %s,
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
                                platform,
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
                        if media_url and url_ok:
                            c.execute(
                                "INSERT INTO regla_medias (regla_id, media_url, media_tipo) VALUES (%s, %s, %s)",
                                (regla_id, media_url, media_tipo),
                            )
                    else:
                        c.execute(
                            "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, platform, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                step,
                                input_text,
                                respuesta,
                                siguiente_step,
                                platform,
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
                platform = _normalize_platform(request.form.get('platform'))

                if regla_id:
                    c.execute(
                        """
                        UPDATE reglas
                           SET step = %s,
                               input_text = %s,
                               respuesta = %s,
                               siguiente_step = %s,
                               platform = %s,
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
                            platform,
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
                        """
                        SELECT id FROM reglas
                         WHERE step = %s AND input_text = %s
                           AND (
                               platform = %s
                               OR (%s IS NULL AND (platform IS NULL OR platform = ''))
                           )
                        """,
                        (step, input_text, platform, platform),
                    )
                    existente = c.fetchone()
                    if existente:
                        regla_id = existente[0]
                        c.execute(
                            """
                            UPDATE reglas
                               SET respuesta = %s,
                                   siguiente_step = %s,
                                   platform = %s,
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
                                platform,
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
                            "INSERT INTO reglas (step, input_text, respuesta, siguiente_step, platform, tipo, media_url, media_tipo, opciones, rol_keyword, calculo, handler) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                step,
                                input_text,
                                respuesta,
                                siguiente_step,
                                platform,
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
            SELECT r.id, r.step, r.input_text, r.respuesta, r.siguiente_step, r.platform, r.tipo,
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
                'platform': row[5],
                'tipo': row[6],
                'media_urls': (row[7] or '').split('||') if row[7] else [],
                'media_tipos': (row[8] or '').split('||') if row[8] else [],
                'opciones': row[9] or '',
                'rol_keyword': row[10],
                'calculo': row[11],
                'handler': row[12],
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
        tenant_env = dict(tenants.get_current_tenant_env() or {})
        instagram_token_present = bool((tenant_env.get("INSTAGRAM_TOKEN") or "").strip())
        chat_state_definitions = get_chat_state_definitions(include_hidden=True)
        return render_template(
            template_name,
            reglas=reglas,
            chat_state_definitions=chat_state_definitions,
            instagram_token_present=instagram_token_present,
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

    oauth_code = (request.args.get("code") or "").strip()
    if oauth_code:
        redirect_uri = _resolve_instagram_redirect_uri(request.base_url)
        result = _handle_instagram_oauth_code(oauth_code, redirect_uri)
        if not result.get("ok"):
            logger.warning(
                "No se pudo procesar el código de Instagram OAuth",
                extra={"error": result.get("error"), "details": result.get("details")},
            )
        return redirect(url_for("configuracion.configuracion_signup"))

    tenant = _resolve_signup_tenant()
    tenant_key = tenant.tenant_key if tenant else tenants.get_active_tenant_key()
    tenant_env = tenants.get_tenant_env(tenant)
    page_selection = _normalize_page_selection(tenant.metadata if tenant else None)
    messenger_page_selection = page_selection.get("messenger") or {}
    instagram_account = {}
    if tenant and isinstance(tenant.metadata, dict):
        instagram_account = tenant.metadata.get("instagram_account") or {}
    instagram_conversation_count, instagram_message_count = _fetch_instagram_backfill_counts(tenant)

    logger.info(
        "Renderizando signup embebido",
        extra={
            "tenant_key": tenant_key,
            "facebook_app_id_configured": bool(Config.FACEBOOK_APP_ID),
            "signup_config_code_present": bool(Config.SIGNUP_FACEBOOK),
        },
    )

    return render_template(
        'configuracion_signup.html',
        signup_config_code=Config.SIGNUP_FACEBOOK,
        facebook_app_id=Config.FACEBOOK_APP_ID,
        signup_instagram_url=Config.SIGNUP_INSTRAGRAM,
        tenant_key=tenant_key,
        tenant_waba_id=tenant_env.get("WABA_ID"),
        tenant_phone_number_id=tenant_env.get("PHONE_NUMBER_ID"),
        messenger_page_id=_resolve_page_env_value("messenger", tenant_env),
        messenger_page_name=messenger_page_selection.get("page_name"),
        instagram_account_name=instagram_account.get("username") or instagram_account.get("id"),
        instagram_token_present=bool((tenant_env.get("INSTAGRAM_TOKEN") or "").strip()),
        instagram_conversation_count=instagram_conversation_count,
        instagram_message_count=instagram_message_count,
    )


@config_bp.route('/configuracion/instagram/callback', methods=['GET'])
def instagram_oauth_callback():
    if not _require_admin():
        return redirect(url_for("auth.login"))

    oauth_code = (request.args.get("code") or "").strip()
    if not oauth_code:
        return redirect(url_for("configuracion.configuracion_signup"))

    redirect_uri = _resolve_instagram_redirect_uri(request.base_url)
    result = _handle_instagram_oauth_code(oauth_code, redirect_uri)
    if not result.get("ok"):
        logger.warning(
            "No se pudo completar el callback de Instagram OAuth",
            extra={"error": result.get("error"), "details": result.get("details")},
        )
    return redirect(url_for("configuracion.configuracion_signup"))


@config_bp.route('/configuracion/signup', methods=['POST'])
def save_signup():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = _resolve_signup_tenant()
    if not tenant:
        logger.warning("Signup embebido falló: tenant actual no encontrado")
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        logger.exception("Signup embebido falló al parsear el payload JSON")
        return {"ok": False, "error": "Payload inválido"}, 400

    logger.info(
        "Procesando signup embebido",
        extra={
            "tenant_key": tenant.tenant_key,
            "payload_keys": sorted(list(payload.keys())),
        },
    )
    logger.info(
        "Payload completo de signup embebido",
        extra={
            "tenant_key": tenant.tenant_key,
            "payload": payload,
        },
    )

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

    logger.info(
        "Actualizando entorno con datos del signup embebido",
        extra={
            "tenant_key": tenant.tenant_key,
            "has_meta_token": bool(env_updates.get("META_TOKEN")),
            "has_long_lived": bool(env_updates.get("LONG_LIVED_TOKEN")),
            "has_phone_number_id": bool(env_updates.get("PHONE_NUMBER_ID")),
            "has_waba_id": bool(env_updates.get("WABA_ID")),
            "has_business_id": bool(env_updates.get("BUSINESS_ID")),
        },
    )

    business_info = payload.get("business") or payload.get("business_info")
    metadata_updates = {
        "embedded_signup_payload": payload,
    }
    if isinstance(business_info, dict) and business_info:
        metadata_updates["whatsapp_business"] = business_info

    if metadata_updates:
        logger.info(
            "Guardando metadata de negocio desde signup embebido",
            extra={
                "tenant_key": tenant.tenant_key,
                "metadata_fields": sorted(list(metadata_updates.keys())),
            },
        )
    else:
        logger.info(
            "No se encontró metadata de negocio en el payload de signup",
            extra={"tenant_key": tenant.tenant_key},
        )

    tenants.update_tenant_env(tenant.tenant_key, env_updates)
    if metadata_updates:
        tenants.update_tenant_metadata(tenant.tenant_key, metadata_updates)

    return {
        "ok": True,
        "message": "Credenciales de WhatsApp actualizadas.",
        "env": tenants.get_tenant_env(tenant),
    }


@config_bp.route('/configuracion/whatsapp/phone-numbers', methods=['GET'])
def whatsapp_phone_numbers():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = _resolve_signup_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    tenant_env = tenants.get_tenant_env(tenant)
    token = tenant_env.get("META_TOKEN")
    waba_id = tenant_env.get("WABA_ID")

    response = list_phone_numbers(token, waba_id)
    status = 200 if response.get("ok") else 400
    return response, status


@config_bp.route('/configuracion/whatsapp/phone-number', methods=['POST'])
def whatsapp_save_phone_number():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = _resolve_signup_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error": "Payload inválido"}, 400

    phone_number_id = (payload.get("phone_number_id") or "").strip()
    if not phone_number_id:
        return {"ok": False, "error": "Selecciona un número válido."}, 400

    current_env = tenants.get_tenant_env(tenant)
    env_updates = {key: current_env.get(key) for key in tenants.TENANT_ENV_KEYS}
    env_updates["PHONE_NUMBER_ID"] = phone_number_id

    tenants.update_tenant_env(tenant.tenant_key, env_updates)

    return {
        "ok": True,
        "message": "Número de WhatsApp actualizado.",
        "env": tenants.get_tenant_env(tenant),
    }


@config_bp.route('/configuracion/messenger/pages', methods=['POST'])
def messenger_pages():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = _resolve_signup_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error": "Payload inválido"}, 400

    platform = (payload.get("platform") or "").strip().lower() or "messenger"
    if platform != "messenger":
        return {"ok": False, "error": "Solo puedes consultar páginas de Messenger."}, 400
    provided_token = (payload.get("user_access_token") or "").strip()
    tenant_env = tenants.get_tenant_env(tenant)
    token = _resolve_page_user_token(platform, tenant_env, provided_token)

    if provided_token:
        env_updates = {key: tenant_env.get(key) for key in tenants.TENANT_ENV_KEYS}
        env_updates[_resolve_user_token_key(platform)] = provided_token
        tenants.update_tenant_env(tenant.tenant_key, env_updates)

    response = _fetch_page_accounts(token)
    if not response.get("ok"):
        return response, 400

    pages = response.get("pages", [])
    return {"ok": True, "pages": [{"id": page["id"], "name": page.get("name")} for page in pages]}


@config_bp.route('/configuracion/messenger/page', methods=['POST'])
def messenger_save_page():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = _resolve_signup_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error": "Payload inválido"}, 400

    page_id = (payload.get("page_id") or "").strip()
    platform = (payload.get("platform") or "").strip().lower()
    provided_token = (payload.get("user_access_token") or "").strip()
    provided_page_token = (payload.get("page_access_token") or "").strip()

    if platform != "messenger":
        return {"ok": False, "error": "Selecciona una plataforma válida."}, 400

    tenant_env = tenants.get_tenant_env(tenant)
    page_entry = None
    if provided_page_token:
        response = _fetch_page_from_token(provided_page_token)
        if not response.get("ok"):
            return response, 400
        page_entry = response.get("page")
        if not page_entry:
            return {"ok": False, "error": "No se pudo obtener la página del token."}, 400
        if page_id and str(page_entry.get("id")) != page_id:
            return {"ok": False, "error": "El token no corresponde a la página indicada."}, 400
    else:
        if not page_id:
            return {"ok": False, "error": "Selecciona una página válida."}, 400
        token = _resolve_page_user_token(platform, tenant_env, provided_token)
        response = _fetch_page_accounts(token)
        if not response.get("ok"):
            return response, 400

        for page in response.get("pages", []):
            if str(page.get("id")) == page_id:
                page_entry = page
                break

        if not page_entry or not page_entry.get("access_token"):
            return {"ok": False, "error": "No se pudo obtener el token de la página."}, 400

    if not page_entry or not page_entry.get("access_token"):
        return {"ok": False, "error": "No se pudo obtener el token de la página."}, 400

    env_updates = {key: tenant_env.get(key) for key in tenants.TENANT_ENV_KEYS}
    env_updates[_resolve_page_env_key(platform, "PAGE_ID")] = page_entry.get("id")
    env_updates[_resolve_page_env_key(platform, "PAGE_ACCESS_TOKEN")] = page_entry.get("access_token")
    env_updates["PLATFORM"] = platform
    if provided_token:
        env_updates[_resolve_user_token_key(platform)] = provided_token

    tenants.update_tenant_env(tenant.tenant_key, env_updates)

    page_selection = _normalize_page_selection(tenant.metadata if tenant else None)
    page_selection[platform] = {
        "page_id": page_entry.get("id"),
        "page_name": page_entry.get("name"),
    }
    tenants.update_tenant_metadata(
        tenant.tenant_key,
        {"page_selection": page_selection},
    )

    return {
        "ok": True,
        "message": "Página actualizada correctamente.",
        "page": {
            "id": page_entry.get("id"),
            "name": page_entry.get("name"),
            "platform": platform,
        },
    }


@config_bp.route('/configuracion/instagram/token', methods=['POST'])
def instagram_save_token():
    if not _require_admin():
        return {"ok": False, "error": "No autorizado"}, 403

    tenant = _resolve_signup_tenant()
    if not tenant:
        return {"ok": False, "error": "No se encontró la empresa actual."}, 400

    try:
        payload = request.get_json(force=True) or {}
    except Exception:
        return {"ok": False, "error": "Payload inválido"}, 400

    user_token = (payload.get("user_access_token") or "").strip()
    if not user_token:
        return {"ok": False, "error": "Ingresa un token de Instagram válido."}, 400

    response = _fetch_instagram_user(user_token)
    if not response.get("ok"):
        return response, 400

    account = response.get("account") or {}
    tenant_env = tenants.get_tenant_env(tenant)
    env_updates = {key: tenant_env.get(key) for key in tenants.TENANT_ENV_KEYS}
    env_updates["INSTAGRAM_TOKEN"] = user_token
    if account.get("id"):
        env_updates["INSTAGRAM_ACCOUNT_ID"] = account.get("id")
    if account.get("user_id") or account.get("id"):
        env_updates["INSTAGRAM_PAGE_ID"] = account.get("user_id") or account.get("id")
    tenants.update_tenant_env(tenant.tenant_key, env_updates)
    tenants.trigger_page_backfill_for_platform(tenant, "instagram")
    tenants.update_tenant_metadata(
        tenant.tenant_key,
        {"instagram_account": account},
    )
    logger.info(
        "Token de Instagram actualizado",
        extra={
            "tenant_key": tenant.tenant_key,
            "instagram_account_id": account.get("id"),
            "instagram_username": account.get("username"),
        },
    )

    return {
        "ok": True,
        "message": "Token de Instagram actualizado.",
        "account": account,
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
