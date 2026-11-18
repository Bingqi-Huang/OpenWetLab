"""Mission, experiment, labware, conversation, and agent-error command handlers."""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from ..materials import Materials
from ..store import inbox, island, runs
from . import report
from .schemas import (
    AgentErrorPayload,
    AskPayload,
    CommandError,
    CorrectLabwarePayload,
    CreateExperimentPayload,
    FinishExperimentPayload,
    FinishMissionPayload,
    MessagePayload,
    NewMissionPayload,
    PlaceLabwarePayload,
    RemoveLabwarePayload,
    ReplyPayload,
)


def next_mission_id(conn: sqlite3.Connection, now: float) -> str:
    """Return the next sequential `M-YYYYMMDD-NNN` mission ID."""
    ymd = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    prefix = f"M-{ymd}-"
    existing = [m["id"] for m in runs.list_missions(conn) if m["id"].startswith(prefix)]
    tails = [int(mid.rsplit("-", 1)[-1]) for mid in existing if mid.rsplit("-", 1)[-1].isdigit()]
    seq = max(tails, default=0) + 1
    return f"{prefix}{seq:03d}"


def next_experiment_id(conn: sqlite3.Connection) -> str:
    """Return the next sequential `E-NNN` experiment ID."""
    rows = conn.execute("SELECT id FROM experiments").fetchall()
    nums: list[int] = []
    for row in rows:
        val = str(row[0])
        parts = val.split("-")
        if val.startswith("E-") and len(parts) > 1 and parts[1].isdigit():
            nums.append(int(parts[1]))
    return f"E-{max(nums, default=0) + 1:03d}"


def handle_new_mission(
    conn: sqlite3.Connection,
    model: NewMissionPayload,
    now: float,
) -> dict[str, Any]:
    """Create a mission whose `mode` comes strictly from the island database (04 §5.13)."""
    mid = model.mission_id or next_mission_id(conn, now)
    title = model.title.strip() or "未命名任务"
    input_text = model.input_text
    mode = island.get_island_mode(conn) or "virtual"
    runs.insert_mission(
        conn,
        id=mid,
        title=title,
        input_text=input_text,
        mode=mode,
        status="running",
        created_at=now,
    )
    payload = {
        "mission_id": mid,
        "title": title,
        "input_text": input_text,
        "mode": mode,
    }
    inbox.append_event(conn, at=now, kind="mission_created", ref_id=mid, payload=payload)
    return {"ok": True, "mission_id": mid}


def handle_labware(
    conn: sqlite3.Connection,
    model: PlaceLabwarePayload | RemoveLabwarePayload | CorrectLabwarePayload,
    now: float,
    on_lw: Callable[[str, str | None], None] | None,
) -> dict[str, Any]:
    """Place, remove, or correct labware position in the ledger."""
    mat = Materials(conn, lambda: now)
    lw_id = model.labware_id
    if isinstance(model, PlaceLabwarePayload):
        pos: str | None = model.position
        mat.place(lw_id, model.type, model.position)
    elif isinstance(model, RemoveLabwarePayload):
        pos = None
        mat.remove(lw_id)
    else:
        pos = None if model.position in (None, "removed") else model.position
        mat.correct(lw_id, pos)
    if on_lw is not None:
        on_lw(lw_id, pos)
    return {"ok": True, "labware_id": lw_id}


def handle_create_experiment(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: CreateExperimentPayload,
    now: float,
) -> dict[str, Any]:
    """Create an experiment and task sheet for the Operator."""
    mid = str(model.mission_id or cmd["target_id"] or "")
    if runs.get_mission(conn, mid) is None:
        raise CommandError(f"unknown mission: {mid}")
    n = runs.next_experiment_n(conn, mid)
    exp_id = model.experiment_id or next_experiment_id(conn)
    task_dict = model.task.model_dump()
    runs.insert_experiment(conn, id=exp_id, mission_id=mid, n=n, task=task_dict, status="running")
    payload = {
        "experiment_id": exp_id,
        "mission_id": mid,
        "n": n,
        "task": task_dict,
    }
    inbox.append_event(conn, at=now, kind="experiment_created", ref_id=exp_id, payload=payload)
    return {"ok": True, "experiment_id": exp_id}


def handle_conversation(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: AskPayload | ReplyPayload | MessagePayload,
    now: float,
) -> dict[str, Any]:
    """Append `question_asked`, `reply`, or `message` conversation event."""
    mid = str(model.mission_id or cmd["target_id"] or "")
    if runs.get_mission(conn, mid) is None:
        raise CommandError(f"unknown mission: {mid}")
    if isinstance(model, AskPayload):
        ev_kind = "question_asked"
        body: dict[str, Any] = {"mission_id": mid, "question": model.question}
    else:
        ev_kind = str(cmd["kind"])
        body = {"mission_id": mid, "text": model.text}
    inbox.append_event(conn, at=now, kind=ev_kind, ref_id=mid, payload=body)
    return {"ok": True}


def handle_finish_experiment(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: FinishExperimentPayload,
    now: float,
) -> dict[str, Any]:
    """Finalize experiment status/notes and emit `experiment_finished`."""
    exp_id = str(model.experiment_id or cmd["target_id"] or "")
    exp = runs.get_experiment(conn, exp_id)
    if exp is None:
        raise CommandError(f"unknown experiment: {exp_id}")
    runs.finish_experiment(conn, exp_id, status=model.status, notes=model.notes)
    payload = {
        "experiment_id": exp_id,
        "mission_id": exp["mission_id"],
        "status": model.status,
        "notes": model.notes,
    }
    inbox.append_event(conn, at=now, kind="experiment_finished", ref_id=exp_id, payload=payload)
    return {"ok": True, "experiment_id": exp_id}


def handle_finish_mission(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: FinishMissionPayload,
    now: float,
    runs_dir: Path,
) -> dict[str, Any]:
    """Finalize mission conclusion, generate `report.md`, and emit `mission_finished`."""
    mid = str(model.mission_id or cmd["target_id"] or "")
    if runs.get_mission(conn, mid) is None:
        raise CommandError(f"unknown mission: {mid}")
    runs.finish_mission(conn, mid, status=model.status, conclusion=model.conclusion)
    report_path = report.write_report(conn, mid, runs_dir=runs_dir, finished_at=now)
    payload = {
        "mission_id": mid,
        "status": model.status,
        "conclusion": model.conclusion,
        "report_path": str(report_path),
    }
    inbox.append_event(conn, at=now, kind="mission_finished", ref_id=mid, payload=payload)
    return {"ok": True, "mission_id": mid, "report_path": str(report_path)}


def handle_agent_error(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: AgentErrorPayload,
    now: float,
) -> dict[str, Any]:
    """Emit `agent_error` event when the Agent worker encounters LLM API errors."""
    tid = str(cmd["target_id"]) if cmd["target_id"] else None
    exp_id = model.experiment_id or (tid if tid and not tid.startswith("M-") else None)
    mid = model.mission_id or (tid if tid and tid.startswith("M-") else None)
    if exp_id and not mid:
        exp = runs.get_experiment(conn, exp_id)
        if exp is not None:
            mid = exp["mission_id"]
    ref_id = mid or exp_id or tid
    payload = model.model_dump()
    payload["error"] = model.error
    payload["mission_id"] = mid
    payload["experiment_id"] = exp_id
    payload["role"] = model.role or str(cmd["who"])
    inbox.append_event(conn, at=now, kind="agent_error", ref_id=ref_id, payload=payload)
    return {"ok": True}
