"""Runtime control command handlers (`pause`, `resume`, `unfreeze`, `abort`)."""

from collections.abc import Callable
import sqlite3
from typing import Any

from ..materials import Materials
from ..store import island, runs
from . import advance
from .protocol_commands import resolve_protocol
from .schemas import AbortPayload, CommandError, PausePayload, ResumePayload, UnfreezePayload


def handle_pause(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: PausePayload,
) -> dict[str, Any]:
    """Pause a running protocol."""
    proto = resolve_protocol(conn, model.protocol_id, cmd["target_id"], expected_status="running")
    if proto is None or proto["status"] != "running":
        raise CommandError("no running protocol to pause")
    advance.pause_protocol(conn, proto["id"])
    return {"ok": True, "protocol_id": proto["id"], "status": "paused"}


def handle_resume(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: ResumePayload,
) -> dict[str, Any]:
    """Resume a paused protocol (rejected for stopped protocols)."""
    proto = resolve_protocol(conn, model.protocol_id, cmd["target_id"], expected_status="paused")
    if proto is None or proto["status"] != "paused":
        raise CommandError("resume is only allowed for a paused protocol")
    advance.resume_protocol(conn, proto["id"])
    return {"ok": True, "protocol_id": proto["id"], "status": "running"}


def handle_unfreeze(
    conn: sqlite3.Connection,
    model: UnfreezePayload,
    now: float,
    on_lw: Callable[[str, str | None], None] | None,
) -> dict[str, Any]:
    """Resolve frozen step (`sent`/`unknown` or `waiting`) and lift island freeze."""
    if not island.get_freeze(conn)["frozen"]:
        raise CommandError("island is not frozen")

    running = runs.list_protocols(conn, status="running")
    paused = runs.list_protocols(conn, status="paused")
    active_proto = next(iter(running + paused), None)
    frozen_step: dict[str, Any] | None = None
    if active_proto is not None:
        proto_steps = runs.list_steps(conn, active_proto["id"])
        frozen_step = next(
            (s for s in proto_steps if s["status"] in ("sent", "unknown")),
            None,
        )
        if frozen_step is None:
            frozen_step = next(
                (s for s in proto_steps if s["status"] == "waiting"),
                None,
            )

    is_transfer = bool(
        frozen_step
        and frozen_step["kind"] == "skill"
        and frozen_step["content"].get("skill") == "transfer"
    )
    has_pos = model.labware_position not in (None, "")
    if is_transfer and not has_pos:
        raise CommandError("unfreeze on a transfer step requires labware_position")

    corrected_lw: tuple[str, str | None] | None = None
    if is_transfer or (model.labware_id is not None and has_pos):
        if model.labware_id:
            lw_id = model.labware_id
        else:
            assert frozen_step is not None
            lw_id = str(frozen_step["content"]["args"]["labware_id"])
        pos_val = None if model.labware_position == "removed" else str(model.labware_position)
        Materials(conn, lambda: now).correct(lw_id, pos_val)
        corrected_lw = (lw_id, pos_val)

    advance.resolve_frozen_step(
        conn,
        proto=active_proto,
        step=frozen_step,
        conclusion=model.conclusion,
        at=now,
    )
    island.clear_frozen(conn)
    if corrected_lw is not None and on_lw is not None:
        on_lw(corrected_lw[0], corrected_lw[1])
    return {"ok": True, "conclusion": model.conclusion}


def handle_abort(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: AbortPayload,
    now: float,
) -> dict[str, Any]:
    """Abort an active experiment and emit `experiment_finished`."""
    proto_id = model.protocol_id
    exp_id = model.experiment_id or (str(cmd["target_id"]) if cmd["target_id"] else None)
    if not exp_id and proto_id:
        pr = runs.get_protocol(conn, proto_id)
        exp_id = pr["experiment_id"] if pr else None
    if not exp_id:
        active = runs.list_protocols(conn, status="running")
        active.extend(runs.list_protocols(conn, status="paused"))
        exp_id = active[-1]["experiment_id"] if active else None
    if not exp_id or runs.get_experiment(conn, exp_id) is None:
        raise CommandError("cannot resolve experiment to abort")
    res = advance.abort_experiment(conn, exp_id, at=now, protocol_id=proto_id)
    return {"ok": True, **res}
