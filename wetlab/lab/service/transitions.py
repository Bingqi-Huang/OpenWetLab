"""Protocol and step state transitions for approval, pause/resume, abort, and unfreeze."""

import sqlite3
from typing import Any

from ..store import inbox, runs

WAIT_STALE_SINCE: dict[tuple[str, int], float] = {}


def mission_id_for_experiment(conn: sqlite3.Connection, experiment_id: str) -> str | None:
    """Return the parent `mission_id` for `experiment_id`, or None."""
    exp = runs.get_experiment(conn, experiment_id)
    return exp["mission_id"] if exp is not None else None


def start_approved_protocol(
    conn: sqlite3.Connection,
    protocol_id: str,
    *,
    decided_by: str,
    decided_at: float,
    rehearsal: dict[str, Any],
) -> None:
    """Transition `submitted -> approved -> running` without committing."""
    runs.decide_protocol(
        conn,
        protocol_id,
        status="approved",
        decided_by=decided_by,
        decided_at=decided_at,
        rehearsal=rehearsal,
    )
    runs.update_protocol_status(conn, protocol_id, "running")


def pause_protocol(conn: sqlite3.Connection, protocol_id: str) -> None:
    """Transition `running -> paused` without committing."""
    runs.update_protocol_status(conn, protocol_id, "paused")


def resume_protocol(conn: sqlite3.Connection, protocol_id: str) -> None:
    """Transition `paused -> running` without committing."""
    runs.update_protocol_status(conn, protocol_id, "running")


def mark_protocol_done(conn: sqlite3.Connection, proto: dict[str, Any], *, at: float) -> None:
    """Mark `proto` as `done` and append `protocol_done` event without committing."""
    runs.update_protocol_status(conn, proto["id"], "done")
    payload = {
        "protocol_id": proto["id"],
        "experiment_id": proto["experiment_id"],
        "mission_id": mission_id_for_experiment(conn, proto["experiment_id"]),
    }
    inbox.append_event(conn, at=at, kind="protocol_done", ref_id=proto["id"], payload=payload)


def mark_protocol_failed(
    conn: sqlite3.Connection,
    proto: dict[str, Any],
    *,
    at: float,
    step_n: int,
    error: dict[str, Any] | None,
) -> None:
    """Mark `proto` as `failed` and append `protocol_failed` event without committing."""
    runs.update_protocol_status(conn, proto["id"], "failed")
    payload = {
        "protocol_id": proto["id"],
        "experiment_id": proto["experiment_id"],
        "mission_id": mission_id_for_experiment(conn, proto["experiment_id"]),
        "step_n": step_n,
        "error": error,
    }
    inbox.append_event(conn, at=at, kind="protocol_failed", ref_id=proto["id"], payload=payload)


def abort_experiment(
    conn: sqlite3.Connection,
    experiment_id: str,
    *,
    at: float,
    protocol_id: str | None = None,
) -> dict[str, Any]:
    """Mark active protocol `aborted`, experiment `stopped` ('人工终止'), and emit event."""
    exp = runs.get_experiment(conn, experiment_id)
    if exp is None:
        raise ValueError(f"unknown experiment: {experiment_id}")

    protos = (
        [runs.get_protocol(conn, protocol_id)]
        if protocol_id
        else runs.list_protocols(conn, experiment_id=experiment_id)
    )
    aborted_protos: list[str] = []
    err_res = {
        "ok": False,
        "status": "failed",
        "result": {},
        "error": {"code": "ABORTED", "message": "人工终止"},
    }
    for p in protos:
        if p is None or p["status"] in ("done", "failed", "rejected", "aborted"):
            continue
        pid = p["id"]
        for st in runs.list_steps(conn, pid):
            step_n = int(st["n"])
            WAIT_STALE_SINCE.pop((pid, step_n), None)
            if st["status"] in ("sent", "waiting", "unknown"):
                runs.update_step(conn, pid, step_n, status="failed", finished_at=at, result=err_res)
        runs.update_protocol_status(conn, pid, "aborted")
        aborted_protos.append(pid)

    runs.finish_experiment(conn, experiment_id, status="stopped", notes="人工终止")
    ev_payload = {
        "experiment_id": experiment_id,
        "mission_id": exp["mission_id"],
        "status": "stopped",
        "notes": "人工终止",
    }
    inbox.append_event(
        conn,
        at=at,
        kind="experiment_finished",
        ref_id=experiment_id,
        payload=ev_payload,
    )
    return {"experiment_id": experiment_id, "aborted_protocols": aborted_protos}


def resolve_frozen_step(
    conn: sqlite3.Connection,
    *,
    proto: dict[str, Any] | None,
    step: dict[str, Any] | None,
    conclusion: str,
    at: float,
) -> None:
    """Apply `step_done`, `step_failed`, or `abort` resolution during `unfreeze`."""
    if conclusion == "abort":
        if proto is not None:
            abort_experiment(conn, proto["experiment_id"], at=at, protocol_id=proto["id"])
        return
    if proto is None or step is None:
        return

    proto_id = proto["id"]
    step_n = int(step["n"])
    WAIT_STALE_SINCE.pop((proto_id, step_n), None)

    if step["status"] == "waiting" and conclusion == "step_done":
        return

    if conclusion == "step_done":
        elapsed = round(max(0.0, at - float(step["started_at"] or at)), 3)
        res = {
            "ok": True,
            "status": "succeeded",
            "result": {"unfreeze_conclusion": "step_done"},
            "error": None,
            "operation_ids": [],
            "elapsed_s": elapsed,
        }
        runs.update_step(conn, proto_id, step_n, status="done", finished_at=at, result=res)
        if all(s["status"] == "done" for s in runs.list_steps(conn, proto_id)):
            mark_protocol_done(conn, proto, at=at)
        return

    err = {"code": "UNFREEZE_STEP_FAILED", "message": "人工确认该步未完成"}
    res = {
        "ok": False,
        "status": "failed",
        "result": {},
        "error": err,
        "operation_ids": [],
        "elapsed_s": 0.0,
    }
    runs.update_step(conn, proto_id, step_n, status="failed", finished_at=at, result=res)
    mark_protocol_failed(conn, proto, at=at, step_n=step_n, error=err)
