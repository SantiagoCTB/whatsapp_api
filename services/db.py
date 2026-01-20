import contextvars
import importlib.util
import json
import os
import re
import contextvars
import sys
import logging
from dataclasses import dataclass
from datetime import datetime

if importlib.util.find_spec("mysql.connector"):
    import mysql.connector
    from mysql.connector import errorcode
    from mysql.connector.errors import Error, IntegrityError, ProgrammingError
    _MYSQL_AVAILABLE = True
else:  # pragma: no cover - fallback para entornos sin el conector
    mysql = None  # type: ignore[assignment]

    class _Errorcode:
        ER_BAD_DB_ERROR = 1049

    errorcode = _Errorcode()

    class Error(Exception):
        """Error base utilizado cuando no está disponible mysql.connector."""

    class ProgrammingError(Error):
        """Error de programación genérico de SQL."""

    class IntegrityError(Error):
        """Error de integridad genérico de SQL."""

    _MYSQL_AVAILABLE = False

from config import Config


logger = logging.getLogger(__name__)


FLOW_RESPONSES_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS flow_responses (
      id INT AUTO_INCREMENT PRIMARY KEY,
      numero VARCHAR(20) NOT NULL,
      flow_name VARCHAR(255),
      response_json LONGTEXT,
      wa_id VARCHAR(255),
      timestamp DATETIME
    ) ENGINE=InnoDB;
"""

TENANTS_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS tenants (
      id INT AUTO_INCREMENT PRIMARY KEY,
      tenant_key VARCHAR(64) NOT NULL UNIQUE,
      name VARCHAR(191) NOT NULL,
      db_name VARCHAR(191) NOT NULL,
      db_host VARCHAR(191) NOT NULL,
      db_port INT NOT NULL DEFAULT 3306,
      db_user VARCHAR(191) NOT NULL,
      db_password TEXT NOT NULL,
      metadata JSON NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    ) ENGINE=InnoDB;
"""

CHAT_STATE_DEFAULTS = [
    {
        "key": "esperando_respuesta",
        "label": "Esperando respuesta",
        "color": "#f0ad4e",
        "text_color": "#1f1f1f",
        "priority": 40,
        "visible": 1,
    },
    {
        "key": "asesor",
        "label": "Asesor",
        "color": "#28a745",
        "text_color": "#ffffff",
        "priority": 30,
        "visible": 1,
    },
    {
        "key": "en_flujo",
        "label": "En flujo",
        "color": "#0d6efd",
        "text_color": "#ffffff",
        "priority": 20,
        "visible": 1,
    },
    {
        "key": "inactivo",
        "label": "Inactivo",
        "color": "#6c757d",
        "text_color": "#ffffff",
        "priority": 10,
        "visible": 1,
    },
    {
        "key": "error_flujo",
        "label": "Error de flujo",
        "color": "#dc3545",
        "text_color": "#ffffff",
        "priority": 50,
        "visible": 1,
    },
]


def _ensure_chat_state_definitions(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_state_definitions (
          state_key VARCHAR(40) PRIMARY KEY,
          label VARCHAR(100) NOT NULL,
          color_hex VARCHAR(10) NOT NULL,
          text_color_hex VARCHAR(10) NOT NULL,
          priority INT NOT NULL DEFAULT 0,
          visible TINYINT(1) NOT NULL DEFAULT 1
        ) ENGINE=InnoDB;
        """
    )

    cursor.execute("SHOW COLUMNS FROM chat_state_definitions LIKE 'text_color_hex';")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE chat_state_definitions ADD COLUMN text_color_hex VARCHAR(10) NOT NULL DEFAULT '#ffffff';"
        )

    cursor.execute("SHOW COLUMNS FROM chat_state_definitions LIKE 'priority';")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE chat_state_definitions ADD COLUMN priority INT NOT NULL DEFAULT 0;"
        )

    cursor.execute("SHOW COLUMNS FROM chat_state_definitions LIKE 'visible';")
    if not cursor.fetchone():
        cursor.execute(
            "ALTER TABLE chat_state_definitions ADD COLUMN visible TINYINT(1) NOT NULL DEFAULT 1;"
        )


def _seed_chat_state_definitions(cursor):
    for definition in CHAT_STATE_DEFAULTS:
        cursor.execute(
            """
            INSERT INTO chat_state_definitions
                (state_key, label, color_hex, text_color_hex, priority, visible)
            SELECT %s, %s, %s, %s, %s, %s
            FROM DUAL
            WHERE NOT EXISTS (
                SELECT 1 FROM chat_state_definitions WHERE state_key = %s
            )
            """,
            (
                definition["key"],
                definition["label"],
                definition["color"],
                definition["text_color"],
                definition["priority"],
                definition["visible"],
                definition["key"],
            ),
        )


@dataclass(frozen=True)
class DatabaseSettings:
    host: str
    port: int
    user: str
    password: str
    name: str


def _should_use_dummy_db() -> bool:
    return (
        os.getenv("INIT_DB_ON_START", "1") == "0"
        or "pytest" in sys.modules
        or "PYTEST_CURRENT_TEST" in os.environ
        or not (Config.DB_HOST and Config.DB_USER and Config.DB_PASSWORD)
    )


class _DummyCursor:
    def __init__(self):
        self.last_query = None

    def execute(self, *args, **kwargs):
        self.last_query = (args, kwargs)

    def executemany(self, *args, **kwargs):
        self.last_query = (args, kwargs)

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        return None


class _DummyConnection:
    def __init__(self):
        self.closed = False

    def cursor(self, *_, **__):
        return _DummyCursor()

    def commit(self):
        return None

    def close(self):
        self.closed = True


_TENANT_DB_SETTINGS = contextvars.ContextVar("tenant_db_settings", default=None)
_TENANT_KEY = contextvars.ContextVar("tenant_key", default=None)


def set_tenant_db_settings(db_settings: DatabaseSettings | None):
    _TENANT_DB_SETTINGS.set(db_settings)


def clear_tenant_db_settings():
    _TENANT_DB_SETTINGS.set(None)


def set_current_tenant_key(tenant_key: str | None):
    _TENANT_KEY.set(tenant_key)


def clear_current_tenant_key():
    _TENANT_KEY.set(None)


def get_current_tenant_key() -> str | None:
    return _TENANT_KEY.get()


def _default_db_settings() -> DatabaseSettings:
    if _should_use_dummy_db():
        return DatabaseSettings(host="", port=0, user="", password="", name="")

    if not Config.DB_NAME:
        raise RuntimeError("DB_NAME no está configurado; no se puede crear la base.")

    return DatabaseSettings(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        name=Config.DB_NAME,
    )


_BASE_DB_SETTINGS = _default_db_settings()


def _require_mysql_connector():
    if _MYSQL_AVAILABLE:
        return
    raise RuntimeError(
        "mysql-connector-python no está instalado; instala la dependencia o "
        "configura INIT_DB_ON_START=0 para omitir la inicialización de base de datos."
    )


def _create_database_if_missing(db_settings: DatabaseSettings):
    """Create the configured database if it does not exist yet."""

    _require_mysql_connector()

    credential_options: list[tuple[str, str | None]] = []

    if db_settings.user:
        credential_options.append((db_settings.user, db_settings.password))

    if Config.DB_USER and Config.DB_PASSWORD:
        credential_options.append((Config.DB_USER, Config.DB_PASSWORD))

    if Config.DB_ROOT_PASSWORD:
        credential_options.append(("root", Config.DB_ROOT_PASSWORD))

    if not credential_options:
        raise RuntimeError(
            "No hay credenciales disponibles para crear la base de datos del tenant."
        )

    last_error: Error | None = None
    for user, password in credential_options:
        try:
            bootstrap_conn = mysql.connector.connect(
                host=db_settings.host,
                port=db_settings.port,
                user=user,
                password=password,
            )
            try:
                cursor = bootstrap_conn.cursor()
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db_settings.name}` "
                    "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
                )
                return
            finally:
                bootstrap_conn.close()
        except Error as exc:  # pragma: no cover - depends on DB privileges
            last_error = exc
            continue

    if last_error:
        raise last_error


def _resolve_db_settings(
    db_settings: DatabaseSettings | None, allow_tenant_context: bool = True
) -> DatabaseSettings:
    if db_settings:
        return db_settings

    if allow_tenant_context:
        ctx_settings = _TENANT_DB_SETTINGS.get()
        if ctx_settings:
            return ctx_settings

    return _BASE_DB_SETTINGS


def get_connection(
    ensure_database: bool = False,
    db_settings: DatabaseSettings | None = None,
    *,
    allow_tenant_context: bool = True,
):
    if _should_use_dummy_db():
        return _DummyConnection()

    _require_mysql_connector()

    target_settings = _resolve_db_settings(db_settings, allow_tenant_context)
    try:
        return mysql.connector.connect(
            host=target_settings.host,
            port=target_settings.port,
            user=target_settings.user,
            password=target_settings.password,
            database=target_settings.name,
        )
    except Error as exc:
        if ensure_database and exc.errno == errorcode.ER_BAD_DB_ERROR:
            _create_database_if_missing(target_settings)
            return mysql.connector.connect(
                host=target_settings.host,
                port=target_settings.port,
                user=target_settings.user,
                password=target_settings.password,
                database=target_settings.name,
            )
        raise


def get_master_connection(ensure_database: bool = False):
    return get_connection(
        ensure_database=ensure_database,
        db_settings=_BASE_DB_SETTINGS,
        allow_tenant_context=False,
    )


def _ensure_auth_schema_and_seed(cursor, admin_hash: str):
    # usuarios
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
      id INT AUTO_INCREMENT PRIMARY KEY,
      username VARCHAR(50) UNIQUE NOT NULL,
      password VARCHAR(128) NOT NULL
    ) ENGINE=InnoDB;
    """)

    # Ampliar password para soportar hashes de Werkzeug
    cursor.execute("SHOW COLUMNS FROM usuarios LIKE 'password';")
    col = cursor.fetchone()
    # col -> (Field, Type, Null, Key, Default, Extra)
    if col and isinstance(col[1], str) and 'varchar(128)' in col[1].lower():
        cursor.execute("ALTER TABLE usuarios MODIFY password VARCHAR(255) NOT NULL;")

    # roles
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS roles (
      id INT AUTO_INCREMENT PRIMARY KEY,
      name VARCHAR(50) NOT NULL,
      keyword VARCHAR(20) UNIQUE NOT NULL
    ) ENGINE=InnoDB;
    """)

    # user_roles (pivote con FKs)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_roles (
      user_id INT NOT NULL,
      role_id INT NOT NULL,
      PRIMARY KEY (user_id, role_id),
      FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE,
      FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
    ) ENGINE=InnoDB;
    """)

    # Migración: si existe usuarios.rol => poblar roles/user_roles y DROP columna
    cursor.execute("SHOW COLUMNS FROM usuarios LIKE 'rol';")
    if cursor.fetchone():
        cursor.execute("SELECT DISTINCT rol FROM usuarios;")
        for (rol,) in cursor.fetchall():
            if not rol:
                continue
            cursor.execute("""
                INSERT INTO roles (name, keyword)
                SELECT %s, %s FROM DUAL
                WHERE NOT EXISTS (SELECT 1 FROM roles WHERE keyword=%s)
            """, (rol.capitalize(), rol, rol))

        cursor.execute("SELECT id, rol FROM usuarios;")
        for user_id, rol in cursor.fetchall():
            if not rol:
                continue
            cursor.execute("SELECT id FROM roles WHERE keyword=%s", (rol,))
            row = cursor.fetchone()
            if row:
                role_id = row[0]
                cursor.execute(
                    "INSERT IGNORE INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                    (user_id, role_id)
                )

        cursor.execute("ALTER TABLE usuarios DROP COLUMN rol;")

    cursor.execute("""
    INSERT INTO usuarios (username, password)
      SELECT %s, %s FROM DUAL
      WHERE NOT EXISTS (SELECT 1 FROM usuarios WHERE username=%s)
    """, ('admin', admin_hash, 'admin'))

    cursor.execute("""
    INSERT INTO usuarios (username, password)
      SELECT %s, %s FROM DUAL
      WHERE NOT EXISTS (SELECT 1 FROM usuarios WHERE username=%s)
    """, ('superadmin', admin_hash, 'superadmin'))

    cursor.execute("""
    INSERT INTO roles (name, keyword)
      SELECT %s, %s FROM DUAL
      WHERE NOT EXISTS (SELECT 1 FROM roles WHERE keyword=%s)
    """, ('Administrador', 'admin', 'admin'))

    cursor.execute("""
    INSERT INTO roles (name, keyword)
      SELECT %s, %s FROM DUAL
      WHERE NOT EXISTS (SELECT 1 FROM roles WHERE keyword=%s)
    """, ('Super Administrador', 'superadmin', 'superadmin'))

    cursor.execute("""
    INSERT INTO roles (name, keyword)
      SELECT %s, %s FROM DUAL
      WHERE NOT EXISTS (SELECT 1 FROM roles WHERE keyword=%s)
    """, ('Tiquetes', 'tiquetes', 'tiquetes'))

    cursor.execute("""
    INSERT INTO roles (name, keyword)
      SELECT %s, %s FROM DUAL
      WHERE NOT EXISTS (SELECT 1 FROM roles WHERE keyword=%s)
    """, ('Cotizar', 'cotizar', 'cotizar'))

    cursor.execute("""
    INSERT IGNORE INTO user_roles (user_id, role_id)
    SELECT u.id, r.id
      FROM usuarios u, roles r
     WHERE u.username=%s AND r.keyword=%s
    """, ('admin', 'admin'))

    cursor.execute("""
    INSERT IGNORE INTO user_roles (user_id, role_id)
    SELECT u.id, r.id
      FROM usuarios u, roles r
     WHERE u.username=%s AND r.keyword=%s
    """, ('admin', 'superadmin'))

    cursor.execute("""
    INSERT IGNORE INTO user_roles (user_id, role_id)
    SELECT u.id, r.id
      FROM usuarios u, roles r
     WHERE u.username=%s AND r.keyword=%s
    """, ('superadmin', 'admin'))

    cursor.execute("""
    INSERT IGNORE INTO user_roles (user_id, role_id)
    SELECT u.id, r.id
      FROM usuarios u, roles r
     WHERE u.username=%s AND r.keyword=%s
    """, ('superadmin', 'superadmin'))


def init_master_db():
    conn = get_master_connection(ensure_database=True)
    c = conn.cursor()
    c.execute(TENANTS_TABLE_DDL)
    _ensure_auth_schema_and_seed(c, Config.DEFAULT_ADMIN_PASSWORD_HASH)
    conn.commit()
    conn.close()


def init_db(db_settings: DatabaseSettings | None = None):
    conn = get_connection(ensure_database=True, db_settings=db_settings)
    c = conn.cursor()

    # mensajes
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes (
      id INT AUTO_INCREMENT PRIMARY KEY,
      wa_id VARCHAR(255),
      reply_to_wa_id VARCHAR(255),
      numero     VARCHAR(20),
      mensaje    TEXT,
      tipo       VARCHAR(50),
      media_id   VARCHAR(255),
      media_url  TEXT,
      mime_type  TEXT,
      opciones   TEXT,
      link_url   TEXT,
      link_title TEXT,
      link_body  TEXT,
      link_thumb TEXT,
      step       TEXT,
      regla_id   INT,
      timestamp  DATETIME
    ) ENGINE=InnoDB;
    """)

    # Migración defensiva de columnas link_*
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'link_url';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN link_url TEXT NULL;")
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'link_title';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN link_title TEXT NULL;")
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'link_body';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN link_body TEXT NULL;")
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'link_thumb';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN link_thumb TEXT NULL;")

    c.execute("SHOW COLUMNS FROM mensajes LIKE 'opciones';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN opciones TEXT NULL;")

    # Migración defensiva de columnas wa_id y reply_to_wa_id
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'wa_id';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN wa_id VARCHAR(255) NULL;")
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'reply_to_wa_id';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN reply_to_wa_id VARCHAR(255) NULL;")

    # Migración defensiva de columnas step y regla_id
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'step';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN step TEXT NULL;")
    c.execute("SHOW COLUMNS FROM mensajes LIKE 'regla_id';")
    if not c.fetchone():
        c.execute("ALTER TABLE mensajes ADD COLUMN regla_id INT NULL;")

    # Índice sobre timestamp para mejorar el ordenamiento cronológico
    c.execute("SHOW INDEX FROM mensajes WHERE Key_name = 'idx_mensajes_timestamp';")
    if not c.fetchone():
        c.execute("CREATE INDEX idx_mensajes_timestamp ON mensajes (timestamp);")

    # mensajes procesados
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes_procesados (
      mensaje_id VARCHAR(255) PRIMARY KEY
    ) ENGINE=InnoDB;
    """)

    # mensajes de backfill (Messenger/Instagram)
    c.execute("""
    CREATE TABLE IF NOT EXISTS page_messages (
      id INT AUTO_INCREMENT PRIMARY KEY,
      tenant_key VARCHAR(64),
      platform VARCHAR(20),
      page_id VARCHAR(64),
      conversation_id VARCHAR(255),
      message_id VARCHAR(255) NOT NULL,
      created_time TEXT,
      from_id VARCHAR(255),
      to_ids_json TEXT,
      message TEXT,
      reply_to_mid VARCHAR(255),
      is_self_reply TINYINT(1),
      inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uniq_page_messages (tenant_key, message_id),
      INDEX idx_page_messages_tenant (tenant_key),
      INDEX idx_page_messages_platform (platform)
    ) ENGINE=InnoDB;
    """)

    # conversaciones de backfill (Messenger/Instagram)
    c.execute("""
    CREATE TABLE IF NOT EXISTS page_conversations (
      id INT AUTO_INCREMENT PRIMARY KEY,
      tenant_key VARCHAR(64),
      platform VARCHAR(20),
      conversation_id VARCHAR(255),
      self_id VARCHAR(255),
      contact_id VARCHAR(255),
      updated_time TEXT,
      inserted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uniq_page_conversations (tenant_key, platform, conversation_id),
      INDEX idx_page_conversations_tenant (tenant_key),
      INDEX idx_page_conversations_platform (platform)
    ) ENGINE=InnoDB;
    """)

    # estados de mensajes (callbacks de status)
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes_status (
      id INT AUTO_INCREMENT PRIMARY KEY,
      wa_id VARCHAR(255) NOT NULL,
      status VARCHAR(50) NOT NULL,
      status_timestamp BIGINT,
      recipient_id VARCHAR(32),
      error_code INT,
      error_title TEXT,
      error_message TEXT,
      error_details TEXT,
      payload_json LONGTEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      UNIQUE KEY uniq_mensajes_status (wa_id, status, status_timestamp, recipient_id),
      INDEX idx_mensajes_status_wa_id (wa_id),
      INDEX idx_mensajes_status_status (status)
    ) ENGINE=InnoDB;
    """)

    # respuestas de flujos (Flow)
    c.execute(FLOW_RESPONSES_TABLE_DDL)

    _ensure_auth_schema_and_seed(c, Config.DEFAULT_ADMIN_PASSWORD_HASH)

    # reglas (incluye rol_keyword alineado a roles.keyword)
    c.execute("""
    CREATE TABLE IF NOT EXISTS reglas (
      id INT AUTO_INCREMENT PRIMARY KEY,
      step TEXT NOT NULL,
      input_text TEXT NOT NULL,
      respuesta TEXT NOT NULL,
      siguiente_step TEXT,
      platform VARCHAR(20) NULL,
      tipo VARCHAR(20) NOT NULL DEFAULT 'texto',
      opciones TEXT,
      rol_keyword VARCHAR(20) NULL,
      calculo TEXT,
      handler VARCHAR(50),
      media_url TEXT,
      media_tipo VARCHAR(20)
    ) ENGINE=InnoDB;
    """)

    # Migración defensiva de columnas platform, calculo, handler y medios
    c.execute("SHOW COLUMNS FROM reglas LIKE 'platform';")
    if not c.fetchone():
        c.execute("ALTER TABLE reglas ADD COLUMN platform VARCHAR(20) NULL;")
    c.execute("SHOW COLUMNS FROM reglas LIKE 'calculo';")
    if not c.fetchone():
        c.execute("ALTER TABLE reglas ADD COLUMN calculo TEXT NULL;")
    c.execute("SHOW COLUMNS FROM reglas LIKE 'handler';")
    if not c.fetchone():
        c.execute("ALTER TABLE reglas ADD COLUMN handler VARCHAR(50) NULL;")
    c.execute("SHOW COLUMNS FROM reglas LIKE 'media_url';")
    if not c.fetchone():
        c.execute("ALTER TABLE reglas ADD COLUMN media_url TEXT NULL;")
    c.execute("SHOW COLUMNS FROM reglas LIKE 'media_tipo';")
    if not c.fetchone():
        c.execute("ALTER TABLE reglas ADD COLUMN media_tipo VARCHAR(20) NULL;")

    # regla_medias: soporta múltiples archivos por regla
    c.execute("""
    CREATE TABLE IF NOT EXISTS regla_medias (
      id INT AUTO_INCREMENT PRIMARY KEY,
      regla_id INT NOT NULL,
      media_url TEXT NOT NULL,
      media_tipo VARCHAR(20),
      FOREIGN KEY (regla_id) REFERENCES reglas(id) ON DELETE CASCADE
    ) ENGINE=InnoDB;
    """)

    # Migración defensiva: copiar datos desde reglas.media_* si existen
    c.execute("SELECT id, media_url, media_tipo FROM reglas WHERE media_url IS NOT NULL")
    for rid, url, tipo in c.fetchall() or []:
        c.execute(
            """
            INSERT INTO regla_medias (regla_id, media_url, media_tipo)
            SELECT %s, %s, %s FROM DUAL
            WHERE NOT EXISTS (
                SELECT 1 FROM regla_medias WHERE regla_id=%s AND media_url=%s
            )
            """,
            (rid, url, tipo, rid, url),
        )

    # botones
    c.execute("""
    CREATE TABLE IF NOT EXISTS botones (
      id INT AUTO_INCREMENT PRIMARY KEY,
      mensaje   TEXT NOT NULL,
      tipo      VARCHAR(50),
      media_url TEXT,
      nombre    VARCHAR(100),
      categoria VARCHAR(100)
    ) ENGINE=InnoDB;
    """)
    # Migración defensiva para columnas nuevas
    c.execute("SHOW COLUMNS FROM botones LIKE 'tipo';")
    if not c.fetchone():
        c.execute("ALTER TABLE botones ADD COLUMN tipo VARCHAR(50) NULL;")
    c.execute("SHOW COLUMNS FROM botones LIKE 'media_url';")
    if not c.fetchone():
        c.execute("ALTER TABLE botones ADD COLUMN media_url TEXT NULL;")
    c.execute("SHOW COLUMNS FROM botones LIKE 'nombre';")
    if not c.fetchone():
        c.execute("ALTER TABLE botones ADD COLUMN nombre VARCHAR(100) NULL;")
    c.execute("SHOW COLUMNS FROM botones LIKE 'opciones';")
    if not c.fetchone():
        c.execute("ALTER TABLE botones ADD COLUMN opciones TEXT NULL;")
    c.execute("SHOW COLUMNS FROM botones LIKE 'categoria';")
    if not c.fetchone():
        c.execute("ALTER TABLE botones ADD COLUMN categoria VARCHAR(100) NULL;")

    # boton_medias: soporta múltiples archivos por botón
    c.execute("""
    CREATE TABLE IF NOT EXISTS boton_medias (
      id INT AUTO_INCREMENT PRIMARY KEY,
      boton_id INT NOT NULL,
      media_url TEXT NOT NULL,
      media_tipo VARCHAR(20),
      FOREIGN KEY (boton_id) REFERENCES botones(id) ON DELETE CASCADE
    ) ENGINE=InnoDB;
    """)

    # boton_usuarios: asigna botones rápidos a usuarios
    c.execute("""
    CREATE TABLE IF NOT EXISTS boton_usuarios (
      boton_id INT NOT NULL,
      user_id INT NOT NULL,
      PRIMARY KEY (boton_id, user_id),
      FOREIGN KEY (boton_id) REFERENCES botones(id) ON DELETE CASCADE,
      FOREIGN KEY (user_id) REFERENCES usuarios(id) ON DELETE CASCADE
    ) ENGINE=InnoDB;
    """)

    # Migración defensiva: copiar datos desde botones.media_url si existen
    c.execute("SELECT id, media_url FROM botones WHERE media_url IS NOT NULL")
    for bid, url in c.fetchall() or []:
        c.execute(
            """
            INSERT INTO boton_medias (boton_id, media_url, media_tipo)
            SELECT %s, %s, NULL FROM DUAL
            WHERE NOT EXISTS (
                SELECT 1 FROM boton_medias WHERE boton_id=%s AND media_url=%s
            )
            """,
            (bid, url, bid, url),
        )

    # Migración: asignar botones existentes a todos los usuarios si no hay relaciones
    c.execute("SELECT COUNT(*) FROM boton_usuarios")
    row = c.fetchone()
    existing_relations = row[0] if row else 0
    if existing_relations == 0:
        c.execute("SELECT id FROM botones")
        boton_ids = [row[0] for row in c.fetchall() or []]
        c.execute("SELECT id FROM usuarios")
        user_ids = [row[0] for row in c.fetchall() or []]
        for boton_id in boton_ids:
            for user_id in user_ids:
                c.execute(
                    "INSERT IGNORE INTO boton_usuarios (boton_id, user_id) VALUES (%s, %s)",
                    (boton_id, user_id),
                )

    # alias
    c.execute("""
    CREATE TABLE IF NOT EXISTS alias (
      numero VARCHAR(20) PRIMARY KEY,
      nombre VARCHAR(100)
    ) ENGINE=InnoDB;
    """)

    # hidden_chats: números ocultos sólo para la vista web
    c.execute("""
    CREATE TABLE IF NOT EXISTS hidden_chats (
      numero VARCHAR(20) PRIMARY KEY
    ) ENGINE=InnoDB;
    """)

    # chat_roles: relaciona cada número de chat con uno o varios roles
    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_roles (
      numero  VARCHAR(20) NOT NULL,
      role_id INT NOT NULL,
      PRIMARY KEY (numero, role_id),
      FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE
    ) ENGINE=InnoDB;
    """)

    # chat_state: almacena el paso actual y última actividad por número
    c.execute("""
    CREATE TABLE IF NOT EXISTS chat_state (
      numero VARCHAR(20) PRIMARY KEY,
      step TEXT,
      estado VARCHAR(20),
      last_activity DATETIME
    ) ENGINE=InnoDB;
    """)

    # Migración defensiva de la columna estado
    c.execute("SHOW COLUMNS FROM chat_state LIKE 'estado';")
    if not c.fetchone():
        c.execute("ALTER TABLE chat_state ADD COLUMN estado VARCHAR(20);")

    _ensure_chat_state_definitions(c)
    _seed_chat_state_definitions(c)

    # ia_catalog_pages: páginas del catálogo indexado para IA
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS ia_catalog_pages (
          id INT AUTO_INCREMENT PRIMARY KEY,
          tenant_key VARCHAR(64) NULL,
          pdf_filename VARCHAR(255) NOT NULL,
          page_number INT NOT NULL,
          text_content LONGTEXT,
          keywords TEXT,
          image_filename VARCHAR(255),
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_pdf_page (pdf_filename, page_number),
          INDEX idx_tenant_pdf (tenant_key, pdf_filename, page_number)
        ) ENGINE=InnoDB;
        """
    )

    # Migración defensiva: agregar tenant_key si no existe
    c.execute("SHOW COLUMNS FROM ia_catalog_pages LIKE 'tenant_key';")
    if not c.fetchone():
        c.execute("ALTER TABLE ia_catalog_pages ADD COLUMN tenant_key VARCHAR(64) NULL AFTER id;")
        c.execute(
            "ALTER TABLE ia_catalog_pages ADD INDEX idx_tenant_pdf (tenant_key, pdf_filename, page_number);"
        )

    # Migración defensiva: agregar keywords si no existe
    c.execute("SHOW COLUMNS FROM ia_catalog_pages LIKE 'keywords';")
    if not c.fetchone():
        c.execute("ALTER TABLE ia_catalog_pages ADD COLUMN keywords TEXT NULL AFTER text_content;")

    conn.commit()
    conn.close()



def guardar_mensaje(
    numero,
    mensaje,
    tipo,
    wa_id=None,
    reply_to_wa_id=None,
    media_id=None,
    media_url=None,
    mime_type=None,
    opciones=None,
    link_url=None,
    link_title=None,
    link_body=None,
    link_thumb=None,
    step=None,
    regla_id=None,
    timestamp=None,
    dedupe_wa_id=False,
    db_settings: DatabaseSettings | None = None,
):
    """Guarda un mensaje en la tabla ``mensajes``.

    Admite campos opcionales para los identificadores de WhatsApp
    (``wa_id`` y ``reply_to_wa_id``), para medios (``media_id``, ``media_url``,
    ``mime_type``), para opciones JSON (``opciones``) y, sólo para mensajes de
    tipo ``referral``, datos de enlaces (``link_url``, ``link_title``,
    ``link_body``, ``link_thumb``). También puede registrar el ``step`` del
    flujo y el ``regla_id`` que originó el mensaje.
    """
    if tipo and str(tipo).startswith('cliente'):
        unhide_chat(numero)

    if tipo != 'referral':
        link_url = link_title = link_body = link_thumb = None

    conn = get_connection(ensure_database=True, db_settings=db_settings)
    c = conn.cursor()
    if dedupe_wa_id and wa_id:
        c.execute("SELECT 1 FROM mensajes WHERE wa_id = %s LIMIT 1", (wa_id,))
        if c.fetchone():
            conn.close()
            return None
    if timestamp is None:
        timestamp_placeholder = "NOW()"
        timestamp_value = ()
    else:
        timestamp_placeholder = "%s"
        timestamp_value = (timestamp,)
    c.execute(
        "INSERT INTO mensajes "
        "(numero, mensaje, tipo, wa_id, reply_to_wa_id, media_id, media_url, mime_type, opciones, "
        "link_url, link_title, link_body, link_thumb, step, regla_id, timestamp) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,{timestamp_placeholder})",
        (
            numero,
            mensaje,
            tipo,
            wa_id,
            reply_to_wa_id,
            media_id,
            media_url,
            mime_type,
            opciones,
            link_url,
            link_title,
            link_body,
            link_thumb,
            step,
            regla_id,
        )
        + timestamp_value,
    )
    mensaje_id = c.lastrowid
    conn.commit()
    conn.close()
    try:
        from services.realtime import emit_chat_list_update, emit_chat_update
    except ImportError:
        return mensaje_id
    emit_chat_update(numero)
    emit_chat_list_update()
    return mensaje_id


def guardar_page_message(
    *,
    tenant_key,
    platform,
    page_id,
    conversation_id,
    message_id,
    created_time,
    from_id,
    to_ids_json,
    message,
    reply_to_mid=None,
    is_self_reply=None,
    db_settings: DatabaseSettings | None = None,
):
    if not tenant_key or not message_id:
        return None

    conn = get_connection(ensure_database=True, db_settings=db_settings)
    c = conn.cursor()
    c.execute(
        "INSERT IGNORE INTO page_messages "
        "(tenant_key, platform, page_id, conversation_id, message_id, created_time, "
        "from_id, to_ids_json, message, reply_to_mid, is_self_reply, inserted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (
            tenant_key,
            platform,
            page_id,
            conversation_id,
            message_id,
            created_time,
            from_id,
            to_ids_json,
            message,
            reply_to_mid,
            1 if is_self_reply else 0 if is_self_reply is not None else None,
        ),
    )
    conn.commit()
    conn.close()
    return message_id


def guardar_conversation(
    *,
    tenant_key,
    platform,
    conversation_id,
    self_id,
    contact_id,
    updated_time,
    db_settings: DatabaseSettings | None = None,
):
    if not tenant_key or not conversation_id:
        return None

    conn = get_connection(ensure_database=True, db_settings=db_settings)
    c = conn.cursor()
    c.execute(
        "INSERT INTO page_conversations "
        "(tenant_key, platform, conversation_id, self_id, contact_id, updated_time, "
        "inserted_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW()) "
        "ON DUPLICATE KEY UPDATE self_id=VALUES(self_id), "
        "contact_id=VALUES(contact_id), updated_time=VALUES(updated_time), "
        "updated_at=NOW()",
        (
            tenant_key,
            platform,
            conversation_id,
            self_id,
            contact_id,
            updated_time,
        ),
    )
    conn.commit()
    conn.close()
    return conversation_id


def guardar_estado_mensaje(
    wa_id,
    status,
    status_timestamp=None,
    recipient_id=None,
    error=None,
    payload=None,
):
    if not wa_id or not status:
        return None

    error = error or {}
    error_details = error.get("details")
    if isinstance(error_details, (dict, list)):
        error_details = json.dumps(error_details, ensure_ascii=False)

    payload_json = None
    if payload is not None:
        payload_json = json.dumps(payload, ensure_ascii=False)

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT IGNORE INTO mensajes_status "
        "(wa_id, status, status_timestamp, recipient_id, error_code, error_title, "
        "error_message, error_details, payload_json, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
        (
            wa_id,
            status,
            status_timestamp,
            recipient_id,
            error.get("code"),
            error.get("title"),
            error.get("message"),
            error_details,
            payload_json,
        ),
    )
    mensaje_status_id = c.lastrowid
    conn.commit()
    conn.close()
    return mensaje_status_id


def guardar_flow_response(numero, flow_name, response_json, wa_id=None):
    """Guarda la respuesta de un Flow (nfm_reply) en la tabla ``flow_responses``."""
    conn = get_connection()
    try:
        c = conn.cursor()
        try:
            c.execute(
                """
                INSERT INTO flow_responses (numero, flow_name, response_json, wa_id, timestamp)
                VALUES (%s, %s, %s, %s, NOW())
                """,
                (numero, flow_name, response_json, wa_id),
            )
        except mysql.connector.errors.ProgrammingError as exc:
            if exc.errno != errorcode.ER_NO_SUCH_TABLE:
                raise
            c.execute(FLOW_RESPONSES_TABLE_DDL)
            c.execute(
                """
                INSERT INTO flow_responses (numero, flow_name, response_json, wa_id, timestamp)
                VALUES (%s, %s, %s, %s, NOW())
                """,
                (numero, flow_name, response_json, wa_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_mensaje_texto(id_mensaje, texto):
    """Actualiza el campo `mensaje` de un registro existente."""
    conn = get_connection()
    c    = conn.cursor()
    c.execute(
        "UPDATE mensajes SET mensaje=%s WHERE id=%s",
        (texto, id_mensaje),
    )
    conn.commit()
    conn.close()


def get_chat_state(numero):
    """Obtiene el step, ``last_activity`` y estado almacenados para un número.

    El orden de las columnas se mantiene por compatibilidad (``step`` en la
    posición 0 y ``last_activity`` en la 1). La columna ``estado`` se expone en
    la posición 2 cuando está disponible.
    """

    conn = get_connection()
    c    = conn.cursor()
    c.execute(
        "SELECT step, last_activity, estado FROM chat_state WHERE numero=%s",
        (numero,),
    )
    row = c.fetchone()
    conn.close()
    return row


def update_chat_state(numero, step, estado=None):
    """Inserta o actualiza el estado del chat y la última actividad."""
    conn = get_connection()
    c    = conn.cursor()
    c.execute(
        """
        INSERT INTO chat_state (numero, step, estado, last_activity)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            step = VALUES(step),
            estado = COALESCE(VALUES(estado), estado),
            last_activity = VALUES(last_activity)
        """,
        (numero, step, estado, datetime.utcnow()),
    )
    conn.commit()
    conn.close()


def get_chat_state_definitions(include_hidden: bool = False):
    conn = get_connection()
    c = conn.cursor()
    try:
        _ensure_chat_state_definitions(c)
        _seed_chat_state_definitions(c)
        conn.commit()
        if include_hidden:
            c.execute(
                """
                SELECT state_key, label, color_hex, text_color_hex, priority, visible
                  FROM chat_state_definitions
                 ORDER BY priority DESC, label
                """
            )
        else:
            c.execute(
                """
                SELECT state_key, label, color_hex, text_color_hex, priority, visible
                  FROM chat_state_definitions
                 WHERE visible = 1
                 ORDER BY priority DESC, label
                """
            )
        rows = c.fetchall()
    finally:
        conn.close()
    definitions = []
    for row in rows:
        definitions.append(
            {
                "key": row[0],
                "label": row[1],
                "color": row[2],
                "text_color": row[3],
                "priority": row[4],
                "visible": bool(row[5]),
            }
        )
    return definitions


def hide_chat(numero):
    """Marca un chat como oculto en la interfaz web sin borrar sus datos."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO hidden_chats (numero)
            VALUES (%s)
            ON DUPLICATE KEY UPDATE numero = VALUES(numero)
            """,
            (numero,),
        )
        conn.commit()
    finally:
        conn.close()


def unhide_chat(numero):
    """Quita la marca de oculto para un chat."""
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("DELETE FROM hidden_chats WHERE numero=%s", (numero,))
        conn.commit()
    finally:
        conn.close()


def delete_chat(numero):
    """Elimina toda la información persistida asociada a un número de chat."""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        tables = (
            ("mensajes", "numero"),
            ("alias", "numero"),
            ("chat_roles", "numero"),
            ("chat_state", "numero"),
        )
        for table, column in tables:
            try:
                cursor.execute(f"DELETE FROM {table} WHERE {column} = %s", (numero,))
            except ProgrammingError as exc:
                if exc.errno != errorcode.ER_NO_SUCH_TABLE:
                    raise

        try:
            cursor.execute("DELETE FROM flow_responses WHERE numero = %s", (numero,))
        except ProgrammingError as exc:
            if exc.errno != errorcode.ER_NO_SUCH_TABLE:
                raise

        conn.commit()
    finally:
        conn.close()


def delete_chat_state(numero):
    """Elimina el registro de estado para un número."""
    conn = get_connection()
    c    = conn.cursor()
    c.execute("DELETE FROM chat_state WHERE numero=%s", (numero,))
    conn.commit()
    conn.close()

def obtener_mensajes_por_numero(numero):
    conn = get_connection()
    c    = conn.cursor()
    c.execute("""
      SELECT mensaje, tipo, timestamp
      FROM mensajes
      WHERE numero = %s
      ORDER BY timestamp ASC
    """, (numero,))
    rows = c.fetchall()
    conn.close()
    return rows  # lista de tuplas (mensaje, tipo, timestamp)


def obtener_historial_chat(
    numero,
    limit: int = 30,
    step: str | None = None,
    anchor_step: str | None = None,
):
    """Obtiene los mensajes más recientes de un chat para alimentar la IA."""

    conn = get_connection()
    c = conn.cursor()
    query = "SELECT mensaje, tipo FROM mensajes WHERE numero = %s"
    params: list = [numero]
    if anchor_step:
        c.execute(
            "SELECT MAX(timestamp) FROM mensajes WHERE numero = %s AND step = %s",
            (numero, anchor_step),
        )
        anchor_row = c.fetchone()
        anchor_ts = anchor_row[0] if anchor_row else None
        if anchor_ts:
            query += " AND timestamp >= %s"
            params.append(anchor_ts)
    if step:
        query += " AND step = %s"
        params.append(step)
    query += " ORDER BY timestamp DESC LIMIT %s"
    params.append(limit)
    c.execute(query, tuple(params))
    rows = c.fetchall()
    conn.close()
    rows.reverse()
    return [
        {"mensaje": mensaje, "tipo": tipo}
        for mensaje, tipo in rows
        if mensaje is not None
    ]


def obtener_ultimo_mensaje_cliente(numero):
    """Devuelve el último mensaje textual enviado por el cliente."""

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT mensaje
          FROM mensajes
         WHERE numero = %s
           AND (tipo = 'cliente' OR tipo LIKE 'cliente_%')
           AND mensaje IS NOT NULL
           AND mensaje <> ''
         ORDER BY timestamp DESC
         LIMIT 1
        """,
        (numero,),
    )
    row = c.fetchone()
    conn.close()
    return (row[0] or "").strip() if row else ""


def obtener_ultimo_mensaje_cliente_info(numero):
    """Retorna el timestamp y tipo del último mensaje del cliente."""

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT tipo, timestamp
          FROM mensajes
         WHERE numero = %s
           AND (tipo = 'cliente' OR tipo LIKE 'cliente_%')
         ORDER BY timestamp DESC
         LIMIT 1
        """,
        (numero,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {"tipo": row[0], "timestamp": row[1]}


def obtener_ultimo_mensaje_cliente_media_info(numero):
    """Retorna el último mensaje del cliente que tenga media asociada."""

    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, tipo, media_id, media_url, mime_type, timestamp
          FROM mensajes
         WHERE numero = %s
           AND (tipo = 'cliente' OR tipo LIKE 'cliente_%')
           AND (media_id IS NOT NULL OR media_url IS NOT NULL)
         ORDER BY timestamp DESC
         LIMIT 1
        """,
        (numero,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0],
        "tipo": row[1],
        "media_id": row[2],
        "media_url": row[3],
        "mime_type": row[4],
        "timestamp": row[5],
    }


def replace_catalog_pages(
    pdf_filename: str, pages, *, media_root: str | None = None, batch_size: int = 100
):
    """Reemplaza todas las páginas indexadas del catálogo con el PDF indicado.

    Admite iterables o generadores de páginas para evitar cargar catálogos
    completos en memoria. Inserta por lotes para reducir la presión sobre la
    base de datos en catálogos muy extensos.
    """

    tenant_key = get_current_tenant_key()

    conn = get_connection()
    c = conn.cursor()
    try:
        if tenant_key:
            c.execute(
                "DELETE FROM ia_catalog_pages WHERE tenant_key=%s", (tenant_key,)
            )
        else:
            c.execute("DELETE FROM ia_catalog_pages WHERE tenant_key IS NULL")
        conn.commit()

        if media_root:
            pages_dir = os.path.join(media_root, "ia_pages")
            if os.path.isdir(pages_dir):
                for entry in os.listdir(pages_dir):
                    try:
                        os.remove(os.path.join(pages_dir, entry))
                    except OSError:
                        continue

        buffer = []
        for page in pages:
            buffer.append(
                (
                    tenant_key,
                    pdf_filename,
                    page.page_number
                    if hasattr(page, "page_number")
                    else page.get("page_number"),
                    page.text_content
                    if hasattr(page, "text_content")
                    else page.get("text_content"),
                    ",".join(page.keywords)
                    if hasattr(page, "keywords") and page.keywords is not None
                    else page.get("keywords"),
                    page.image_filename
                    if hasattr(page, "image_filename")
                    else page.get("image_filename"),
                )
            )
            if len(buffer) >= batch_size:
                c.executemany(
                    """
                    INSERT INTO ia_catalog_pages
                        (tenant_key, pdf_filename, page_number, text_content, keywords, image_filename)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    buffer,
                )
                conn.commit()
                buffer.clear()

        if buffer:
            c.executemany(
                """
                INSERT INTO ia_catalog_pages
                    (tenant_key, pdf_filename, page_number, text_content, keywords, image_filename)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                buffer,
            )
            conn.commit()
    finally:
        conn.close()


def search_catalog_pages(
    query: str,
    limit: int = 3,
    *,
    tenant_key: str | None = None,
    fallback_to_default: bool = False,
):
    """Busca páginas del catálogo que coincidan con el texto proporcionado.

    Solo consulta el catálogo del tenant indicado (o el activo en contexto).
    El ``DEFAULT_TENANT`` ya no se usa como respaldo automático para evitar
    mezclar datos entre empresas.
    """

    tokens = [t for t in re.split(r"\W+", (query or "").lower()) if len(t) > 2]

    active_tenant = tenant_key if tenant_key is not None else get_current_tenant_key()
    default_tenant = (Config.DEFAULT_TENANT or "").strip() or None
    tenant_candidates: list[str | None] = [active_tenant]
    if fallback_to_default and default_tenant and default_tenant not in tenant_candidates:
        tenant_candidates.append(default_tenant)

    rows_cache: list[tuple[str | None, list[tuple]]] = []

    def _select_evenly(items: list[dict], limit: int) -> list[dict]:
        if limit <= 0 or len(items) <= limit:
            return items
        last_index = len(items) - 1
        if limit == 1:
            return [items[0]]
        step = last_index / (limit - 1)
        selected: list[dict] = []
        used: set[int] = set()
        for idx in range(limit):
            pick = int(round(idx * step))
            pick = min(max(pick, 0), last_index)
            if pick in used:
                continue
            selected.append(items[pick])
            used.add(pick)
        if len(selected) < limit:
            for pick in range(last_index, -1, -1):
                if pick in used:
                    continue
                selected.append(items[pick])
                used.add(pick)
                if len(selected) >= limit:
                    break
        return selected

    def _fetch_rows(target_tenant: str | None):
        conn = get_connection()
        try:
            c = conn.cursor()
            if target_tenant:
                c.execute(
                    """
                    SELECT pdf_filename, page_number, text_content, keywords, image_filename
                      FROM ia_catalog_pages
                     WHERE tenant_key=%s
                     ORDER BY page_number ASC
                    """,
                    (target_tenant,),
                )
            else:
                c.execute(
                    """
                    SELECT pdf_filename, page_number, text_content, keywords, image_filename
                      FROM ia_catalog_pages
                     WHERE tenant_key IS NULL
                     ORDER BY page_number ASC
                    """,
                )
            return c.fetchall()
        finally:
            conn.close()

    def _fallback_rows():
        for candidate, rows in rows_cache:
            if not rows:
                continue
            logger.info(
                "Catálogo IA: sin coincidencias exactas, usando páginas sugeridas",
                extra={"tenant": candidate, "tokens": tokens, "rows": len(rows)},
            )
            items = [
                {
                    "pdf_filename": pdf_filename,
                    "page_number": page_number,
                    "text_content": text_content,
                    "keywords": keywords,
                    "image_filename": image_filename,
                    "score": 0,
                    "tenant_key": candidate,
                }
                for pdf_filename, page_number, text_content, keywords, image_filename in rows
            ]
            return _select_evenly(items, limit)
        return []

    if not tokens:
        logger.debug("Búsqueda de catálogo sin tokens, usando sugerencias", extra={"query": query})
        for candidate in tenant_candidates:
            rows_cache.append((candidate, _fetch_rows(candidate)))
        fallback = _fallback_rows()
        if fallback:
            return fallback
        logger.warning(
            "No hay páginas de catálogo disponibles para sugerir",
            extra={"tenant": active_tenant, "tokens": tokens},
        )
        return []

    for idx, candidate in enumerate(tenant_candidates):
        rows = _fetch_rows(candidate)
        rows_cache.append((candidate, rows))
        scored = []
        for pdf_filename, page_number, text_content, keywords, image_filename in rows:
            text_lower = (text_content or "").lower()
            kw_tokens = (keywords or "").lower().split(",") if keywords else []
            score = sum(1 for token in tokens if token in text_lower)
            if not score and kw_tokens:
                score = sum(1 for token in tokens if token in kw_tokens)
            if score:
                scored.append(
                    {
                        "pdf_filename": pdf_filename,
                        "page_number": page_number,
                        "text_content": text_content,
                        "keywords": keywords,
                        "image_filename": image_filename,
                        "score": score,
                        "tenant_key": candidate,
                    }
                )

        if scored:
            if idx > 0:
                logger.info(
                    "Catálogo IA: se usó fallback al tenant por defecto",
                    extra={
                        "original_tenant": active_tenant,
                        "fallback_tenant": candidate,
                        "tokens": tokens,
                    },
                )
            scored.sort(key=lambda item: item["page_number"])
            return _select_evenly(scored, limit)

        logger.debug(
            "Catálogo IA sin coincidencias",
            extra={"tenant": candidate, "tokens": tokens, "rows": len(rows)},
        )

    fallback_rows = _fallback_rows()
    if fallback_rows:
        return fallback_rows

    logger.warning(
        "No se encontraron coincidencias en ningún catálogo disponible",
        extra={"tenant": active_tenant, "tokens": tokens},
    )
    return []


def get_conversation(numero):
    """Obtiene la conversación de un número uniendo ``mensajes`` con ``reglas``.

    Realiza un ``JOIN`` entre ``mensajes`` y ``reglas`` usando ``regla_id`` y
    ordenando por ``reglas.id``. El resultado se devuelve en una sola fila con
    columnas dinámicas del tipo ``regla_step``, ``mensaje_usuario``,
    ``regla_step2``, ``mensaje_usuario_step2``, etc.
    """
    conn = get_connection()
    c    = conn.cursor()
    c.execute(
        """
        SELECT m.numero, r.step, m.mensaje
          FROM mensajes m
          JOIN reglas r ON m.regla_id = r.id
         WHERE m.numero = %s
         ORDER BY r.id
        """,
        (numero,),
    )
    rows = c.fetchall()
    conn.close()

    result = {"numero": numero}
    for idx, (_numero, step, mensaje) in enumerate(rows, start=1):
        if idx == 1:
            result["regla_step"] = step
            result["mensaje_usuario"] = mensaje
        else:
            result[f"regla_step{idx}"] = step
            result[f"mensaje_usuario_step{idx}"] = mensaje
    return result


def obtener_lista_chats():
    conn = get_connection()
    c    = conn.cursor(dictionary=True)
    # obtenemos cada número único, su último timestamp y alias si existe
    c.execute("""
      SELECT m.numero,
             (SELECT nombre FROM alias a WHERE a.numero=m.numero) AS alias,
             EXISTS(
               SELECT 1 FROM reglas r WHERE r.step='asesor' AND r.input_text=m.numero
             ) AS asesor
      FROM mensajes m
      GROUP BY m.numero
      ORDER BY MAX(m.timestamp) DESC;
    """)
    rows = c.fetchall()
    conn.close()
    return rows  # lista de dicts {numero, alias, asesor}


def obtener_botones():
    conn = get_connection()
    c    = conn.cursor(dictionary=True)
    c.execute("SELECT mensaje FROM botones ORDER BY id ASC;")
    rows = c.fetchall()
    conn.close()
    return [r['mensaje'] for r in rows]


def set_alias(numero, nombre):
    conn = get_connection()
    c    = conn.cursor()
    c.execute("""
      INSERT INTO alias (numero, nombre)
      VALUES (%s, %s)
      ON DUPLICATE KEY UPDATE nombre = VALUES(nombre);
    """, (numero, nombre))
    conn.commit()
    conn.close()


def get_roles_by_user(
    user_id,
    db_settings: DatabaseSettings | None = None,
    *,
    allow_tenant_context: bool = True,
):
    """Retorna una lista de keywords de roles asignados a un usuario."""
    conn = get_connection(
        db_settings=db_settings, allow_tenant_context=allow_tenant_context
    )
    c    = conn.cursor()
    c.execute("""
      SELECT r.keyword
        FROM roles r
        JOIN user_roles ur ON r.id = ur.role_id
       WHERE ur.user_id = %s
    """, (user_id,))
    roles = [row[0] for row in c.fetchall()]
    conn.close()
    return roles


def assign_role_to_user(
    user_id,
    role_keyword,
    role_name=None,
    *,
    db_settings: DatabaseSettings | None = None,
):
    """Asigna un rol (por keyword) a un usuario. Si el rol no existe se crea."""
    conn = get_connection(db_settings=db_settings)
    c    = conn.cursor()
    # Obtener rol existente o crearlo
    c.execute("SELECT id FROM roles WHERE keyword=%s", (role_keyword,))
    row = c.fetchone()
    if row:
        role_id = row[0]
    else:
        name = role_name or role_keyword.capitalize()
        c.execute("INSERT INTO roles (name, keyword) VALUES (%s, %s)", (name, role_keyword))
        role_id = c.lastrowid
    # Asignar rol al usuario
    c.execute("INSERT IGNORE INTO user_roles (user_id, role_id) VALUES (%s, %s)", (user_id, role_id))
    conn.commit()
    conn.close()
