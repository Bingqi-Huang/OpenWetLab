"""Mission-scoped query helpers isolating a single mission's facts (04 section 6.5)."""

from dataclasses import dataclass
import json
import sqlite3
from typing import Any

from ..store import inbox, records, runs

HUMAN_INTERVENTION_KINDS: frozenset[str] = frozenset(
    {"pause", "resume", "stop_all", "unfreeze", "abort", "correct_labware", "reject"}
)
SYSTEM_ALERT_EVENT_KINDS: frozenset[str] = frozenset(
    {"frozen", "gateway_offline", "stop_failed"}
)


@dataclass
class MissionReportData:
    """Strictly mission-scoped snapshot of SQLite records for rendering `report.md`."""

    mission: dict[str, Any]
    experiments: list[dict[str, Any]]
    protocols: list[dict[str, Any]]
    steps_by_proto: dict[str, list[dict[str, Any]]]
    operations: list[dict[str, Any]]
    labware_ids: list[str]
    labware_rows: dict[str, dict[str, Any] | None]
    labware_contents: dict[str, list[dict[str, Any]]]
    labware_events: dict[str, list[dict[str, Any]]]
    intervention_commands: list[dict[str, Any]]
    alert_events: list[dict[str, Any]]


def _all_commands(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, key, at, who, kind, target_id, payload_json, handled_at, outcome "
        "FROM commands ORDER BY id ASC"
    )
    rows = conn.execute(sql).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        raw_p = json.loads(r["payload_json"])
        payload = raw_p if isinstance(raw_p, dict) else {}
        out.append(
            {
                "id": int(r["id"]),
                "key": r["key"],
                "at": float(r["at"]),
                "who": r["who"],
                "kind": r["kind"],
                "target_id": r["target_id"],
                "payload": payload,
                "handled_at": r["handled_at"],
                "outcome": r["outcome"],
            }
        )
    return out


def _declared_labware_ids(
    experiments: list[dict[str, Any]],
    protocols: list[dict[str, Any]],
) -> list[str]:
    declared: set[str] = set()
    for exp in experiments:
        task_lw = exp["task"].get("labware")
        if isinstance(task_lw, dict):
            declared.update(str(k) for k in task_lw)
    for proto in protocols:
        proto_lw = proto["content"].get("labware")
        if isinstance(proto_lw, dict):
            declared.update(str(k) for k in proto_lw)
    return sorted(declared)


def _command_in_mission(
    cmd: dict[str, Any],
    *,
    mission_id: str,
    exp_ids: set[str],
    proto_ids: set[str],
    lw_ids: set[str],
    start_at: float,
    end_at: float,
    start_cmd_id: int | None,
    end_cmd_id: int | None,
) -> bool:
    if cmd["kind"] not in HUMAN_INTERVENTION_KINDS:
        return False
    if not (start_at <= cmd["at"] <= end_at):
        return False
    if start_cmd_id is not None and cmd["id"] <= start_cmd_id:
        return False
    if end_cmd_id is not None and cmd["id"] >= end_cmd_id:
        return False

    kind = cmd["kind"]
    p = cmd["payload"]
    tid = cmd["target_id"]
    if kind in ("pause", "resume", "reject"):
        return (p.get("protocol_id") or tid) in proto_ids
    if kind == "abort":
        return (p.get("experiment_id") or tid) in exp_ids or p.get("protocol_id") in proto_ids
    if kind == "correct_labware":
        return (p.get("labware_id") or tid) in lw_ids
    if kind == "stop_all":
        outcome = json.loads(cmd["outcome"]) if cmd["outcome"] else {}
        stopped = outcome.get("stopped_protocols")
        if isinstance(stopped, list) and stopped:
            return bool(set(stopped) & proto_ids)
        return tid in (None, mission_id) or tid in proto_ids
    lw_id = p.get("labware_id")
    if lw_id is not None and lw_ids and lw_id not in lw_ids:
        return False
    return tid in (None, mission_id) or tid in exp_ids or tid in proto_ids


def _event_in_mission(
    ev: dict[str, Any],
    *,
    owned_ids: set[str],
    proto_ids: set[str],
    start_at: float,
    end_at: float,
    start_ev_id: int | None,
    end_ev_id: int | None,
) -> bool:
    if ev["kind"] not in SYSTEM_ALERT_EVENT_KINDS:
        return False
    if not (start_at <= float(ev["at"]) <= end_at):
        return False
    ev_id = int(ev["id"])
    if start_ev_id is not None and ev_id <= start_ev_id:
        return False
    if end_ev_id is not None and ev_id >= end_ev_id:
        return False
    if ev["ref_id"] is not None and ev["ref_id"] not in owned_ids:
        return False
    p = ev["payload"] or {}
    if p.get("protocol_id") is not None and p["protocol_id"] not in proto_ids:
        return False
    pids = p.get("protocol_ids")
    if isinstance(pids, list) and pids and not (set(pids) & proto_ids):
        return False
    return True


def _scoped_contents(
    conn: sqlite3.Connection,
    lw_id: str,
    all_evs: list[dict[str, Any]],
    end_at: float,
) -> list[dict[str, Any]]:
    content_evs = [e for e in all_evs if e["what"] in ("placed", "contents_updated")]
    if any(float(e["at"]) > end_at for e in content_evs):
        prior = [e for e in content_evs if float(e["at"]) <= end_at]
        return list(prior[-1]["detail"].get("contents") or []) if prior else []
    return records.list_labware_contents(conn, lw_id)


def collect_mission_data(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    finished_at: float | None = None,
) -> MissionReportData:
    """Query and strictly scope all SQLite rows belonging to `mission_id`."""
    mission = runs.get_mission(conn, mission_id)
    if mission is None:
        raise ValueError(f"unknown mission: {mission_id}")

    experiments = runs.list_experiments(conn, mission_id=mission_id)
    exp_ids = {e["id"] for e in experiments}
    protocols: list[dict[str, Any]] = []
    for exp in experiments:
        protocols.extend(runs.list_protocols(conn, experiment_id=exp["id"]))
    proto_ids = {p["id"] for p in protocols}
    steps_by_proto = {p["id"]: runs.list_steps(conn, p["id"]) for p in protocols}

    ops: list[dict[str, Any]] = []
    for pid in proto_ids:
        ops.extend(records.list_operations(conn, protocol_id=pid))
    ops.sort(key=lambda op: op["seq"])

    all_events = inbox.list_events_after(conn, 0)
    created_evs = [
        e for e in all_events if e["kind"] == "mission_created" and e["ref_id"] == mission_id
    ]
    finished_evs = [
        e for e in all_events if e["kind"] == "mission_finished" and e["ref_id"] == mission_id
    ]
    start_ev_id = int(created_evs[0]["id"]) if created_evs else None
    end_ev_id = int(finished_evs[-1]["id"]) if finished_evs else None
    start_at = float(mission["created_at"])
    if finished_at is not None:
        end_at = float(finished_at)
    elif finished_evs:
        end_at = float(finished_evs[-1]["at"])
    else:
        end_at = float("inf")

    prev_fin_evs = [
        e
        for e in all_events
        if e["kind"] == "mission_finished"
        and e["ref_id"] != mission_id
        and (start_ev_id is None or int(e["id"]) < start_ev_id)
    ]
    prev_end_at = max((float(e["at"]) for e in prev_fin_evs), default=None)

    all_cmds = _all_commands(conn)
    new_cmds = [
        c
        for c in all_cmds
        if c["kind"] == "new_mission"
        and (
            c["target_id"] == mission_id
            or c["payload"].get("mission_id") == mission_id
            or json.loads(c["outcome"] or "{}").get("mission_id") == mission_id
        )
    ]
    fin_cmds = [
        c
        for c in all_cmds
        if c["kind"] == "finish_mission"
        and (c["target_id"] == mission_id or c["payload"].get("mission_id") == mission_id)
    ]
    start_cmd_id = new_cmds[0]["id"] if new_cmds else None
    end_cmd_id = fin_cmds[-1]["id"] if fin_cmds else None

    lw_ids = _declared_labware_ids(experiments, protocols)
    lw_rows: dict[str, dict[str, Any] | None] = {}
    lw_contents: dict[str, list[dict[str, Any]]] = {}
    lw_events: dict[str, list[dict[str, Any]]] = {}
    for lw_id in lw_ids:
        lw_rows[lw_id] = records.get_labware(conn, lw_id)
        all_lw_evs = records.list_labware_events(conn, lw_id)
        lw_contents[lw_id] = _scoped_contents(conn, lw_id, all_lw_evs, end_at)
        scoped: list[dict[str, Any]] = []
        for ev in all_lw_evs:
            ev_at = float(ev["at"])
            if not (start_at <= ev_at <= end_at):
                continue
            if prev_end_at is not None and start_at == prev_end_at and ev_at <= prev_end_at:
                if end_at > start_at and ev["what"] not in ("placed", "contents_updated"):
                    continue
            op_id = ev["operation_id"]
            if op_id is not None:
                op = records.get_operation(conn, str(op_id))
                if op and op["protocol_id"] and op["protocol_id"] not in proto_ids:
                    continue
            scoped.append(ev)
        lw_events[lw_id] = scoped

    interventions = [
        c
        for c in all_cmds
        if _command_in_mission(
            c,
            mission_id=mission_id,
            exp_ids=exp_ids,
            proto_ids=proto_ids,
            lw_ids=set(lw_ids),
            start_at=start_at,
            end_at=end_at,
            start_cmd_id=start_cmd_id,
            end_cmd_id=end_cmd_id,
        )
    ]
    owned_ids = proto_ids | exp_ids | {mission_id}
    alerts = [
        e
        for e in all_events
        if _event_in_mission(
            e,
            owned_ids=owned_ids,
            proto_ids=proto_ids,
            start_at=start_at,
            end_at=end_at,
            start_ev_id=start_ev_id,
            end_ev_id=end_ev_id,
        )
    ]

    return MissionReportData(
        mission=mission,
        experiments=experiments,
        protocols=protocols,
        steps_by_proto=steps_by_proto,
        operations=ops,
        labware_ids=lw_ids,
        labware_rows=lw_rows,
        labware_contents=lw_contents,
        labware_events=lw_events,
        intervention_commands=interventions,
        alert_events=alerts,
    )
