"""Read-only island snapshot from SQLite (docs/spec/04 section 5.2 & 16.8)."""

from collections.abc import Mapping
import sqlite3
from typing import Any

from .devices import ISLAND, KEY_PROPS
from .store import island, records


def _last_timestamp(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        """
        SELECT MAX(ts) FROM (
            SELECT MAX(updated_at) AS ts FROM device_state
            UNION ALL SELECT MAX(started_at + elapsed_s) FROM operations
            UNION ALL SELECT MAX(at) FROM labware_events
            UNION ALL SELECT MAX(at) FROM freeze
            UNION ALL SELECT MAX(at) FROM events
            UNION ALL SELECT MAX(COALESCE(handled_at, at)) FROM commands
            UNION ALL SELECT MAX(created_at) FROM missions
            UNION ALL SELECT MAX(COALESCE(decided_at, submitted_at)) FROM protocols
            UNION ALL SELECT MAX(COALESCE(finished_at, started_at)) FROM steps
        )
        """
    ).fetchone()
    return float(row[0]) if row[0] is not None else 0.0


def extract_key_state(
    device_props: dict[str, dict[str, Any]],
    labware_positions: Mapping[str, Any],
) -> dict[str, Any]:
    """Filter device properties down to `KEY_PROPS` plus on-island labware positions."""
    summary: dict[str, Any] = {
        name: {prop: device_props[name][prop] for prop in keys if prop in device_props[name]}
        for name, keys in KEY_PROPS.items()
        if name in device_props
    }
    summary["labware"] = {
        lw_id: (val["position"] if isinstance(val, Mapping) else val)
        for lw_id, val in labware_positions.items()
    }
    return summary


def read_snapshot(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Read current device states, gateway health, labware, freeze, and latest timestamp."""
    stored_states = island.get_all_device_states(conn)
    devices_out: dict[str, dict[str, Any]] = {}
    for dev in ISLAND:
        name = dev.spec.NAME
        entry = stored_states.get(name)
        props = dict(entry["props"]) if entry is not None else {}
        updated_at = entry["updated_at"] if entry is not None else None
        age_s = (now - updated_at) if (now is not None and updated_at is not None) else None
        devices_out[name] = {
            "props": props,
            "key_props": {prop: props[prop] for prop in KEY_PROPS[name] if prop in props},
            "error": entry["error"] if entry is not None else None,
            "updated_at": updated_at,
            "age_s": age_s,
        }

    gw_entry = stored_states.get("_gateway")
    gw_props = gw_entry["props"] if gw_entry is not None else {}
    gw_updated_at = gw_entry["updated_at"] if gw_entry is not None else None
    gw_age_s = (
        (now - gw_updated_at) if (now is not None and gw_updated_at is not None) else None
    )

    active_labware = records.list_labware(conn, active_only=True)
    contents_by_lw = records.list_contents_by_labware(conn)
    labware_out = {
        item["id"]: {
            "position": item["position"],
            "type": item["type"],
            "contents": contents_by_lw.get(item["id"], []),
        }
        for item in active_labware
    }
    return {
        "devices": devices_out,
        "gateway": {
            "online": bool(gw_props.get("online", False)),
            "busy_by_others": bool(gw_props.get("busy_by_others", False)),
            "updated_at": gw_updated_at,
            "age_s": gw_age_s,
        },
        "labware": labware_out,
        "labware_details": {lw_id: dict(info) for lw_id, info in labware_out.items()},
        "freeze": island.get_freeze(conn),
        "last_timestamp": _last_timestamp(conn),
    }
