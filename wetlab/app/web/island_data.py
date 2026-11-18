"""Read model for the island: mode, link, devices, plates, freeze, alerts, and to-dos."""

import json
import sqlite3
from typing import Any

from wetlab.lab.devices import BY_NAME, ISLAND, LABWARE_POSITIONS, PROPS
from wetlab.lab.protocol import describe_step
from wetlab.lab.safety import MAX_STATE_AGE_S
from wetlab.lab.snapshot import read_snapshot
from wetlab.lab.store import inbox, island, records, runs

from . import labels
from .db import configured_mode, island_now

_SKILL_DEVICE = {"pcr_stop": "pcr", "shake_stop": "shaker"}


def _link(gw: dict[str, Any], now: float) -> dict[str, Any]:
    """Gateway heartbeat written by service each round; stale means service stopped."""
    if gw["updated_at"] is None:
        return {"text": "未收到实验岛服务的状态", "tone": "gray", "ok": False}
    age = now - gw["updated_at"]
    if age > MAX_STATE_AGE_S:
        return {"text": f"实验岛服务 {labels.ago_text(age)}更新", "tone": "amber", "ok": False}
    if not gw["online"]:
        return {"text": "网关离线", "tone": "red", "ok": False}
    if gw["busy_by_others"]:
        return {"text": "网关被厂商系统占用", "tone": "amber", "ok": True}
    return {"text": "网关在线", "tone": "green", "ok": True}


def _rule_fault(name: str, props: dict[str, Any]) -> str | None:
    dev = BY_NAME.get(name)
    fault_fn = getattr(dev.rules, "fault", None) if (dev and dev.rules is not None) else None
    return fault_fn({name: props}) if fault_fn is not None else None


def _health(name: str, info: dict[str, Any], age: float | None) -> tuple[str, str]:
    if info["updated_at"] is None:
        return "无数据", "gray"
    if info["error"]:
        return "报错", "red"
    if _rule_fault(name, info["props"]):
        return "故障", "red"
    if age is not None and age > MAX_STATE_AGE_S:
        return "状态过期", "amber"
    working = info["props"].get("WorkingStatus")
    if working == 2:
        return "上次动作失败", "red"
    if working == 1:
        return "动作中", "blue"
    return "正常", "green"


def _devices(snap: dict[str, Any], now: float) -> list[dict[str, Any]]:
    out = []
    for name, info in snap["devices"].items():
        age = (now - info["updated_at"]) if info["updated_at"] is not None else None
        health_pair = _health(name, info, age)
        rows = [
            labels.prop_text(name, prop, info["props"][prop])
            for prop in PROPS[name]
            if prop in info["props"]
        ]
        out.append(
            {
                "name": name,
                "label": labels.device_label(name),
                "health": health_pair[0],
                "tone": health_pair[1],
                "rows": rows,
                "updated": labels.ago_text(age) if age is not None else "从未收到",
                "error": info["error"],
            }
        )
    return out


def read_labware_contents(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Read `labware_contents` rows grouped by `labware_id` in row order `n`."""
    sql = "SELECT labware_id, name, wells, volume_ul, note FROM labware_contents ORDER BY n ASC"
    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(sql).fetchall():
        parsed = json.loads(r["wells"]) if r["wells"] else None
        wells = [str(w) for w in parsed] if parsed else None
        vol = float(r["volume_ul"]) if r["volume_ul"] is not None else None
        note = str(r["note"]) if r["note"] is not None else None
        item = {"name": str(r["name"]), "wells": wells, "volume_ul": vol, "note": note}
        out.setdefault(str(r["labware_id"]), []).append(item)
    return out


def _tiles(snap: dict[str, Any], contents: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Positions grouped by the device they belong to, with the plate and its contents."""
    at_pos: dict[str, dict[str, Any]] = {}
    for lw_id, info in snap["labware_details"].items():
        items = contents.get(lw_id, [])
        summary = labels.contents_text(items)
        shown_type = f"{info['type']} · {summary}" if summary else str(info["type"])
        at_pos[info["position"]] = {"id": lw_id, "type": shown_type, "contents": items}
    tiles = []
    for dev in ISLAND:
        slots = [
            {"position": pos, "label": p.label, "labware": at_pos.get(pos)}
            for pos, p in LABWARE_POSITIONS.items()
            if p.device == dev.spec.NAME
        ]
        if slots:
            tiles.append({"label": dev.spec.LABEL, "slots": slots})
    return tiles


def _active_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    active = runs.list_protocols(conn, status="running") + runs.list_protocols(
        conn, status="paused"
    )
    if not active:
        return None
    proto = active[-1]
    steps = runs.list_steps(conn, proto["id"])
    done = sum(1 for s in steps if s["status"] == "done")
    current = next((s for s in steps if s["status"] != "done"), None)
    exp = runs.get_experiment(conn, proto["experiment_id"])
    return {
        "protocol": proto,
        "mission_id": exp["mission_id"] if exp else None,
        "done": done,
        "total": len(steps),
        "current": describe_step(current["content"]) if current else "",
    }


def _stop_failed_devices(conn: sqlite3.Connection) -> list[str]:
    ev_row = conn.execute(
        "SELECT at, payload_json FROM events WHERE kind = 'stop_failed' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    cmd_row = conn.execute(
        "SELECT handled_at, outcome FROM commands "
        "WHERE kind = 'stop_all' AND handled_at IS NOT NULL "
        "ORDER BY handled_at DESC, id DESC LIMIT 1"
    ).fetchone()
    started_row = conn.execute(
        "SELECT COALESCE(MAX(decided_at), 0) FROM protocols "
        "WHERE status IN ('running', 'paused', 'done')"
    ).fetchone()
    latest_started = float(started_row[0] or 0.0)
    cmd_outcome = json.loads(cmd_row["outcome"]) if (cmd_row and cmd_row["outcome"]) else None
    if cmd_row is not None and cmd_outcome and cmd_outcome.get("ok") is True:
        if ev_row is None or float(cmd_row["handled_at"]) >= float(ev_row["at"]):
            return []
    if ev_row is not None and latest_started <= float(ev_row["at"]):
        payload = json.loads(ev_row["payload_json"]) if ev_row["payload_json"] else {}
        devs = [str(d) for d in (payload.get("failed_devices") or []) if d]
        return devs or ["未知设备"]
    if cmd_row is not None and cmd_outcome and cmd_outcome.get("ok") is False:
        if latest_started <= float(cmd_row["handled_at"]):
            devs = [str(d) for d in (cmd_outcome.get("failed_devices") or []) if d]
            if not devs:
                raw_outs = cmd_outcome.get("device_outcomes") or []
                devs = [
                    _SKILL_DEVICE.get(str(o.get("skill")), str(o.get("skill") or "未知设备"))
                    for o in raw_outs
                    if o.get("status") != "succeeded"
                ]
            return devs or ["未知设备"]
    return []


def _last_human_command(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, key, who, kind, target_id, handled_at, outcome "
        "FROM commands "
        "WHERE handled_at IS NOT NULL AND who NOT IN ('coordinator', 'operator', 'agents') "
        "ORDER BY handled_at DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    outcome = json.loads(row["outcome"]) if row["outcome"] else {}
    kind = str(row["kind"])
    return {
        "id": int(row["id"]),
        "key": str(row["key"]),
        "who": str(row["who"]),
        "kind": kind,
        "label": labels.COMMAND_LABELS.get(kind, kind),
        "target_id": row["target_id"],
        "handled_at": float(row["handled_at"]),
        "ok": bool(outcome.get("ok")),
        "error": str(outcome.get("error") or ""),
        "outcome": outcome,
    }


def frozen_context(conn: sqlite3.Connection) -> dict[str, Any]:
    """Freeze row plus the protocol step it interrupted (`sent`, `unknown`, or `waiting`)."""
    fr = island.get_freeze(conn)
    op = records.get_operation(conn, fr["operation_id"]) if fr["operation_id"] else None
    step = None
    if op is not None and op["protocol_id"] is not None and op["step_n"] is not None:
        step = runs.get_step(conn, op["protocol_id"], int(op["step_n"]))
    if step is None:
        for proto in reversed(runs.list_protocols(conn)):
            if proto["status"] not in ("running", "paused"):
                continue
            active_statuses = ("sent", "unknown", "waiting")
            step = next(
                (s for s in runs.list_steps(conn, proto["id"]) if s["status"] in active_statuses),
                None,
            )
            if step is not None:
                break
    skill_name = step["content"].get("skill") if (step and step["kind"] == "skill") else None
    transfer_lw = str(step["content"]["args"]["labware_id"]) if skill_name == "transfer" else None
    return {
        "freeze": fr,
        "operation": op,
        "step": step,
        "step_text": describe_step(step["content"]) if step else None,
        "transfer_labware_id": transfer_lw,
    }


def _open_questions(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Missions whose latest Agent question came after the person's latest message."""
    last_q: dict[str, dict[str, Any]] = {}
    last_msg: dict[str, int] = {}
    for ev in inbox.list_events_after(conn, 0):
        mid = (ev["payload"] or {}).get("mission_id")
        if ev["kind"] == "question_asked" and mid:
            last_q[mid] = ev
        elif ev["kind"] == "message" and mid:
            last_msg[mid] = ev["id"]
    return {mid: ev for mid, ev in last_q.items() if ev["id"] > last_msg.get(mid, 0)}


def todos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Things only a person can do: review submitted protocols and answer questions."""
    items = []
    for proto in runs.list_protocols(conn, status="submitted"):
        exp = runs.get_experiment(conn, proto["experiment_id"])
        mission = runs.get_mission(conn, exp["mission_id"]) if exp else None
        submitted_str = labels.clock_text(proto["submitted_at"])
        items.append(
            {
                "mission_id": mission["id"] if mission else None,
                "tag": "待审阅",
                "title": f"方案 {proto['id']}",
                "sub": f"{mission['title'] if mission else ''} · {submitted_str} 提交",
                "action": "去审阅",
            }
        )
    for mid, ev in _open_questions(conn).items():
        mission = runs.get_mission(conn, mid)
        title = mission["title"] if mission else mid
        question = ev["payload"].get("question", "")
        items.append(
            {
                "mission_id": mid,
                "tag": "待回答",
                "title": "实验助手的提问",
                "sub": f"{title} · {question}",
                "action": "去回答",
            }
        )
    return items


def read_island(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything the island page and the header show, from one snapshot."""
    snap = read_snapshot(conn)
    now = island_now(snap["last_timestamp"])
    contents_by_id = read_labware_contents(conn)
    labware = {
        lw_id: {**info, "contents": contents_by_id.get(lw_id, [])}
        for lw_id, info in snap["labware_details"].items()
    }
    return {
        "mode": island.get_island_mode(conn) or configured_mode(),
        "now": now,
        "link": _link(snap["gateway"], now),
        "frozen": bool(snap["freeze"]["frozen"]),
        "devices": _devices(snap, now),
        "closed_skills": sorted(island.get_closed_skills(conn)),
        "stop_failed_devices": _stop_failed_devices(conn),
        "last_human_command": _last_human_command(conn),
        "tiles": _tiles(snap, contents_by_id),
        "labware": labware,
        "active": _active_run(conn),
    }


def open_question_missions(conn: sqlite3.Connection) -> set[str]:
    return set(_open_questions(conn))
