"""Read model for one research mission: stage, protocols, execution steps, and conversation."""

import sqlite3
from typing import Any

from wetlab.lab.protocol import describe_step, labware_paths
from wetlab.lab.store import inbox, runs

from . import labels
from .db import runs_dir
from .island_data import open_question_missions

EXECUTED = ("running", "paused", "done", "failed", "stopped", "aborted")
VIEWS = ("对话", "方案", "执行", "报告")


def list_missions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Newest first, flagged when the person has something to do in it."""
    asking = open_question_missions(conn)
    reviewing = set()
    for proto in runs.list_protocols(conn, status="submitted"):
        exp = runs.get_experiment(conn, proto["experiment_id"])
        if exp:
            reviewing.add(exp["mission_id"])
    out = []
    for m in reversed(runs.list_missions(conn)):
        text, _ = labels.status(labels.MISSION_STATUS, m["status"])
        needs = "待审阅" if m["id"] in reviewing else ("待回答" if m["id"] in asking else "")
        out.append({**m, "status_text": text, "needs": needs})
    return out


def _run_protocol(protocols: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Active protocol, else the latest protocol that was executed."""
    active = [p for p in protocols if p["status"] in ("running", "paused")]
    if active:
        return active[-1]
    executed = [p for p in protocols if p["status"] in EXECUTED]
    return executed[-1] if executed else None


def _stages(
    mission: dict[str, Any],
    protocols: list[dict[str, Any]],
    run: dict[str, Any] | None,
    steps: list[dict[str, Any]],
    report_ready: bool,
) -> list[dict[str, str]]:
    finished = mission["status"] != "running"
    submitted = [p for p in protocols if p["status"] == "submitted"]
    plan = {"name": "方案", "state": "todo", "note": "实验助手制定中"}
    if submitted:
        plan = {"name": "方案", "state": "now", "note": f"{submitted[-1]['id']} 待您审阅"}
    elif run is not None:
        plan = {"name": "方案", "state": "done", "note": f"已批准 {run['id']}"}
    elif finished:
        plan = {"name": "方案", "state": "todo", "note": "未执行任何方案"}
    run_stage = {"name": "执行", "state": "todo", "note": "批准后开始"}
    if run is not None:
        done = sum(1 for s in steps if s["status"] == "done")
        text, _ = labels.status(labels.PROTOCOL_STATUS, run["status"])
        state = "done" if (run["status"] == "done" or finished) else "now"
        run_stage = {"name": "执行", "state": state, "note": f"{text} · {done}/{len(steps)} 步"}
    report = {
        "name": "报告",
        "state": "done" if report_ready else "todo",
        "note": "已生成" if report_ready else "任务结束后生成",
    }
    first = {"name": "提出要求", "state": "done", "note": labels.clock_text(mission["created_at"])}
    return [first, plan, run_stage, report]


def mission_detail(conn: sqlite3.Connection, mission_id: str) -> dict[str, Any]:
    mission = runs.get_mission(conn, mission_id)
    assert mission is not None
    experiments = runs.list_experiments(conn, mission_id=mission_id)
    protocols = [p for e in experiments for p in runs.list_protocols(conn, experiment_id=e["id"])]
    protocols.sort(key=lambda p: (p["submitted_at"], p["version"]))
    run = _run_protocol(protocols)
    steps = runs.list_steps(conn, run["id"]) if run else []
    folder = runs_dir() / mission_id
    report_file = folder / "report.md"
    plan_file = folder / "plan.md"
    asking = mission_id in open_question_missions(conn)
    submitted = [p for p in protocols if p["status"] == "submitted"]
    if asking:
        view = "对话"
    elif submitted:
        view = "方案"
    elif run is not None and mission["status"] == "running":
        view = "执行"
    elif mission["status"] != "running":
        view = "报告"
    else:
        view = "对话"
    return {
        "mission": mission,
        "experiments": experiments,
        "protocols": protocols,
        "submitted": submitted,
        "run": run,
        "steps": steps,
        "asking": asking,
        "stages": _stages(mission, protocols, run, steps, report_file.is_file()),
        "default_view": view,
        "report": report_file.read_text("utf-8") if report_file.is_file() else None,
        "plan": plan_file.read_text("utf-8") if plan_file.is_file() else None,
    }


def step_rows(
    protocol: dict[str, Any],
    steps: list[dict[str, Any]],
    now: float,
) -> list[dict[str, Any]]:
    """Execution steps with status, elapsed time, and remaining wait window."""
    reh_steps = (protocol["rehearsal"] or {}).get("steps", [])
    estimate = {int(s["n"]): float(s["elapsed_s"]) for s in reh_steps}
    rows = []
    for s in steps:
        text, tone = labels.status(labels.STEP_STATUS, s["status"])
        right = ""
        note = ""
        if s["started_at"] is not None and s["finished_at"] is not None:
            right = f"用时 {labels.duration_text(s['finished_at'] - s['started_at'])}"
        elif s["status"] == "waiting" and s["wait_deadline"] is not None:
            right = f"最多再等 {labels.duration_text(s['wait_deadline'] - now)}"
        elif s["status"] == "sent" and s["started_at"] is not None:
            right = f"已进行 {labels.duration_text(now - s['started_at'])}"
        elif s["n"] in estimate:
            est_s = estimate[s["n"]]
            right = "预计不到 1 秒" if est_s < 1 else f"预计 {labels.duration_text(est_s)}"
        error = (s["result"] or {}).get("error") if s["status"] in ("failed", "unknown") else None
        if error:
            note = f"{error.get('code', '')} {error.get('message', '')}".strip()
        rows.append(
            {
                "n": s["n"],
                "text": describe_step(s["content"]),
                "status": s["status"],
                "status_text": text,
                "tone": tone,
                "right": right,
                "note": note,
                "content": s["content"],
                "started_at": s["started_at"],
                "estimate": estimate.get(s["n"], 0.0),
            }
        )
    return rows


def planned_rows(protocol: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps of a protocol that has not run, with the rehearsed time of each."""
    steps = [
        {
            "n": n,
            "content": c,
            "status": "pending",
            "started_at": None,
            "finished_at": None,
            "result": None,
            "wait_deadline": None,
        }
        for n, c in enumerate(protocol["content"]["steps"], start=1)
    ]
    return step_rows(protocol, steps, now=0.0)


def mission_labware(protocols: list[dict[str, Any]]) -> list[str]:
    """Plates any of the mission's protocols uses."""
    seen: dict[str, None] = {}
    for proto in protocols:
        for lw_id in labware_paths(proto["content"]):
            seen[lw_id] = None
    return list(seen)


def remaining_estimate(rows: list[dict[str, Any]], now: float) -> float:
    """Rehearsed time still ahead: remaining of current step plus unstarted steps."""
    left = 0.0
    for r in rows:
        if r["status"] == "pending":
            left += r["estimate"]
        elif r["status"] in ("sent", "waiting") and r["started_at"] is not None:
            left += max(0.0, r["estimate"] - (now - r["started_at"]))
    return left


def _event_line(ev: dict[str, Any]) -> dict[str, Any] | None:
    p = ev["payload"] or {}
    kind = ev["kind"]
    if kind == "mission_created":
        return {"role": "user", "text": p.get("input_text", ""), "tag": "研究要求"}
    if kind == "question_asked":
        return {"role": "assistant", "text": p.get("question", ""), "tag": "提问"}
    if kind == "reply":
        return {"role": "assistant", "text": p.get("text", ""), "tag": ""}
    if kind == "message":
        return {"role": "user", "text": p.get("text", ""), "tag": ""}
    if kind == "experiment_finished" and p.get("notes"):
        status_text, _ = labels.status(labels.EXPERIMENT_STATUS, p.get("status", ""))
        tag = f"实验 {p.get('experiment_id')} {status_text}"
        return {"role": "assistant", "text": p["notes"], "tag": tag}
    obj = (p.get("task") or {}).get("objective", "")
    system = {
        "experiment_created": lambda: f"开始实验 {p.get('experiment_id')}：{obj}",
        "protocol_submitted": lambda: f"方案 {p.get('protocol_id')} 已提交，等待您审阅",
        "protocol_rejected": lambda: f"方案 {p.get('protocol_id')} 已退回：{p.get('reason', '')}",
        "protocol_done": lambda: f"方案 {p.get('protocol_id')} 执行完成",
        "protocol_failed": lambda: f"方案 {p.get('protocol_id')} 第 {p.get('step_n')} 步失败",
        "protocol_stopped": lambda: f"方案 {p.get('protocol_id')} 已被人工停止",
        "stop_failed": lambda: labels.stop_failed_text(list(p.get("failed_devices") or [])),
        "agent_error": lambda: f"实验助手接口异常：{p.get('error', '')}",
        "experiment_finished": lambda: f"实验 {p.get('experiment_id')} 结束",
        "frozen": lambda: f"实验岛冻结：{p.get('reason', '')}",
        "mission_finished": lambda: f"任务结束：{p.get('conclusion', '')}",
    }
    return {"role": "system", "text": system[kind]()} if kind in system else None


def conversation(conn: sqlite3.Connection, detail: dict[str, Any]) -> list[dict[str, Any]]:
    """The mission's story in time order: people, the assistant, and system milestones."""
    mid = detail["mission"]["id"]
    exp_ids = {e["id"] for e in detail["experiments"]}
    proto_ids = {p["id"] for p in detail["protocols"]}
    owned_refs = exp_ids | proto_ids | {mid}
    items = []
    for ev in inbox.list_events_after(conn, 0):
        p = ev["payload"] or {}
        in_mission = (
            p.get("mission_id") == mid
            or ev["ref_id"] in owned_refs
            or p.get("experiment_id") in exp_ids
            or p.get("protocol_id") in proto_ids
        )
        if not in_mission:
            continue
        line = _event_line(ev)
        if line is not None:
            items.append({**line, "at": ev["at"]})
    for proto in detail["protocols"]:
        if proto["status"] in EXECUTED and proto["decided_at"] is not None:
            items.append(
                {
                    "role": "system",
                    "at": proto["decided_at"],
                    "text": f"{proto['decided_by']} 批准了方案 {proto['id']}，开始执行",
                }
            )
    items.sort(key=lambda it: it["at"])
    return items
