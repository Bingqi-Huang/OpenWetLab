"""Island health check (`wetlab doctor`, docs/spec/04 Step 7)."""

import sqlite3
import time
from typing import Any

from wetlab.lab.backend import Backend
from wetlab.lab.devices import ISLAND
from wetlab.lab.safety import MAX_STATE_AGE_S
from wetlab.lab.service.reconcile import sync_device_state
from wetlab.lab.snapshot import read_snapshot


def clock_now(conn: sqlite3.Connection) -> float:
    """Return wall clock or latest recorded telemetry timestamp (whichever is ahead)."""
    row = conn.execute("SELECT COALESCE(MAX(updated_at), 0) FROM device_state").fetchone()
    return max(time.time(), float(row[0] or 0.0))


def _probe_writable(conn: sqlite3.Connection, issues: list[str]) -> bool:
    try:
        conn.execute("SAVEPOINT doctor_write_probe")
        conn.execute("UPDATE freeze SET frozen = frozen")
        conn.execute("ROLLBACK TO SAVEPOINT doctor_write_probe")
        conn.execute("RELEASE SAVEPOINT doctor_write_probe")
        return True
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        issues.append(f"数据库不可写: {exc}")
        return False


def _device_fault_reason(dev: Any, info: dict[str, Any]) -> str | None:
    if info["error"]:
        return str(info["error"])
    props = info["props"]
    if props.get("WorkingStatus") == 2:
        return "WorkingStatus=2"
    fault_fn = getattr(dev.rules, "fault", None) if dev.rules is not None else None
    if fault_fn is not None:
        return fault_fn({dev.spec.NAME: props})
    return None


def run_doctor(
    conn: sqlite3.Connection,
    *,
    now: float | None = None,
    backend: Backend | None = None,
) -> dict[str, Any]:
    """Check gateway online, telemetry freshness, device errors, external busy, and DB write."""
    issues: list[str] = []
    db_writable = _probe_writable(conn, issues)

    if backend is not None and db_writable:
        ts = backend.now() if now is None else now
        with conn:
            sync_device_state(conn, backend.state(), now=ts)
        now = ts

    effective_now = now if now is not None else clock_now(conn)
    snap = read_snapshot(conn, now=effective_now)
    gw = snap["gateway"]
    gw_online = bool(gw["online"]) and (gw["age_s"] is None or gw["age_s"] <= MAX_STATE_AGE_S)
    if not gw_online:
        issues.append("网关不在线或心跳超时")
    busy_by_others = bool(gw["busy_by_others"])
    if busy_by_others:
        issues.append("网关正被外部指令占用（busy_by_others=true）")

    fresh_ok = True
    no_errors = True
    for dev in ISLAND:
        name = dev.spec.NAME
        info = snap["devices"][name]
        props = info["props"]
        age = info["age_s"]
        missing = [p for p in dev.spec.STATE_PROPS if props.get(p) is None]
        if info["updated_at"] is None or (age is not None and age > MAX_STATE_AGE_S) or missing:
            fresh_ok = False
            issues.append(f"设备 {name} 状态不新鲜或不完整（age={age}, missing={missing}）")
        fault_reason = _device_fault_reason(dev, info)
        if fault_reason:
            no_errors = False
            issues.append(f"设备 {name} 存在报错或故障: {fault_reason}")

    checks = {
        "gateway_online": gw_online,
        "devices_fresh": fresh_ok,
        "no_device_errors": no_errors,
        "not_busy_by_others": not busy_by_others,
        "db_writable": db_writable,
    }
    return {"ok": all(checks.values()), "checks": checks, "issues": issues}
