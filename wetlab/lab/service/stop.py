"""Emergency / manual stop_all handling for active island devices and running protocols."""

import sqlite3
from typing import Any

from .. import registry
from ..backend import Backend
from ..lab import Lab
from ..store import inbox, runs
from . import reconcile


def stop_active_devices(
    backend: Backend,
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Invoke `pcr_stop` and `shake_stop` on active devices, bypassing `closed_skills`."""
    lab = Lab(backend, conn)
    live = lab.state()
    saved_closed = set(backend.closed_skills)
    outcomes: list[dict[str, Any]] = []
    try:
        backend.closed_skills.discard("pcr_stop")
        backend.closed_skills.discard("shake_stop")

        pcr_state = live.get("pcr") or {}
        pcr_active = (
            pcr_state.get("PcrState") in ("Running", "Paused")
            or pcr_state.get("HeaterLidEnabled") is True
        )
        if pcr_active:
            pcr_res = registry.call(lab, "pcr_stop")
            outcomes.append({"device": "pcr", "skill": "pcr_stop", **pcr_res})

        shaker_state = live.get("shaker") or {}
        shaker_speed = shaker_state.get("ActualSpeed") or 0
        shaker_active = shaker_state.get("ShakeState") == 0 or shaker_speed > 0
        if shaker_active:
            shaker_res = registry.call(lab, "shake_stop")
            outcomes.append({"device": "shaker", "skill": "shake_stop", **shaker_res})
    finally:
        backend.closed_skills = saved_closed
    return outcomes


def stop_all(backend: Backend, conn: sqlite3.Connection) -> dict[str, Any]:
    """Stop active devices, mark active protocols `stopped`, and report failed stops."""
    device_outcomes = stop_active_devices(backend, conn)
    failed_devices = [
        str(item["device"])
        for item in device_outcomes
        if item.get("status") != "succeeded"
    ]
    stop_ops = [
        oid
        for out in device_outcomes
        for oid in out.get("operation_ids", [])
    ]
    now = backend.now()
    stopped_protocols: list[str] = []

    with conn:
        reconcile.sync_device_state(conn, backend.state(), now=now)
        for status in ("running", "paused"):
            for proto in runs.list_protocols(conn, status=status):
                proto_id = proto["id"]
                steps = runs.list_steps(conn, proto_id)
                cur = next(
                    (
                        s
                        for s in steps
                        if s["status"] in ("sent", "waiting", "pending", "unknown")
                    ),
                    None,
                )
                if cur is not None:
                    raw_started = cur["started_at"]
                    started = float(raw_started if raw_started is not None else now)
                    elapsed = round(max(0.0, now - started), 3)
                    step_res = {
                        "ok": False,
                        "status": "failed",
                        "result": {},
                        "error": {"code": "STOPPED_BY_USER", "message": "被人工停止"},
                        "operation_ids": stop_ops,
                        "elapsed_s": elapsed,
                    }
                    runs.update_step(
                        conn,
                        proto_id,
                        int(cur["n"]),
                        status="failed",
                        started_at=started,
                        finished_at=now,
                        result=step_res,
                    )
                runs.update_protocol_status(conn, proto_id, "stopped")
                exp = runs.get_experiment(conn, proto["experiment_id"])
                ev_payload = {
                    "protocol_id": proto_id,
                    "experiment_id": proto["experiment_id"],
                    "mission_id": exp["mission_id"] if exp else None,
                    "reason": "被人工停止",
                }
                inbox.append_event(
                    conn,
                    at=now,
                    kind="protocol_stopped",
                    ref_id=proto_id,
                    payload=ev_payload,
                )
                stopped_protocols.append(proto_id)

        if failed_devices:
            err_msg = f"设备 {', '.join(failed_devices)} 没有停下来"
            ref_id = stopped_protocols[0] if stopped_protocols else None
            fail_payload = {
                "failed_devices": failed_devices,
                "protocol_ids": stopped_protocols,
                "error": err_msg,
            }
            inbox.append_event(
                conn,
                at=now,
                kind="stop_failed",
                ref_id=ref_id,
                payload=fail_payload,
            )
            return {
                "ok": False,
                "error": err_msg,
                "failed_devices": failed_devices,
                "stopped_protocols": stopped_protocols,
                "device_outcomes": device_outcomes,
            }

    return {
        "ok": True,
        "failed_devices": [],
        "stopped_protocols": stopped_protocols,
        "device_outcomes": device_outcomes,
    }
