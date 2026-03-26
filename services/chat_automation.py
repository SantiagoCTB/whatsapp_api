"""Preferencias de automatización por chat (IA y seguimiento)."""

from __future__ import annotations

from services.db import get_connection


def _ensure_chat_automation_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_automation_settings (
          numero VARCHAR(64) PRIMARY KEY,
          ai_enabled TINYINT(1) NOT NULL DEFAULT 1,
          followup_enabled TINYINT(1) NOT NULL DEFAULT 1,
          locked_by_advisor TINYINT(1) NOT NULL DEFAULT 0,
          updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
        """
    )


def _chat_has_advisor_message(cursor, numero: str) -> bool:
    cursor.execute(
        """
        SELECT 1
          FROM mensajes
         WHERE numero = %s
           AND tipo LIKE 'asesor%%'
         LIMIT 1
        """,
        (numero,),
    )
    return cursor.fetchone() is not None


def get_chat_automation_status(numero: str) -> dict[str, bool]:
    conn = get_connection()
    c = conn.cursor()
    try:
        _ensure_chat_automation_table(c)
        c.execute(
            """
            SELECT ai_enabled, followup_enabled, locked_by_advisor
              FROM chat_automation_settings
             WHERE numero = %s
             LIMIT 1
            """,
            (numero,),
        )
        row = c.fetchone()
        advisor_lock = _chat_has_advisor_message(c, numero)
    finally:
        conn.close()

    ai_enabled = bool(row[0]) if row else True
    followup_enabled = bool(row[1]) if row else True
    locked_by_advisor = bool(row[2]) if row else False
    if advisor_lock:
        locked_by_advisor = True
        ai_enabled = False
        followup_enabled = False

    return {
        "ai_enabled": ai_enabled,
        "followup_enabled": followup_enabled,
        "locked_by_advisor": locked_by_advisor,
    }


def set_chat_automation_settings(
    numero: str,
    *,
    ai_enabled: bool | None = None,
    followup_enabled: bool | None = None,
    locked_by_advisor: bool | None = None,
) -> dict[str, bool]:
    current = get_chat_automation_status(numero)
    next_ai = current["ai_enabled"] if ai_enabled is None else bool(ai_enabled)
    next_followup = (
        current["followup_enabled"] if followup_enabled is None else bool(followup_enabled)
    )
    next_locked = (
        current["locked_by_advisor"] if locked_by_advisor is None else bool(locked_by_advisor)
    )

    if current["locked_by_advisor"]:
        next_locked = True
        next_ai = False
        next_followup = False

    conn = get_connection()
    c = conn.cursor()
    try:
        _ensure_chat_automation_table(c)
        c.execute(
            """
            INSERT INTO chat_automation_settings (numero, ai_enabled, followup_enabled, locked_by_advisor)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                ai_enabled = VALUES(ai_enabled),
                followup_enabled = VALUES(followup_enabled),
                locked_by_advisor = VALUES(locked_by_advisor)
            """,
            (numero, int(next_ai), int(next_followup), int(next_locked)),
        )
        conn.commit()
    finally:
        conn.close()

    return get_chat_automation_status(numero)


def lock_chat_automation_due_to_advisor(numero: str) -> dict[str, bool]:
    return set_chat_automation_settings(
        numero,
        ai_enabled=False,
        followup_enabled=False,
        locked_by_advisor=True,
    )
