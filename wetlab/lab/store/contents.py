"""Non-committing SQL helpers for the `labware_contents` table (04 section 5.7 & 16.8)."""

from collections.abc import Mapping, Sequence
import json
import sqlite3
from typing import Any


def _row_to_content(row: sqlite3.Row) -> dict[str, Any]:
    raw_wells = row["wells"]
    parsed_wells = json.loads(raw_wells) if raw_wells is not None else None
    wells = parsed_wells if parsed_wells else None
    vol = float(row["volume_ul"]) if row["volume_ul"] is not None else None
    return {
        "name": str(row["name"]),
        "wells": wells,
        "volume_ul": vol,
        "note": row["note"],
    }


def replace_labware_contents(
    conn: sqlite3.Connection,
    labware_id: str,
    items: Sequence[Mapping[str, Any]],
) -> None:
    """Replace all `labware_contents` rows for `labware_id` without committing."""
    conn.execute("DELETE FROM labware_contents WHERE labware_id = ?", (labware_id,))
    for idx, item in enumerate(items, start=1):
        raw_wells = item.get("wells")
        wells_list = [str(w).strip() for w in raw_wells if str(w).strip()] if raw_wells else []
        wells_json = json.dumps(wells_list, ensure_ascii=False) if wells_list else None
        raw_vol = item.get("volume_ul")
        vol = float(raw_vol) if raw_vol is not None else None
        raw_note = item.get("note")
        note = str(raw_note) if raw_note is not None else None
        conn.execute(
            """
            INSERT INTO labware_contents (labware_id, n, name, wells, volume_ul, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (labware_id, idx, str(item["name"]), wells_json, vol, note),
        )


def list_labware_contents(
    conn: sqlite3.Connection,
    labware_id: str,
) -> list[dict[str, Any]]:
    """Return `labware_contents` items for `labware_id` ordered by row number `n`."""
    rows = conn.execute(
        """
        SELECT name, wells, volume_ul, note
        FROM labware_contents
        WHERE labware_id = ?
        ORDER BY n ASC
        """,
        (labware_id,),
    ).fetchall()
    return [_row_to_content(row) for row in rows]


def list_contents_by_labware(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Return all `labware_contents` grouped by `labware_id`, preserving row order `n`."""
    rows = conn.execute(
        """
        SELECT labware_id, name, wells, volume_ul, note
        FROM labware_contents
        ORDER BY labware_id ASC, n ASC
        """
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        lw_id = str(row["labware_id"])
        grouped.setdefault(lw_id, []).append(_row_to_content(row))
    return grouped


def find_labware_by_content(
    conn: sqlite3.Connection,
    name: str,
    *,
    active_only: bool = True,
) -> list[str]:
    """Return deduplicated `labware_id` list containing `name` in insertion order."""
    rows = conn.execute(
        """
        SELECT c.labware_id
        FROM labware_contents AS c
        JOIN labware AS l ON l.id = c.labware_id
        WHERE c.name = ?
          AND (? = 0 OR l.position IS NOT NULL)
        ORDER BY c.rowid ASC
        """,
        (name, int(active_only)),
    ).fetchall()
    return list(dict.fromkeys(str(row["labware_id"]) for row in rows))
