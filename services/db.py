import sqlite3
from datetime import datetime
import hashlib
from config import Config

DB_PATH = Config.DB_PATH

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Tabla mensajes
    c.execute('''
        CREATE TABLE IF NOT EXISTS mensajes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT,
            mensaje TEXT,
            tipo TEXT,
            timestamp TEXT
        )
    ''')
    
    # Tabla mensajes procesados
    c.execute('''
        CREATE TABLE IF NOT EXISTS mensajes_procesados (
            mensaje_id TEXT PRIMARY KEY
        )
    ''')

    # Tabla de usuarios
    c.execute('''
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            rol TEXT NOT NULL
        )
    ''')

    # Tabla de reglas de automatización
    c.execute('''
        CREATE TABLE IF NOT EXISTS reglas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step TEXT NOT NULL,
            input_text TEXT NOT NULL,
            respuesta TEXT NOT NULL,
            siguiente_step TEXT,
            tipo TEXT DEFAULT 'texto'
        )
    ''')

    # Tabla de botones
    c.execute('''
        CREATE TABLE IF NOT EXISTS botones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mensaje TEXT NOT NULL
        )
    ''')

    # Crear usuario admin si no existe
    c.execute("SELECT * FROM usuarios WHERE username = 'admin'")
    if not c.fetchone():
        import hashlib
        password = 'admin123'
        hashed = hashlib.sha256(password.encode()).hexdigest()
        c.execute("INSERT INTO usuarios (username, password, rol) VALUES (?, ?, ?)",
                  ('admin', hashed, 'admin'))

    conn.commit()
    conn.close()


def guardar_mensaje(numero, mensaje, tipo):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO mensajes (numero, mensaje, tipo, timestamp) VALUES (?, ?, ?, ?)",
              (numero, mensaje, tipo, str(datetime.now())))
    conn.commit()
    conn.close()