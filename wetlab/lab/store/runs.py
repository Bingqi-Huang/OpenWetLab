"""Non-committing SQL helpers for missions, experiments, protocols, and steps."""

from collections.abc import Sequence
import json
import sqlite3
from typing import Any


def _dumps(value: dict[str, Any] | None) -> str | None:
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def _loads(raw: str | None) -> dict[str, Any] | None:
    return json.loads(raw) if raw is not None else None


def _row_to_mission(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "input_text": row["input_text"],
        "mode": row["mode"],
        "status": row["status"],
        "created_at": row["created_at"],
        "conclusion": row["conclusion"],
    }


def insert_mission(
    conn: sqlite3.Connection, *, id: str, title: str, input_text: str,
    mode: str, status: str, created_at: float, conclusion: str | None = None,
) -> None:
    """Insert a mission row without committing."""
    conn.execute(
        "INSERT INTO missions (id, title, input_text, mode, status, created_at, conclusion) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id, title, input_text, mode, status, created_at, conclusion),
    )


def get_mission(conn: sqlite3.Connection, mission_id: str) -> dict[str, Any] | None:
    """Fetch a mission by id, or None if absent."""
    row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
    return _row_to_mission(row) if row is not None else None


def list_missions(conn: sqlite3.Connection, *, status: str | None = None) -> list[dict[str, Any]]:
    """List missions ordered by creation time and id, optionally filtered by status."""
    rows = conn.execute(
        "SELECT * FROM missions WHERE (? IS NULL OR status = ?) ORDER BY created_at ASC, id ASC",
        (status, status),
    ).fetchall()
    return [_row_to_mission(row) for row in rows]


def finish_mission(
    conn: sqlite3.Connection, mission_id: str, *, status: str, conclusion: str | None = None
) -> None:
    """Update mission status and conclusion without committing."""
    conn.execute(
        "UPDATE missions SET status = ?, conclusion = ? WHERE id = ?",
        (status, conclusion, mission_id),
    )


def _row_to_experiment(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "mission_id": row["mission_id"],
        "n": row["n"],
        "task": json.loads(row["task_json"]),
        "status": row["status"],
        "notes": row["notes"],
    }


def next_experiment_n(conn: sqlite3.Connection, mission_id: str) -> int:
    """Return the next 1-based experiment number `n` within `mission_id`."""
    row = conn.execute(
        "SELECT COALESCE(MAX(n), 0) + 1 FROM experiments WHERE mission_id = ?", (mission_id,)
    ).fetchone()
    return int(row[0])


def insert_experiment(
    conn: sqlite3.Connection, *, id: str, mission_id: str,
    n: int, task: dict[str, Any], status: str, notes: str | None = None,
) -> None:
    """Insert an experiment row without committing."""
    conn.execute(
        "INSERT INTO experiments (id, mission_id, n, task_json, status, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (id, mission_id, n, json.dumps(task, ensure_ascii=False), status, notes),
    )


def get_experiment(conn: sqlite3.Connection, experiment_id: str) -> dict[str, Any] | None:
    """Fetch an experiment by id, or None if absent."""
    row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
    return _row_to_experiment(row) if row is not None else None


def list_experiments(
    conn: sqlite3.Connection, *, mission_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    """List experiments ordered by mission_id and n."""
    rows = conn.execute(
        "SELECT * FROM experiments "
        "WHERE (? IS NULL OR mission_id = ?) AND (? IS NULL OR status = ?) "
        "ORDER BY mission_id ASC, n ASC",
        (mission_id, mission_id, status, status),
    ).fetchall()
    return [_row_to_experiment(row) for row in rows]


def finish_experiment(
    conn: sqlite3.Connection, experiment_id: str, *, status: str, notes: str | None = None
) -> None:
    """Update experiment status and operator notes without committing."""
    conn.execute(
        "UPDATE experiments SET status = ?, notes = ? WHERE id = ?",
        (status, notes, experiment_id),
    )


def _row_to_protocol(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "experiment_id": row["experiment_id"],
        "version": row["version"],
        "content": json.loads(row["content_json"]),
        "fingerprint": row["fingerprint"],
        "rehearsal": _loads(row["rehearsal_json"]),
        "text_md": row["text_md"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "decided_by": row["decided_by"],
        "decided_at": row["decided_at"],
        "reject_comment": row["reject_comment"],
        "positions_hash": row["positions_hash"],
    }


def next_protocol_version(conn: sqlite3.Connection, experiment_id: str) -> int:
    """Return the next 1-based protocol version under `experiment_id`."""
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM protocols WHERE experiment_id = ?",
        (experiment_id,),
    ).fetchone()
    return int(row[0])


def insert_protocol(
    conn: sqlite3.Connection,
    *,
    id: str,
    experiment_id: str,
    version: int,
    content: dict[str, Any],
    fingerprint: str,
    status: str,
    submitted_at: float,
    rehearsal: dict[str, Any] | None = None,
    text_md: str | None = None,
    decided_by: str | None = None,
    decided_at: float | None = None,
    reject_comment: str | None = None,
    positions_hash: str | None = None,
) -> None:
    """Insert a protocol row without committing."""
    cols = (
        "id, experiment_id, version, content_json, fingerprint, rehearsal_json, "
        "text_md, status, submitted_at, decided_by, decided_at, reject_comment, positions_hash"
    )
    vals = (
        id, experiment_id, version, json.dumps(content, ensure_ascii=False),
        fingerprint, _dumps(rehearsal), text_md, status, submitted_at,
        decided_by, decided_at, reject_comment, positions_hash,
    )
    conn.execute(
        f"INSERT INTO protocols ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", vals
    )


def get_protocol(conn: sqlite3.Connection, protocol_id: str) -> dict[str, Any] | None:
    """Fetch a protocol by id, or None if absent."""
    row = conn.execute("SELECT * FROM protocols WHERE id = ?", (protocol_id,)).fetchone()
    return _row_to_protocol(row) if row is not None else None


def list_protocols(
    conn: sqlite3.Connection, *, experiment_id: str | None = None, status: str | None = None
) -> list[dict[str, Any]]:
    """List protocols ordered by submission time, version, and id."""
    rows = conn.execute(
        "SELECT * FROM protocols "
        "WHERE (? IS NULL OR experiment_id = ?) AND (? IS NULL OR status = ?) "
        "ORDER BY submitted_at ASC, version ASC, id ASC",
        (experiment_id, experiment_id, status, status),
    ).fetchall()
    return [_row_to_protocol(row) for row in rows]


def decide_protocol(
    conn: sqlite3.Connection,
    protocol_id: str,
    *,
    status: str,
    decided_by: str | None,
    decided_at: float,
    reject_comment: str | None = None,
    rehearsal: dict[str, Any] | None = None,
    positions_hash: str | None = None,
) -> None:
    """Record approval or rejection decision fields on a protocol without committing."""
    sql = (
        "UPDATE protocols SET status = ?, decided_by = ?, decided_at = ?, reject_comment = ?, "
        "rehearsal_json = COALESCE(?, rehearsal_json), "
        "positions_hash = COALESCE(?, positions_hash) WHERE id = ?"
    )
    params = (
        status, decided_by, decided_at, reject_comment,
        _dumps(rehearsal), positions_hash, protocol_id,
    )
    conn.execute(sql, params)


def update_protocol_status(conn: sqlite3.Connection, protocol_id: str, status: str) -> None:
    """Update runtime protocol status (running/paused/done/failed/stopped/aborted)."""
    conn.execute("UPDATE protocols SET status = ? WHERE id = ?", (status, protocol_id))


def _row_to_step(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "protocol_id": row["protocol_id"],
        "n": row["n"],
        "kind": row["kind"],
        "content": json.loads(row["content_json"]),
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": _loads(row["result_json"]),
        "wait_deadline": row["wait_deadline"],
    }


def insert_steps(conn: sqlite3.Connection, protocol_id: str, steps: Sequence[dict[str, Any]]) -> None:
    """Bulk-insert step rows (`n`, `kind`, `content`, optional `status`) without committing."""
    conn.executemany(
        "INSERT INTO steps (protocol_id, n, kind, content_json, status) VALUES (?, ?, ?, ?, ?)",
        [
            (protocol_id, s["n"], s["kind"], json.dumps(s["content"], ensure_ascii=False), s.get("status", "pending"))
            for s in steps
        ],
    )


def get_step(conn: sqlite3.Connection, protocol_id: str, n: int) -> dict[str, Any] | None:
    """Fetch step `n` of `protocol_id`, or None if absent."""
    row = conn.execute("SELECT * FROM steps WHERE protocol_id = ? AND n = ?", (protocol_id, n)).fetchone()
    return _row_to_step(row) if row is not None else None


def list_steps(conn: sqlite3.Connection, protocol_id: str) -> list[dict[str, Any]]:
    """List all steps of `protocol_id` ordered by `n` ascending."""
    rows = conn.execute("SELECT * FROM steps WHERE protocol_id = ? ORDER BY n ASC", (protocol_id,)).fetchall()
    return [_row_to_step(row) for row in rows]


def update_step(
    conn: sqlite3.Connection,
    protocol_id: str,
    n: int,
    *,
    status: str,
    started_at: float | None = None,
    finished_at: float | None = None,
    result: dict[str, Any] | None = None,
    wait_deadline: float | None = None,
) -> None:
    """Update step status and non-None timing/result fields without committing."""
    conn.execute(
        "UPDATE steps SET status = ?, "
        "started_at = COALESCE(?, started_at), finished_at = COALESCE(?, finished_at), "
        "result_json = COALESCE(?, result_json), wait_deadline = COALESCE(?, wait_deadline) "
        "WHERE protocol_id = ? AND n = ?",
        (status, started_at, finished_at, _dumps(result), wait_deadline, protocol_id, n),
    )
