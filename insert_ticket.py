import sqlite3
from datetime import datetime

# Conectar a la base de datos
conn = sqlite3.connect('database.db')
c = conn.cursor()

# Insertar un registro de prueba
numero = '573001112233'
mensaje = 'Necesito ayuda'
timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

c.execute("INSERT INTO tickets (numero, mensaje, timestamp) VALUES (?, ?, ?)",
          (numero, mensaje, timestamp))

conn.commit()
conn.close()

print("✅ Registro insertado correctamente.")
