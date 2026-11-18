"""Coordinator/Operator handoff schemas, factual result builder, and event formatter."""

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..lab.store import records, runs

AgentRole = Literal["coordinator", "operator"]

COORDINATOR_TERMINAL_COMMANDS: frozenset[str] = frozenset(
    {"create_experiment", "ask", "finish_mission"}
)
OPERATOR_SUBMIT_OR_FINISH: frozenset[str] = frozenset(
    {"submit_protocol", "finish_experiment"}
)
OPERATOR_FINISH_ONLY: frozenset[str] = frozenset({"finish_experiment"})


class ExperimentTask(BaseModel):
    """Task sheet handed from Coordinator to Operator via `create_experiment`."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1)
    procedure: str = Field(min_length=1)
    labware: dict[str, str] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)


class StepOutcome(BaseModel):
    """Factual step execution record inside `ProtocolOutcome`."""

    n: int
    skill: str
    status: str
    elapsed_s: float
    operation_ids: list[str] = Field(default_factory=list)


class ProtocolOutcome(BaseModel):
    """Per-version protocol outcome inside `ExperimentResult` (`04` section 6.3)."""

    id: str
    status: str
    steps: list[StepOutcome] = Field(default_factory=list)


class LabwareOutcome(BaseModel):
    """Current labware position and chronological history within this experiment."""

    position: str | None
    history: list[str] = Field(default_factory=list)


class ExperimentResult(BaseModel):
    """Experiment outcome handed back to Coordinator (`04` section 6.3)."""

    experiment_id: str
    status: str
    protocols: list[ProtocolOutcome] = Field(default_factory=list)
    labware: dict[str, LabwareOutcome] = Field(default_factory=dict)
    notes: str = ""


@dataclass(frozen=True)
class WakeTarget:
    """Routing decision for one event in the `events` table."""

    role: AgentRole
    thread_id: str
    mission_id: str
    experiment_id: str | None
    required_commands: frozenset[str] | None


def _format_labware_event(ev: dict[str, Any]) -> str:
    detail = ev.get("detail") or {}
    if not detail:
        return str(ev["what"])
    return f"{ev['what']} {json.dumps(detail, ensure_ascii=False)}"


def _build_step_outcome(
    conn: sqlite3.Connection,
    protocol_id: str,
    step: dict[str, Any],
) -> StepOutcome:
    res = step["result"] or {}
    content = step["content"]
    skill_label = str(content.get("skill") or step["kind"])
    if step["status"] == "done":
        step_status = str(res.get("status") or "succeeded")
    else:
        step_status = str(res.get("status") or step["status"])
    if "elapsed_s" in res:
        elapsed = float(res["elapsed_s"])
    elif step["started_at"] is not None and step["finished_at"] is not None:
        elapsed = round(float(step["finished_at"] - step["started_at"]), 3)
    else:
        elapsed = 0.0
    op_ids = list(res.get("operation_ids") or [])
    if not op_ids:
        ops = records.list_operations(conn, protocol_id=protocol_id, step_n=step["n"])
        op_ids = [op["id"] for op in ops]
    return StepOutcome(
        n=step["n"],
        skill=skill_label,
        status=step_status,
        elapsed_s=elapsed,
        operation_ids=op_ids,
    )


def _experiment_window(
    conn: sqlite3.Connection,
    experiment_id: str,
    protos: list[dict[str, Any]],
) -> tuple[float | None, float | None, float | None]:
    start_row = conn.execute(
        "SELECT MIN(at) FROM events WHERE kind = 'experiment_created' AND ref_id = ?",
        (experiment_id,),
    ).fetchone()
    end_row = conn.execute(
        "SELECT MAX(at) FROM events WHERE kind = 'experiment_finished' AND ref_id = ?",
        (experiment_id,),
    ).fetchone()
    proto_start = float(protos[0]["submitted_at"]) if protos else None
    start_at = (
        float(start_row[0])
        if (start_row is not None and start_row[0] is not None)
        else proto_start
    )
    end_at = (
        float(end_row[0])
        if (end_row is not None and end_row[0] is not None)
        else None
    )
    return start_at, end_at, proto_start


def build_experiment_result(
    conn: sqlite3.Connection,
    experiment_id: str,
) -> ExperimentResult:
    """Generate factual `ExperimentResult` from SQLite plus Operator `notes`."""
    exp = runs.get_experiment(conn, experiment_id)
    if exp is None:
        raise ValueError(f"unknown experiment: {experiment_id}")

    protos = runs.list_protocols(conn, experiment_id=experiment_id)
    proto_outcomes: list[ProtocolOutcome] = []
    declared_lw_ids: list[str] = []

    for proto in protos:
        proto_lw = (proto["content"] or {}).get("labware") or {}
        for lw_id in proto_lw:
            if lw_id not in declared_lw_ids:
                declared_lw_ids.append(str(lw_id))
        raw_steps = runs.list_steps(conn, proto["id"])
        was_executed = proto["status"] not in ("rejected", "submitted") or any(
            s["status"] != "pending" or s["started_at"] is not None for s in raw_steps
        )
        steps = (
            [_build_step_outcome(conn, proto["id"], s) for s in raw_steps]
            if was_executed
            else []
        )
        proto_outcomes.append(
            ProtocolOutcome(id=proto["id"], status=proto["status"], steps=steps)
        )

    start_at, end_at, proto_start = _experiment_window(conn, experiment_id, protos)
    labware_summary: dict[str, LabwareOutcome] = {}
    for lw_id in declared_lw_ids:
        lw_row = records.get_labware(conn, lw_id)
        pos = lw_row["position"] if lw_row is not None else None
        filtered: list[str] = []
        for ev in records.list_labware_events(conn, lw_id):
            ev_at = float(ev["at"])
            if start_at is not None and ev_at < start_at:
                continue
            if end_at is not None and ev_at > end_at:
                continue
            if ev["by"] == "device" and proto_start is not None and ev_at < proto_start:
                continue
            filtered.append(_format_labware_event(ev))
        labware_summary[lw_id] = LabwareOutcome(position=pos, history=filtered)

    return ExperimentResult(
        experiment_id=experiment_id,
        status=exp["status"],
        protocols=proto_outcomes,
        labware=labware_summary,
        notes=exp["notes"] or "",
    )


def has_unanswered_question(
    conn: sqlite3.Connection,
    mission_id: str,
    before_event_id: int,
) -> bool:
    """Return True iff `mission_id` has an unanswered `question_asked` event."""
    q_row = conn.execute(
        "SELECT id FROM events "
        "WHERE kind = 'question_asked' AND ref_id = ? AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (mission_id, before_event_id),
    ).fetchone()
    if q_row is None:
        return False
    q_ev_id = int(q_row["id"])

    cmd_rows = conn.execute(
        "SELECT key, kind FROM commands WHERE who = 'coordinator' AND target_id = ?",
        (mission_id,),
    ).fetchall()
    for row in cmd_rows:
        if row["kind"] not in COORDINATOR_TERMINAL_COMMANDS:
            continue
        key = str(row["key"])
        if ":ev" in key:
            ev_part = key.split(":ev", 1)[1].split(":", 1)[0]
            if ev_part.isdigit() and q_ev_id < int(ev_part) < before_event_id:
                return False

    ev_rows = conn.execute(
        "SELECT kind, ref_id, payload_json FROM events WHERE id > ? AND id < ?",
        (q_ev_id, before_event_id),
    ).fetchall()
    for erow in ev_rows:
        if erow["kind"] == "mission_finished" and erow["ref_id"] == mission_id:
            return False
        if erow["kind"] == "experiment_created":
            ep = json.loads(erow["payload_json"])
            if ep.get("mission_id") == mission_id:
                return False
    return True


def classify_event(conn: sqlite3.Connection, event: dict[str, Any]) -> WakeTarget | None:
    """Return `WakeTarget` if `event` wakes an Agent per `04` section 6.4, else `None`."""
    kind = event["kind"]
    ref_id = str(event.get("ref_id") or "")
    payload = event.get("payload") or {}

    if kind == "mission_created":
        return WakeTarget("coordinator", ref_id, ref_id, None, COORDINATOR_TERMINAL_COMMANDS)

    if kind == "experiment_finished":
        exp = runs.get_experiment(conn, ref_id)
        mid = str(payload.get("mission_id") or (exp["mission_id"] if exp else ""))
        return WakeTarget("coordinator", mid, mid, ref_id, COORDINATOR_TERMINAL_COMMANDS)

    if kind == "message":
        mid = str(payload.get("mission_id") or ref_id)
        needs_cmd = has_unanswered_question(conn, mid, int(event["id"]))
        req = COORDINATOR_TERMINAL_COMMANDS if needs_cmd else None
        return WakeTarget("coordinator", mid, mid, None, req)

    if kind == "experiment_created":
        exp = runs.get_experiment(conn, ref_id)
        mid = str(payload.get("mission_id") or (exp["mission_id"] if exp else ""))
        return WakeTarget("operator", ref_id, mid, ref_id, OPERATOR_SUBMIT_OR_FINISH)

    if kind in ("protocol_rejected", "protocol_done", "protocol_failed", "protocol_stopped"):
        proto = runs.get_protocol(conn, ref_id)
        exp_id = str(payload.get("experiment_id") or (proto["experiment_id"] if proto else ref_id))
        exp = runs.get_experiment(conn, exp_id)
        mid = exp["mission_id"] if exp is not None else str(payload.get("mission_id") or "")
        req = OPERATOR_FINISH_ONLY if kind == "protocol_done" else OPERATOR_SUBMIT_OR_FINISH
        return WakeTarget("operator", exp_id, mid, exp_id, req)

    if kind == "command_failed":
        who = str(payload.get("who") or "")
        if who == "coordinator":
            mid = str(payload.get("mission_id") or ref_id)
            return WakeTarget("coordinator", mid, mid, None, COORDINATOR_TERMINAL_COMMANDS)
        if who == "operator":
            exp_id = str(payload.get("experiment_id") or ref_id)
            exp = runs.get_experiment(conn, exp_id)
            mid = str(payload.get("mission_id") or (exp["mission_id"] if exp else ""))
            return WakeTarget("operator", exp_id, mid, exp_id, OPERATOR_SUBMIT_OR_FINISH)

    return None


from .messages import event_to_message as event_to_message  # noqa: E402

