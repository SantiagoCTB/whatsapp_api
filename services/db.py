import mysql.connector
from datetime import datetime
import hashlib
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

    # Tabla mensajes
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes (
        id INT AUTO_INCREMENT PRIMARY KEY,
        numero VARCHAR(20),
        mensaje TEXT,
        tipo VARCHAR(50),
        timestamp DATETIME
    );
    """)

    # Tabla mensajes_procesados
    c.execute("""
    CREATE TABLE IF NOT EXISTS mensajes_procesados (
        mensaje_id VARCHAR(255) PRIMARY KEY
    );
    """)

    # Tabla usuarios
    c.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) UNIQUE NOT NULL,
        password VARCHAR(128) NOT NULL,
        rol VARCHAR(20) NOT NULL
    );
    """)

    # Tabla reglas de automatización
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

    # Tabla botones
    c.execute("""
    CREATE TABLE IF NOT EXISTS botones (
        id INT AUTO_INCREMENT PRIMARY KEY,
        mensaje TEXT NOT NULL
    );
    """)

    # Tabla alias personalizados
    c.execute("""
    CREATE TABLE IF NOT EXISTS alias (
        numero VARCHAR(20) PRIMARY KEY,
        nombre VARCHAR(100)
    );
    """)

    # Crear usuario admin si no existe
    hashed = hashlib.sha256('admin123'.encode()).hexdigest()
    c.execute("""
    INSERT INTO usuarios (username, password, rol)
    SELECT tmp.username, tmp.password, tmp.rol
      FROM (SELECT %s AS username, %s AS password, 'admin' AS rol) AS tmp
     WHERE NOT EXISTS (
        SELECT 1 FROM usuarios WHERE username = %s
     )
    LIMIT 1;
    """, ('admin', hashed, 'admin'))

    conn.commit()
    conn.close()


def guardar_mensaje(numero, mensaje, tipo):
    conn = get_connection()
    c    = conn.cursor()
    c.execute("""
        INSERT INTO mensajes (numero, mensaje, tipo, timestamp)
        VALUES (%s, %s, %s, %s)
    """, (numero, mensaje, tipo, datetime.now()))
    conn.commit()
    conn.close()
