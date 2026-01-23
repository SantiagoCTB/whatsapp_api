from __future__ import annotations

from services.db import get_connection


def update_user_presence(username: str, *, is_active: bool) -> None:
    if not username:
        return
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM usuarios WHERE username = %s", (username,))
        row = c.fetchone()
        if not row:
            return
        user_id = row[0]
        c.execute(
            """
            INSERT INTO user_presence (user_id, is_active, last_seen)
            VALUES (%s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              is_active = VALUES(is_active),
              last_seen = VALUES(last_seen)
            """,
            (user_id, 1 if is_active else 0),
        )
        conn.commit()
    finally:
        conn.close()
