import mysql.connector
from werkzeug.security import generate_password_hash
from config import Config

def get_connection():
    return mysql.connector.connect(
        host     = Config.DB_HOST,
        port     = Config.DB_PORT,
        user     = Config.DB_USER,
        password = Config.DB_PASSWORD,
        database = Config.DB_NAME
    )

def init_db():
    conn = get_connection()
    c    = conn.cursor()

    # Elimino la tabla si existe
    #c.execute("DROP TABLE IF EXISTS mensajes;")

    # mensajes
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes (
      id INT AUTO_INCREMENT PRIMARY KEY,
      numero     VARCHAR(20),
      mensaje    TEXT,
      tipo       VARCHAR(50),
      media_id   VARCHAR(255),
      media_url  TEXT,
      mime_type  TEXT,
      timestamp  DATETIME
    );
    """)

    # mensajes procesados
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes_procesados (
      mensaje_id VARCHAR(255) PRIMARY KEY
    );
    """)

    # usuarios
    c.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
      id INT AUTO_INCREMENT PRIMARY KEY,
      username VARCHAR(50) UNIQUE NOT NULL,
      password VARCHAR(128) NOT NULL,
      rol VARCHAR(20) NOT NULL
    );
    """)

    # reglas
    c.execute("""
    CREATE TABLE IF NOT EXISTS reglas (
      id INT AUTO_INCREMENT PRIMARY KEY,
      step TEXT NOT NULL,
      input_text TEXT NOT NULL,
      respuesta TEXT NOT NULL,
      siguiente_step TEXT,
      tipo VARCHAR(20) NOT NULL DEFAULT 'texto',
      opciones TEXT
    );
    """)

    # botones
    c.execute("""
    CREATE TABLE IF NOT EXISTS botones (
      id INT AUTO_INCREMENT PRIMARY KEY,
      mensaje TEXT NOT NULL
    );
    """)

    # alias
    c.execute("""
    CREATE TABLE IF NOT EXISTS alias (
      numero VARCHAR(20) PRIMARY KEY,
      nombre VARCHAR(100)
    );
    """)

    # usuario admin inicial
    hashed = generate_password_hash('admin123')
    c.execute("""
    INSERT INTO usuarios (username, password, rol)
      SELECT %s, %s, 'admin'
     FROM DUAL
     WHERE NOT EXISTS (
       SELECT 1 FROM usuarios WHERE username=%s
     )
     LIMIT 1;
    """, ('admin', hashed, 'admin'))

    conn.commit()
    conn.close()


def guardar_mensaje(numero, mensaje, tipo, media_id=None, media_url=None, mime_type=None):
    """
    Guarda un mensaje en la tabla 'mensajes'.
    Ahora admite un campo opcional mime_type para audio/video.
    """
    conn = get_connection()
    c    = conn.cursor()
    c.execute(
        "INSERT INTO mensajes "
        "(numero, mensaje, tipo, media_id, media_url, mime_type, timestamp) "
        "VALUES (%s, %s, %s, %s, %s, %s, NOW())",
        (numero, mensaje, tipo, media_id, media_url, mime_type)
    )
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
