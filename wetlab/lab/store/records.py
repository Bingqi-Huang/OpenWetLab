"""Non-committing SQL helpers for operations, labware, contents, and labware_events."""

import json
import sqlite3
from typing import Any

from .contents import (
    find_labware_by_content as find_labware_by_content,
    list_contents_by_labware as list_contents_by_labware,
    list_labware_contents as list_labware_contents,
    replace_labware_contents as replace_labware_contents,
)

OP_SUCCEEDED = "succeeded"


def _row_to_operation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "seq": row["seq"],
        "protocol_id": row["protocol_id"],
        "step_n": row["step_n"],
        "device": row["device"],
        "action": row["action"],
        "params": json.loads(row["params_json"]),
        "status": row["status"],
        "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
        "error_code": row["error_code"],
        "started_at": row["started_at"],
        "elapsed_s": row["elapsed_s"],
    }


def next_operation_seq(conn: sqlite3.Connection) -> int:
    """Return the next sequential operation number (1-based) in this database."""
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM operations").fetchone()
    return int(row[0])


def insert_operation(
    conn: sqlite3.Connection,
    *,
    id: str,
    seq: int,
    device: str,
    action: str,
    params: dict[str, Any],
    status: str,
    started_at: float,
    elapsed_s: float = 0.0,
    protocol_id: str | None = None,
    step_n: int | None = None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    """Insert an operation row without committing."""
    conn.execute(
        """
        INSERT INTO operations (
            id, seq, protocol_id, step_n, device, action,
            params_json, status, result_json, error_code, started_at, elapsed_s
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            id,
            seq,
            protocol_id,
            step_n,
            device,
            action,
            json.dumps(params, ensure_ascii=False),
            status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            error_code,
            started_at,
            elapsed_s,
        ),
    )


def update_operation(
    conn: sqlite3.Connection,
    id: str,
    *,
    status: str,
    elapsed_s: float,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    """Update outcome fields of an existing operation row without committing."""
    conn.execute(
        """
        UPDATE operations
        SET status = ?, result_json = ?, error_code = ?, elapsed_s = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps(result, ensure_ascii=False) if result is not None else None,
            error_code,
            elapsed_s,
            id,
        ),
    )


def get_operation(conn: sqlite3.Connection, id: str) -> dict[str, Any] | None:
    """Fetch a single operation by id, or None if absent."""
    row = conn.execute("SELECT * FROM operations WHERE id = ?", (id,)).fetchone()
    return _row_to_operation(row) if row is not None else None


def last_operation(
    conn: sqlite3.Connection,
    device: str,
    action: str,
) -> dict[str, Any] | None:
    """Return the latest succeeded operation for (device, action), ordered by seq."""
    row = conn.execute(
        """
        SELECT * FROM operations
        WHERE device = ? AND action = ? AND status = ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (device, action, OP_SUCCEEDED),
    ).fetchone()
    return _row_to_operation(row) if row is not None else None


def list_operations(
    conn: sqlite3.Connection,
    *,
    protocol_id: str | None = None,
    step_n: int | None = None,
    device: str | None = None,
) -> list[dict[str, Any]]:
    """List operations in seq order, with optional protocol/step/device filters."""
    rows = conn.execute(
        """
        SELECT * FROM operations
        WHERE (? IS NULL OR protocol_id = ?)
          AND (? IS NULL OR step_n = ?)
          AND (? IS NULL OR device = ?)
        ORDER BY seq ASC
        """,
        (protocol_id, protocol_id, step_n, step_n, device, device),
    ).fetchall()
    return [_row_to_operation(row) for row in rows]


def _row_to_labware(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "position": row["position"],
    }


def get_labware(conn: sqlite3.Connection, labware_id: str) -> dict[str, Any] | None:
    """Fetch a labware item by id, or None if never registered."""
    row = conn.execute(
        "SELECT id, type, position FROM labware WHERE id = ?",
        (labware_id,),
    ).fetchone()
    return _row_to_labware(row) if row is not None else None


def get_labware_at(conn: sqlite3.Connection, position: str) -> dict[str, Any] | None:
    """Fetch the labware item currently occupying `position`, or None."""
    row = conn.execute(
        "SELECT id, type, position FROM labware WHERE position = ?",
        (position,),
    ).fetchone()
    return _row_to_labware(row) if row is not None else None


def list_labware(
    conn: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    """List labware items ordered by id (only on-island items when active_only=True)."""
    rows = conn.execute(
        """
        SELECT id, type, position FROM labware
        WHERE (? = 0 OR position IS NOT NULL)
        ORDER BY id ASC
        """,
        (int(active_only),),
    ).fetchall()
    return [_row_to_labware(row) for row in rows]


def upsert_labware(
    conn: sqlite3.Connection,
    *,
    id: str,
    type: str,
    position: str | None,
) -> None:
    """Insert or update a labware row without committing."""
    conn.execute(
        """
        INSERT INTO labware (id, type, position)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type = excluded.type,
            position = excluded.position
        """,
        (id, type, position),
    )


def update_labware_position(
    conn: sqlite3.Connection,
    labware_id: str,
    position: str | None,
) -> None:
    """Update the position of an existing labware row without committing."""
    conn.execute(
        "UPDATE labware SET position = ? WHERE id = ?",
        (position, labware_id),
    )


def insert_labware_event(
    conn: sqlite3.Connection,
    *,
    labware_id: str,
    at: float,
    what: str,
    by: str,
    detail: dict[str, Any] | None = None,
    operation_id: str | None = None,
) -> int:
    """Append an event to `labware_events` without committing and return its id."""
    cur = conn.execute(
        """
        INSERT INTO labware_events (labware_id, at, what, detail_json, operation_id, by)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            labware_id,
            at,
            what,
            json.dumps(detail if detail is not None else {}, ensure_ascii=False),
            operation_id,
            by,
        ),
    )
    return int(cur.lastrowid)


def list_labware_events(
    conn: sqlite3.Connection,
    labware_id: str,
) -> list[dict[str, Any]]:
    """List all events for `labware_id` in chronological insertion order."""
    rows = conn.execute(
        """
        SELECT id, labware_id, at, what, detail_json, operation_id, by
        FROM labware_events
        WHERE labware_id = ?
        ORDER BY id ASC
        """,
        (labware_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "labware_id": row["labware_id"],
            "at": row["at"],
            "what": row["what"],
            "detail": json.loads(row["detail_json"]),
            "operation_id": row["operation_id"],
            "by": row["by"],
        }
        for row in rows
    ]
