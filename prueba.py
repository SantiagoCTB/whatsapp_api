import hashlib
import sqlite3

DB_PATH = "database.db"
username = "admin"
password = "admin123"
hashed = hashlib.sha256(password.encode()).hexdigest()

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("SELECT * FROM usuarios WHERE username = ? AND password = ?", (username, hashed))
user = c.fetchone()
conn.close()

print("✅ Login correcto" if user else "❌ Login fallido")