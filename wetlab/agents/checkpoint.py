"""Checkpoint identity binding, path resolution, and turn-outcome helpers for `AgentWorker`."""

from pathlib import Path
import sqlite3
from typing import Any

from langchain_core.messages import AIMessage

from ..lab.store import db as store_db, inbox, island, open_db
from .context import island_clock
from .handoff import WakeTarget


def resolve_worker_paths(
    db_path: str | Path | None,
    mode: str | None,
    runs_dir: str | Path | None,
) -> tuple[Path, Path, str | None]:
    """Resolve `(db_path, runs_dir, open_mode)` for `AgentWorker` (`04` section 5.13)."""
    open_mode = (
        store_db.resolve_mode(mode) if (mode is not None or db_path is None) else None
    )
    if runs_dir is not None:
        resolved_runs = Path(runs_dir)
    elif db_path is not None:
        resolved_runs = Path(db_path).parent
    else:
        resolved_runs = store_db.island_dir(open_mode)

    if db_path is not None:
        resolved_db = Path(db_path)
    elif runs_dir is not None:
        resolved_db = resolved_runs / "wetlab.sqlite"
    else:
        resolved_db = store_db.default_db_path(open_mode)

    return resolved_db, resolved_runs, open_mode


def bind_agents_db_identity(
    ckpt_conn: sqlite3.Connection,
    db_path: Path,
    open_mode: str | None,
) -> None:
    """Verify or record `meta['db_id']` in `agents.sqlite` against `wetlab.sqlite`."""
    ckpt_conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = ckpt_conn.execute("SELECT value FROM meta WHERE key = 'db_id'").fetchone()
    existing_id = str(row[0]) if row is not None else None
    is_file_db = str(db_path) != ":memory:"
    if existing_id is not None and is_file_db and not db_path.exists():
        raise RuntimeError(
            "wetlab.sqlite is missing while agents.sqlite is bound to a previous db_id"
        )
    main_conn = open_db(db_path, mode=open_mode)
    try:
        expected_id = island.get_db_id(main_conn)
    finally:
        main_conn.close()
    if existing_id is not None and existing_id != expected_id:
        raise RuntimeError(f"agents.sqlite db_id mismatch: {existing_id} != {expected_id}")
    if existing_id is None:
        with ckpt_conn:
            ckpt_conn.execute(
                "INSERT INTO meta (key, value) VALUES ('db_id', ?)",
                (expected_id,),
            )


def turn_wrote_required(
    conn: sqlite3.Connection,
    target: WakeTarget,
    event_id: int,
) -> bool:
    """Return True iff the turn command for `(target, event_id)` is in `required_commands`."""
    assert target.required_commands is not None
    key = f"turn:{target.thread_id}:ev{event_id}"
    cmd = inbox.get_command_by_key(conn, key)
    return cmd is not None and cmd["kind"] in target.required_commands


def enqueue_agent_error(
    conn: sqlite3.Connection,
    target: WakeTarget,
    event_id: int,
    error: str,
) -> None:
    """Enqueue an `agent_error` notification command on the first model error of a turn."""
    payload = {
        "role": target.role,
        "thread_id": target.thread_id,
        "mission_id": target.mission_id,
        "experiment_id": target.experiment_id,
        "event_id": event_id,
        "error": error,
    }
    with conn:
        inbox.enqueue_command(
            conn,
            key=f"agent_error:{target.thread_id}:ev{event_id}",
            at=island_clock(conn),
            who="agents",
            kind="agent_error",
            target_id=target.thread_id,
            payload=payload,
        )


def enqueue_autofail(
    conn: sqlite3.Connection,
    target: WakeTarget,
    event_id: int,
    reason: str,
) -> None:
    """Enqueue a failed `finish_experiment` or `finish_mission` command on behalf of an Agent."""
    if target.role == "operator" and target.experiment_id:
        kind = "finish_experiment"
        tid = target.experiment_id
        payload: dict[str, Any] = {"experiment_id": tid, "status": "failed", "notes": reason}
    else:
        kind = "finish_mission"
        tid = target.mission_id
        payload = {"mission_id": tid, "status": "failed", "conclusion": reason}
    with conn:
        inbox.enqueue_command(
            conn,
            key=f"autofail:{target.thread_id}:ev{event_id}",
            at=island_clock(conn),
            who=target.role,
            kind=kind,
            target_id=tid,
            payload=payload,
        )


def extract_turn_reply(messages: list[Any], msg_id: str) -> str:
    """Extract the trailing AI text produced during the turn starting at `msg_id`."""
    idx = next((i for i, m in enumerate(messages) if getattr(m, "id", None) == msg_id), None)
    tail = messages[idx + 1 :] if idx is not None else messages[1:]
    for msg in reversed(tail):
        if not isinstance(msg, AIMessage):
            continue
        content = msg.content
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
            joined = "".join(parts).strip()
            if joined:
                return joined
    return ""


def export_plan_md(mission_id: str, todos: list[dict[str, Any]], runs_dir: Path) -> Path:
    """Export Coordinator's `todos` state to `<runs_dir>/<mission_id>/plan.md`."""
    plan_path = runs_dir / mission_id / "plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    status_mark = {"completed": "[x]", "in_progress": "[-]", "pending": "[ ]"}
    lines = [f"# 任务计划（{mission_id}）", ""]
    if not todos:
        lines.append("- （暂无待办项）")
    else:
        for item in todos:
            mark = status_mark.get(str(item.get("status", "pending")), "[ ]")
            lines.append(f"- {mark} {item.get('content', '')}")
    lines.append("")
    plan_path.write_text("\n".join(lines), encoding="utf-8")
    return plan_path
