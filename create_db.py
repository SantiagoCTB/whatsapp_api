# Proyecto: App tipo WhatsApp Web con Flask + SQLite + Frontend tipo chat
# Estructura completa paso a paso.

####################
# 1. BASE DE DATOS #
####################

# Archivo: create_db.py
import sqlite3

conn = sqlite3.connect('chat_support.db')
c = conn.cursor()

# Tabla de chats
c.execute('''CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id TEXT,
    status TEXT DEFAULT 'pendiente'
)''')

# Tabla de mensajes
c.execute('''CREATE TABLE IF NOT EXISTS mensajes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    remitente TEXT,
    mensaje TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (chat_id) REFERENCES chats(id)
)''')

conn.commit()
conn.close()

print("Base de datos creada exitosamente.")