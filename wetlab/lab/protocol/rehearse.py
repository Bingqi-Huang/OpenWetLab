"""Virtual dry-run rehearsal from a read-only SQLite snapshot (docs/spec/04 section 5.9)."""

import sqlite3
from typing import Any

from ..lab import Lab
from ..sim import Scenario, SimBackend
from ..snapshot import extract_key_state, read_snapshot
from ..store import island, open_db, records
from .model import Protocol, coerce_protocol
from .run import run_step
from .validate import validate


def _snapshot_key_state(snap: dict[str, Any]) -> dict[str, Any]:
    dev_props = {name: info["props"] for name, info in snap["devices"].items()}
    return extract_key_state(dev_props, snap["labware"])


def rehearse(
    conn: sqlite3.Connection,
    protocol: Protocol | dict[str, Any],
) -> dict[str, Any]:
    """Rehearse `protocol` on a fresh virtual island initialized from `conn`'s snapshot.

    Strictly read-only with respect to `conn`.
    """
    snap = read_snapshot(conn)
    problems = validate(protocol, conn)
    if problems:
        return {
            "ok": False,
            "steps": [],
            "total_s": 0.0,
            "failure": {
                "step_n": None,
                "code": "VALIDATION_FAILED",
                "reason": "；".join(problems),
                "state": _snapshot_key_state(snap),
            },
            "accidents": [],
        }

    proto = coerce_protocol(protocol)
    scenario = Scenario.from_snapshot(snap)
    sim_backend = SimBackend(scenario)
    closed = island.get_closed_skills(conn)
    sim_backend.closed_skills = set(closed)
    sim_conn = open_db(":memory:")
    try:
        with sim_conn:
            island.set_closed_skills(sim_conn, closed)
            for lw_id, detail in snap["labware_details"].items():
                records.upsert_labware(
                    sim_conn,
                    id=lw_id,
                    type=detail["type"],
                    position=detail["position"],
                )

        sim_lab = Lab(sim_backend, sim_conn)
        executed: list[dict[str, Any]] = []
        failure: dict[str, Any] | None = None

        for idx, step in enumerate(proto.steps, start=1):
            outcome = run_step(sim_lab, step)
            entry = {
                "n": idx,
                "kind": step.kind,
                "step": step.to_step_dict(),
                "ok": outcome["ok"],
                "status": outcome["status"],
                "elapsed_s": outcome["elapsed_s"],
                "result": outcome["result"],
                "error": outcome["error"],
                "operation_ids": outcome["operation_ids"],
                "state_after": outcome["state_after"],
            }
            executed.append(entry)
            if not outcome["ok"]:
                err = outcome["error"] or {}
                failure = {
                    "step_n": idx,
                    "code": err.get("code", "STEP_FAILED"),
                    "reason": err.get("message", f"step {idx} failed"),
                    "state": outcome["state_after"],
                }
                break

        accidents = list(sim_backend.world.accidents)
        if failure is None and accidents:
            last_state = executed[-1]["state_after"] if executed else _snapshot_key_state(snap)
            failure = {
                "step_n": len(executed),
                "code": "SIM_ACCIDENT",
                "reason": accidents[0]["what"],
                "state": last_state,
            }

        total_s = round(sum(float(s["elapsed_s"]) for s in executed), 3)
        return {
            "ok": failure is None and not accidents,
            "steps": executed,
            "total_s": total_s,
            "failure": failure,
            "accidents": accidents,
        }
    finally:
        sim_conn.close()
