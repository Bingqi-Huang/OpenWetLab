"""Non-committing SQL helpers for commands, events, and cursors."""

import json
import sqlite3
from typing import Any


def _row_to_command(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "key": row["key"],
        "at": row["at"],
        "who": row["who"],
        "kind": row["kind"],
        "target_id": row["target_id"],
        "payload": json.loads(row["payload_json"]),
        "handled_at": row["handled_at"],
        "outcome": row["outcome"],
    }


def enqueue_command(
    conn: sqlite3.Connection,
    *,
    key: str,
    at: float,
    who: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    target_id: str | None = None,
) -> bool:
    """Idempotently enqueue a command without committing; returns True iff newly inserted."""
    cur = conn.execute(
        """
        INSERT INTO commands (key, at, who, kind, target_id, payload_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (
            key,
            at,
            who,
            kind,
            target_id,
            json.dumps(payload if payload is not None else {}, ensure_ascii=False),
        ),
    )
    return cur.rowcount == 1


def get_command_by_key(
    conn: sqlite3.Connection,
    key: str,
) -> dict[str, Any] | None:
    """Fetch a command by its idempotency `key`, or None if absent."""
    row = conn.execute("SELECT * FROM commands WHERE key = ?", (key,)).fetchone()
    return _row_to_command(row) if row is not None else None


def list_unhandled_commands(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List unhandled commands (`handled_at IS NULL`) in arrival `id` order."""
    rows = conn.execute(
        "SELECT * FROM commands WHERE handled_at IS NULL ORDER BY id ASC"
    ).fetchall()
    return [_row_to_command(row) for row in rows]


def mark_command_handled(
    conn: sqlite3.Connection,
    command_id: int,
    *,
    handled_at: float,
    outcome: str,
) -> None:
    """Record handling timestamp and outcome on `command_id` without committing."""
    conn.execute(
        "UPDATE commands SET handled_at = ?, outcome = ? WHERE id = ?",
        (handled_at, outcome, command_id),
    )


def append_event(
    conn: sqlite3.Connection,
    *,
    at: float,
    kind: str,
    payload: dict[str, Any] | None = None,
    ref_id: str | None = None,
) -> int:
    """Append an event row without committing and return its monotonic `id`."""
    cur = conn.execute(
        """
        INSERT INTO events (at, kind, ref_id, payload_json)
        VALUES (?, ?, ?, ?)
        """,
        (
            at,
            kind,
            ref_id,
            json.dumps(payload if payload is not None else {}, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid)


def list_events_after(
    conn: sqlite3.Connection,
    after_id: int = 0,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """List events with `id > after_id` in ascending `id` order."""
    rows = conn.execute(
        """
        SELECT id, at, kind, ref_id, payload_json
        FROM events
        WHERE id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (after_id, limit if limit is not None else -1),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "at": row["at"],
            "kind": row["kind"],
            "ref_id": row["ref_id"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def get_cursor(conn: sqlite3.Connection, name: str) -> int:
    """Return `last_event_id` for `name` (0 if never set)."""
    row = conn.execute(
        "SELECT COALESCE((SELECT last_event_id FROM cursors WHERE name = ?), 0)",
        (name,),
    ).fetchone()
    return int(row[0])


def set_cursor(conn: sqlite3.Connection, name: str, last_event_id: int) -> None:
    """Upsert `last_event_id` for cursor `name` without committing."""
    conn.execute(
        """
        INSERT INTO cursors (name, last_event_id)
        VALUES (?, ?)
        ON CONFLICT(name) DO UPDATE SET last_event_id = excluded.last_event_id
        """,
        (name, last_event_id),
    )


def change_marker(conn: sqlite3.Connection) -> tuple[int, int]:
    """(Latest event id, number of handled commands): changes whenever the service records something new."""
    row = conn.execute(
        "SELECT (SELECT COALESCE(MAX(id), 0) FROM events), "
        "(SELECT COUNT(*) FROM commands WHERE handled_at IS NOT NULL)"
    ).fetchone()
    return int(row[0]), int(row[1])
