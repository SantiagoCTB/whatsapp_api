#!/usr/bin/env python3
"""One-time script to upgrade stored user passwords to Werkzeug hashes."""
import hashlib
from getpass import getpass

from werkzeug.security import generate_password_hash

from services.db import get_connection


def main():
    conn = get_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, password FROM usuarios")

    for user in cursor.fetchall():
        if user["password"].startswith("pbkdf2:"):
            print(f"Skipping {user['username']}: already migrated")
            continue

        print(f"Rehashing password for {user['username']}")
        plain = getpass("Enter current password: ")
        if hashlib.sha256(plain.encode()).hexdigest() != user["password"]:
            print("Password mismatch; skipping")
            continue

        new_hash = generate_password_hash(plain)
        cursor.execute(
            "UPDATE usuarios SET password = %s WHERE id = %s",
            (new_hash, user["id"]),
        )
        conn.commit()

    cursor.close()
    conn.close()
    print("Migration complete")


if __name__ == "__main__":
    main()
