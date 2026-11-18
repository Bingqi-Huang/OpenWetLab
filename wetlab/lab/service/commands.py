"""Transactional command dispatcher for all 20 `commands` kinds (04 section 5.14)."""

from collections.abc import Callable
import json
from pathlib import Path
import sqlite3
from typing import Any

from pydantic import BaseModel

from ..backend import Backend
from ..store import inbox, island, runs
from . import messages, mission_commands, protocol_commands, runtime_commands, schemas, stop
from .schemas import (
    AbortPayload,
    AgentErrorPayload,
    ApprovePayload,
    AskPayload,
    CorrectLabwarePayload,
    CreateExperimentPayload,
    FinishExperimentPayload,
    FinishMissionPayload,
    MessagePayload,
    NewMissionPayload,
    PausePayload,
    PlaceLabwarePayload,
    RejectPayload,
    RemoveLabwarePayload,
    ReplyPayload,
    ResumePayload,
    SetLabwareContentsPayload,
    SubmitProtocolPayload,
    UnfreezePayload,
)

AGENT_ROLES: frozenset[str] = frozenset({"coordinator", "operator"})
LABWARE_MODELS = (
    PlaceLabwarePayload,
    RemoveLabwarePayload,
    CorrectLabwarePayload,
    SetLabwareContentsPayload,
)


def _dispatch_validated(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: BaseModel,
    now: float,
    runs_dir: Path,
    on_lw: Callable[[str, str | None], None] | None,
) -> dict[str, Any]:
    if isinstance(model, NewMissionPayload):
        return mission_commands.handle_new_mission(conn, model, now)
    if isinstance(model, LABWARE_MODELS):
        return mission_commands.handle_labware(conn, model, now, on_lw)
    if isinstance(model, CreateExperimentPayload):
        return mission_commands.handle_create_experiment(conn, cmd, model, now)
    if isinstance(model, SubmitProtocolPayload):
        return protocol_commands.handle_submit_protocol(conn, cmd, model, now, runs_dir)
    if isinstance(model, ApprovePayload):
        return protocol_commands.handle_approve(conn, cmd, model, now)
    if isinstance(model, RejectPayload):
        return protocol_commands.handle_reject(conn, cmd, model, now)
    if isinstance(model, PausePayload):
        return runtime_commands.handle_pause(conn, cmd, model)
    if isinstance(model, ResumePayload):
        return runtime_commands.handle_resume(conn, cmd, model)
    if isinstance(model, UnfreezePayload):
        return runtime_commands.handle_unfreeze(conn, model, now, on_lw)
    if isinstance(model, AbortPayload):
        return runtime_commands.handle_abort(conn, cmd, model, now)
    if isinstance(model, (AskPayload, ReplyPayload, MessagePayload)):
        return mission_commands.handle_conversation(conn, cmd, model, now)
    if isinstance(model, FinishExperimentPayload):
        return mission_commands.handle_finish_experiment(conn, cmd, model, now)
    if isinstance(model, FinishMissionPayload):
        return mission_commands.handle_finish_mission(conn, cmd, model, now, runs_dir)
    assert isinstance(model, AgentErrorPayload)
    return mission_commands.handle_agent_error(conn, cmd, model, now)


def _resolve_command_context(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
) -> tuple[str | None, str | None]:
    raw_p = cmd.get("payload")
    p = raw_p if isinstance(raw_p, dict) else {}
    raw_mid = p.get("mission_id")
    raw_eid = p.get("experiment_id")
    mid = raw_mid if isinstance(raw_mid, str) and raw_mid else None
    eid = raw_eid if isinstance(raw_eid, str) and raw_eid else None
    tid = cmd.get("target_id")
    if isinstance(tid, str) and tid:
        if cmd["who"] == "coordinator" and mid is None:
            mid = tid
        elif cmd["who"] == "operator" and eid is None:
            eid = tid
        elif tid.startswith("M-") and mid is None:
            mid = tid
        elif eid is None:
            eid = tid
    if eid is not None and mid is None:
        exp = runs.get_experiment(conn, eid)
        if exp is not None:
            mid = exp["mission_id"]
    return mid, eid


def _finalize_outcome(outcome: dict[str, Any], cmd: dict[str, Any]) -> dict[str, Any]:
    if outcome.get("ok") is not False:
        return outcome
    raw_val = outcome.get("error_raw")
    err_val = outcome.get("error")
    error_raw = str(raw_val if raw_val is not None else (err_val or ""))
    error = messages.translate_text_error(str(err_val or error_raw), cmd)
    return {**outcome, "ok": False, "error": error, "error_raw": error_raw}


def _record_failed_command(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    now: float,
    exc: BaseException,
    *,
    outer_tx: bool,
) -> dict[str, Any]:
    error_raw = str(exc)
    error = messages.format_command_error(exc, cmd)
    outcome = {"ok": False, "error": error, "error_raw": error_raw}
    outcome_json = json.dumps(outcome, ensure_ascii=False)
    cmd_id = int(cmd["id"])
    who = str(cmd["who"])

    def _write() -> None:
        inbox.mark_command_handled(conn, cmd_id, handled_at=now, outcome=outcome_json)
        if who in AGENT_ROLES:
            ctx_ids = _resolve_command_context(conn, cmd)
            mid = ctx_ids[0]
            eid = ctx_ids[1]
            ref_id = mid if who == "coordinator" else (eid or mid)
            fail_payload = {
                "who": who,
                "command_id": cmd_id,
                "kind": cmd["kind"],
                "error": error,
                "error_raw": error_raw,
                "mission_id": mid,
                "experiment_id": eid,
            }
            inbox.append_event(
                conn,
                at=now,
                kind="command_failed",
                ref_id=ref_id,
                payload=fail_payload,
            )

    if outer_tx:
        _write()
    else:
        with conn:
            _write()
    return outcome


def process_command(
    backend: Backend,
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    *,
    runs_dir: Path,
    on_labware_change: Callable[[str, str | None], None] | None = None,
) -> dict[str, Any]:
    """Validate and execute one command in a savepoint; rollback & log on error without freezing."""
    kind = str(cmd["kind"])
    cmd_id = int(cmd["id"])
    outer_tx = conn.in_transaction

    if kind == "stop_all":
        now = backend.now()
        try:
            schemas.validate_command_payload(kind, cmd["payload"])
        except Exception as exc:
            return _record_failed_command(conn, cmd, now, exc, outer_tx=outer_tx)
        outcome = _finalize_outcome(stop.stop_all(backend, conn), cmd)
        handled_at = backend.now()
        out_json = json.dumps(outcome, ensure_ascii=False)
        if outer_tx:
            inbox.mark_command_handled(conn, cmd_id, handled_at=handled_at, outcome=out_json)
        else:
            with conn:
                inbox.mark_command_handled(conn, cmd_id, handled_at=handled_at, outcome=out_json)
        return outcome

    now = backend.now()
    conn.execute("SAVEPOINT cmd_sp")
    try:
        if kind in ("submit_protocol", "approve"):
            island.set_closed_skills(conn, backend.closed_skills)
        model = schemas.validate_command_payload(kind, cmd["payload"])
        raw_outcome = _dispatch_validated(conn, cmd, model, now, runs_dir, on_labware_change)
        outcome = _finalize_outcome(raw_outcome, cmd)
        out_json = json.dumps(outcome, ensure_ascii=False)
        inbox.mark_command_handled(conn, cmd_id, handled_at=now, outcome=out_json)
        conn.execute("RELEASE SAVEPOINT cmd_sp")
        if not outer_tx:
            conn.commit()
        return outcome
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT cmd_sp")
        conn.execute("RELEASE SAVEPOINT cmd_sp")
        if not outer_tx and conn.in_transaction:
            conn.rollback()
        return _record_failed_command(conn, cmd, now, exc, outer_tx=outer_tx)
