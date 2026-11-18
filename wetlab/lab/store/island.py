"""Non-committing SQL helpers for device_state, freeze, and island meta."""

from collections.abc import Iterable
import json
import sqlite3
from typing import Any


def _row_to_device_state(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "device": row["device"],
        "props": json.loads(row["props_json"]),
        "error": row["error"],
        "updated_at": row["updated_at"],
    }


def upsert_device_state(
    conn: sqlite3.Connection,
    *,
    device: str,
    props: dict[str, Any],
    updated_at: float,
    error: str | None = None,
) -> None:
    """Insert or update a device or gateway row in `device_state` without committing."""
    payload = json.dumps(props, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO device_state (device, props_json, error, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device) DO UPDATE SET
            props_json = excluded.props_json,
            error = excluded.error,
            updated_at = excluded.updated_at
        """,
        (device, payload, error, updated_at),
    )


def get_device_state(conn: sqlite3.Connection, device: str) -> dict[str, Any] | None:
    """Fetch a single device/gateway row from `device_state`, or None if absent."""
    row = conn.execute(
        "SELECT device, props_json, error, updated_at FROM device_state WHERE device = ?",
        (device,),
    ).fetchone()
    return _row_to_device_state(row) if row is not None else None


def get_all_device_states(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Read all rows from `device_state`, keyed by device name."""
    rows = conn.execute(
        "SELECT device, props_json, error, updated_at FROM device_state ORDER BY device ASC"
    ).fetchall()
    return {row["device"]: _row_to_device_state(row) for row in rows}


def get_freeze(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read the single-row `freeze` state."""
    row = conn.execute(
        "SELECT frozen, reason, operation_id, at FROM freeze"
    ).fetchone()
    return {
        "frozen": bool(row["frozen"]),
        "reason": row["reason"],
        "operation_id": row["operation_id"],
        "at": row["at"],
    }


def set_frozen(
    conn: sqlite3.Connection,
    *,
    reason: str,
    at: float,
    operation_id: str | None = None,
) -> None:
    """Mark the island as frozen without committing."""
    conn.execute(
        "UPDATE freeze SET frozen = 1, reason = ?, operation_id = ?, at = ?",
        (reason, operation_id, at),
    )


def clear_frozen(conn: sqlite3.Connection) -> None:
    """Reset the island to unfrozen without committing."""
    conn.execute(
        "UPDATE freeze SET frozen = 0, reason = NULL, operation_id = NULL, at = NULL"
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    """Read a value from `meta`, or None if absent."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row is not None else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Insert or update a key in `meta` without committing."""
    conn.execute(
        """
        INSERT INTO meta (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def get_db_id(conn: sqlite3.Connection) -> str:
    """Return this database's persistent `db_id`."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'db_id'").fetchone()
    return str(row["value"])


def get_island_mode(conn: sqlite3.Connection) -> str | None:
    """Return this database's bound `island_mode` ('virtual' or 'real'), or None."""
    return get_meta(conn, "island_mode")


def get_closed_skills(conn: sqlite3.Connection) -> set[str]:
    """Return the set of closed skill names persisted in `meta`, or an empty set."""
    raw = get_meta(conn, "closed_skills")
    if raw is None:
        return set()
    return set(json.loads(raw))


def set_closed_skills(conn: sqlite3.Connection, skills: Iterable[str]) -> None:
    """Persist the sorted list of closed skill names in `meta` without committing."""
    payload = json.dumps(sorted(set(skills)), ensure_ascii=False)
    set_meta(conn, "closed_skills", payload)
