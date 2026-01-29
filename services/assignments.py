from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from services.db import get_connection


def _pick_next_user_id(active_ids: List[int], last_user_id: Optional[int]) -> int:
    if not active_ids:
        raise ValueError("No hay usuarios activos para asignar.")
    active_ids_sorted = sorted(active_ids)
    if last_user_id not in active_ids_sorted:
        return active_ids_sorted[0]
    idx = active_ids_sorted.index(last_user_id)
    return active_ids_sorted[(idx + 1) % len(active_ids_sorted)]


def _pick_equitable_user_id(
    active_ids: List[int],
    last_user_id: Optional[int],
    assignment_counts: Dict[int, int],
) -> int:
    if not active_ids:
        raise ValueError("No hay usuarios activos para asignar.")

    active_ids_sorted = sorted(active_ids)
    min_count = min(assignment_counts.get(user_id, 0) for user_id in active_ids_sorted)
    candidates = [user_id for user_id in active_ids_sorted if assignment_counts.get(user_id, 0) == min_count]

    if len(candidates) == 1:
        return candidates[0]

    if last_user_id in candidates:
        last_index = candidates.index(last_user_id)
        return candidates[(last_index + 1) % len(candidates)]

    return candidates[0]


def assign_chat_to_active_user(numero: str, role_keyword: str) -> Optional[Dict[str, str]]:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM roles WHERE keyword=%s", (role_keyword,))
        role_row = c.fetchone()
        if not role_row:
            return None
        role_id = role_row[0]

        c.execute(
            """
            SELECT u.id, u.username
              FROM usuarios u
              JOIN user_roles ur ON u.id = ur.user_id
             WHERE ur.role_id = %s
             ORDER BY u.id
            """,
            (role_id,),
        )
        role_users = c.fetchall()
        if not role_users:
            return None

        role_user_ids = [row[0] for row in role_users]
        role_usernames = {row[0]: row[1] for row in role_users}

        c.execute(
            "SELECT user_id FROM chat_assignments WHERE numero = %s",
            (numero,),
        )
        existing = c.fetchone()
        if existing and existing[0] in role_user_ids:
            user_id = existing[0]
            return {"user_id": str(user_id), "username": role_usernames.get(user_id, "")}

        c.execute(
            "SELECT last_user_id FROM role_assignment_state WHERE role_id = %s",
            (role_id,),
        )
        last_row = c.fetchone()
        last_user_id = last_row[0] if last_row else None

        placeholders = ", ".join(["%s"] * len(role_user_ids))
        c.execute(
            f"""
            SELECT user_id, COUNT(*) AS total
              FROM chat_assignments
             WHERE role_id = %s
               AND user_id IN ({placeholders})
             GROUP BY user_id
            """,
            [role_id, *role_user_ids],
        )
        assignment_counts = {row[0]: row[1] for row in c.fetchall()}

        selected_user_id = _pick_equitable_user_id(
            role_user_ids,
            last_user_id,
            assignment_counts,
        )

        c.execute(
            """
            INSERT INTO role_assignment_state (role_id, last_user_id, updated_at)
            VALUES (%s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              last_user_id = VALUES(last_user_id),
              updated_at = VALUES(updated_at)
            """,
            (role_id, selected_user_id),
        )
        c.execute(
            """
            INSERT INTO chat_assignments (numero, user_id, role_id, assigned_at)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              user_id = VALUES(user_id),
              role_id = VALUES(role_id),
              assigned_at = VALUES(assigned_at)
            """,
            (numero, selected_user_id, role_id),
        )
        conn.commit()
        return {
            "user_id": str(selected_user_id),
            "username": role_usernames.get(selected_user_id, ""),
        }
    finally:
        conn.close()


def assign_chat_to_non_admin_user(
    numero: str,
    role_ids: List[int],
) -> Optional[Dict[str, str]]:
    if not role_ids:
        return None

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT user_id FROM chat_assignments WHERE numero = %s", (numero,))
        if c.fetchone():
            return None

        role_placeholders = ", ".join(["%s"] * len(role_ids))
        c.execute(
            f"""
            SELECT DISTINCT u.id, COALESCE(NULLIF(u.nombre, ''), u.username)
              FROM usuarios u
              JOIN user_roles ur ON u.id = ur.user_id
             WHERE ur.role_id IN ({role_placeholders})
               AND u.id NOT IN (
                     SELECT ur_admin.user_id
                       FROM user_roles ur_admin
                       JOIN roles r_admin ON ur_admin.role_id = r_admin.id
                      WHERE r_admin.keyword = 'admin'
               )
             ORDER BY u.id
            """,
            role_ids,
        )
        role_users = c.fetchall()
        if not role_users:
            return None

        eligible_user_ids = [row[0] for row in role_users]
        user_names = {row[0]: row[1] for row in role_users}

        user_placeholders = ", ".join(["%s"] * len(eligible_user_ids))
        c.execute(
            f"""
            SELECT user_id, COUNT(*) AS total
              FROM chat_assignments
             WHERE user_id IN ({user_placeholders})
             GROUP BY user_id
            """,
            eligible_user_ids,
        )
        assignment_counts = {row[0]: row[1] for row in c.fetchall()}

        min_count = min(assignment_counts.get(user_id, 0) for user_id in eligible_user_ids)
        candidates = [user_id for user_id in eligible_user_ids if assignment_counts.get(user_id, 0) == min_count]

        selected_user_id = candidates[0]
        if len(candidates) > 1:
            c.execute(
                f"""
                SELECT user_id, MAX(assigned_at) AS last_assigned
                  FROM chat_assignments
                 WHERE user_id IN ({user_placeholders})
                 GROUP BY user_id
                """,
                eligible_user_ids,
            )
            last_assigned = {row[0]: row[1] for row in c.fetchall()}
            candidates.sort(
                key=lambda user_id: (
                    last_assigned.get(user_id) is not None,
                    last_assigned.get(user_id) or datetime.min,
                    user_id,
                )
            )
            selected_user_id = candidates[0]

        c.execute(
            f"""
            SELECT role_id
              FROM user_roles
             WHERE user_id = %s
               AND role_id IN ({role_placeholders})
             ORDER BY role_id
             LIMIT 1
            """,
            [selected_user_id, *role_ids],
        )
        role_row = c.fetchone()
        if not role_row:
            return None
        selected_role_id = role_row[0]

        c.execute(
            """
            INSERT INTO chat_assignments (numero, user_id, role_id, assigned_at)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
              user_id = VALUES(user_id),
              role_id = VALUES(role_id),
              assigned_at = VALUES(assigned_at)
            """,
            (numero, selected_user_id, selected_role_id),
        )
        conn.commit()
        return {
            "user_id": str(selected_user_id),
            "username": user_names.get(selected_user_id, ""),
            "role_id": str(selected_role_id),
        }
    finally:
        conn.close()
