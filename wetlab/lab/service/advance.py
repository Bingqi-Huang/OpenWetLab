"""Single-tick advancement of running protocols and `wait_until` monitoring (04 section 5.14)."""

import sqlite3
from typing import Any

from ..backend import Backend
from ..devices import BY_NAME
from ..lab import Lab
from ..protocol import check_wait_until, run_step
from ..safety import MAX_STATE_AGE_S
from ..store import inbox, island, runs
from . import reconcile
from .transitions import (
    WAIT_STALE_SINCE,
    abort_experiment,
    mark_protocol_done,
    mark_protocol_failed,
    pause_protocol,
    resolve_frozen_step,
    resume_protocol,
    start_approved_protocol,
)

__all__ = [
    "abort_experiment",
    "advance_running_protocol",
    "pause_protocol",
    "resolve_frozen_step",
    "resume_protocol",
    "start_approved_protocol",
]


def _is_telemetry_stale(device: str, live_state: dict[str, Any]) -> bool:
    dev_mod = BY_NAME[device]
    dev_state = live_state.get(device) or {}
    age = dev_state.get("_age_s")
    if age is None or float(age) > MAX_STATE_AGE_S:
        return True
    return any(dev_state.get(prop) is None for prop in dev_mod.spec.STATE_PROPS)


def _advance_waiting_step(
    backend: Backend,
    conn: sqlite3.Connection,
    proto: dict[str, Any],
    cur: dict[str, Any],
    total_steps: int,
    now: float,
) -> str:
    proto_id = proto["id"]
    step_n = int(cur["n"])
    deadline = float(cur["wait_deadline"])
    started_at = float(cur["started_at"] or now)
    if cur["kind"] == "wait":
        if now < deadline:
            return "waiting"
        elapsed = round(max(0.0, now - started_at), 3)
        res = {
            "ok": True,
            "status": "succeeded",
            "result": {"waited_s": elapsed},
            "error": None,
            "operation_ids": [],
            "elapsed_s": elapsed,
        }
        with conn:
            runs.update_step(conn, proto_id, step_n, status="done", finished_at=now, result=res)
            if step_n == total_steps:
                mark_protocol_done(conn, proto, at=now)
        return "step_done"

    spec = cur["content"]["wait_until"]
    dev_name = str(spec["device"])
    live = Lab(backend, conn).state()
    key = (proto_id, step_n)
    if dev_name in BY_NAME and _is_telemetry_stale(dev_name, live):
        if key not in WAIT_STALE_SINCE:
            age = (live.get(dev_name) or {}).get("_age_s")
            if age is not None and float(age) > MAX_STATE_AGE_S:
                WAIT_STALE_SINCE[key] = now - float(age)
            else:
                WAIT_STALE_SINCE[key] = now
        stale_since: float | None = WAIT_STALE_SINCE[key]
    else:
        WAIT_STALE_SINCE.pop(key, None)
        stale_since = None

    wait_check = check_wait_until(
        dev_name,
        spec["state"],
        live,
        now=now,
        deadline=deadline,
        stale_since=stale_since,
    )
    outcome = wait_check[0]
    code = wait_check[1]
    msg = wait_check[2]

    if outcome == "waiting":
        return "waiting"
    WAIT_STALE_SINCE.pop(key, None)
    if outcome == "freeze":
        reason = msg or f"等待期间失去 {dev_name} 的状态"
        frz_payload = {"protocol_id": proto_id, "step_n": step_n, "reason": reason}
        with conn:
            if not island.get_freeze(conn)["frozen"]:
                island.set_frozen(conn, reason=reason, at=now)
            inbox.append_event(conn, at=now, kind="frozen", ref_id=proto_id, payload=frz_payload)
        return "frozen"

    elapsed = round(max(0.0, now - started_at), 3)
    with conn:
        if outcome == "done":
            res = {
                "ok": True,
                "status": "succeeded",
                "result": {"waited_s": elapsed, "matched": spec["state"]},
                "error": None,
                "operation_ids": [],
                "elapsed_s": elapsed,
            }
            runs.update_step(conn, proto_id, step_n, status="done", finished_at=now, result=res)
            if step_n == total_steps:
                mark_protocol_done(conn, proto, at=now)
            return "step_done"
        err = {"code": code or "WAIT_FAILED", "message": msg or "wait_until failed"}
        res = {
            "ok": False,
            "status": "failed",
            "result": {},
            "error": err,
            "operation_ids": [],
            "elapsed_s": elapsed,
        }
        runs.update_step(conn, proto_id, step_n, status="failed", finished_at=now, result=res)
        mark_protocol_failed(conn, proto, at=now, step_n=step_n, error=err)
        return "failed"


def advance_running_protocol(backend: Backend, conn: sqlite3.Connection) -> str | None:
    """Advance at most one running protocol by one step/tick when unfrozen."""
    if island.get_freeze(conn)["frozen"]:
        return None
    running = runs.list_protocols(conn, status="running")
    if not running:
        return None

    proto = running[0]
    proto_id = proto["id"]
    steps = runs.list_steps(conn, proto_id)
    now = backend.now()

    cur = next((s for s in steps if s["status"] != "done"), None)
    if cur is None:
        with conn:
            mark_protocol_done(conn, proto, at=now)
        return "done"

    step_n = int(cur["n"])
    if cur["status"] == "failed":
        err_dict = (cur["result"] or {}).get("error")
        with conn:
            mark_protocol_failed(conn, proto, at=now, step_n=step_n, error=err_dict)
        return "failed"
    if cur["status"] in ("sent", "unknown"):
        return str(cur["status"])

    if cur["status"] == "pending" and cur["kind"] in ("wait", "wait_until"):
        c = cur["content"]
        dur = (
            float(c["wait"]["seconds"])
            if cur["kind"] == "wait"
            else float(c["wait_until"]["max_seconds"])
        )
        with conn:
            runs.update_step(
                conn,
                proto_id,
                step_n,
                status="waiting",
                started_at=now,
                wait_deadline=now + dur,
            )
        cur = runs.get_step(conn, proto_id, step_n)
        assert cur is not None

    if cur["status"] == "waiting":
        return _advance_waiting_step(backend, conn, proto, cur, len(steps), now)

    with conn:
        runs.update_step(conn, proto_id, step_n, status="sent", started_at=now)

    step_lab = Lab(backend, conn, protocol_id=proto_id, step_n=step_n)
    outcome_env = run_step(step_lab, cur["content"])
    finished_at = backend.now()
    out_status = outcome_env["status"]
    raw_after = backend.state()

    with conn:
        reconcile.sync_device_state(conn, raw_after, now=finished_at)
        if out_status == "succeeded":
            runs.update_step(
                conn,
                proto_id,
                step_n,
                status="done",
                finished_at=finished_at,
                result=outcome_env,
            )
            if step_n == len(steps):
                mark_protocol_done(conn, proto, at=finished_at)
            return "step_done"
        if out_status in ("unknown", "frozen"):
            runs.update_step(
                conn,
                proto_id,
                step_n,
                status="unknown",
                finished_at=finished_at,
                result=outcome_env,
            )
            frz = island.get_freeze(conn)
            frz_payload = {
                "protocol_id": proto_id,
                "step_n": step_n,
                "reason": frz["reason"],
                "operation_id": frz["operation_id"],
            }
            inbox.append_event(
                conn,
                at=finished_at,
                kind="frozen",
                ref_id=proto_id,
                payload=frz_payload,
            )
            return "unknown"
        runs.update_step(
            conn,
            proto_id,
            step_n,
            status="failed",
            finished_at=finished_at,
            result=outcome_env,
        )
        mark_protocol_failed(
            conn,
            proto,
            at=finished_at,
            step_n=step_n,
            error=outcome_env.get("error"),
        )

    reconcile.clear_failed_working_status(backend, conn)
    return "failed"
