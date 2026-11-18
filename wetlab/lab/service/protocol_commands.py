"""Protocol submission, 5-version limit, approval, and rejection command handlers."""

from pathlib import Path
import sqlite3
from typing import Any

from pydantic import ValidationError

from ..devices import positions_hash
from ..protocol import coerce_protocol, fingerprint, rehearse, render
from ..store import inbox, runs
from . import advance
from .mission_commands import next_experiment_id
from .schemas import ApprovePayload, CommandError, RejectPayload, SubmitProtocolPayload

MAX_PROTOCOL_VERSIONS = 5
MAX_VERSIONS_NOTE = "方案修改次数超过上限"


def resolve_protocol(
    conn: sqlite3.Connection,
    explicit_id: str | None,
    target_id: str | None,
    *,
    expected_status: str | None = None,
) -> dict[str, Any] | None:
    """Resolve target protocol by explicit ID, `target_id`, or latest matching `expected_status`."""
    pid = explicit_id or target_id
    if pid:
        return runs.get_protocol(conn, str(pid))
    matches = runs.list_protocols(conn, status=expected_status) if expected_status else []
    return matches[-1] if matches else None


def handle_submit_protocol(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: SubmitProtocolPayload,
    now: float,
    runs_dir: Path,
) -> dict[str, Any]:
    """Validate/rehearse a submitted protocol, enforcing the 5-version experiment cap (#21)."""
    raw_proto = model.protocol
    tid = cmd["target_id"]
    exp_id = model.experiment_id
    if not exp_id and tid and not str(tid).startswith("M-"):
        exp_id = str(tid)

    if not exp_id and model.mission_id:
        mid = model.mission_id
        if runs.get_mission(conn, mid) is None:
            raise CommandError(f"unknown mission: {mid}")
        existing_exps = runs.list_experiments(conn, mission_id=mid)
        if existing_exps:
            exp_id = existing_exps[-1]["id"]
        else:
            exp_id = next_experiment_id(conn)
            raw_lw = raw_proto.get("labware")
            lw_dict = dict(raw_lw) if isinstance(raw_lw, dict) else {}
            task = {
                "objective": str(raw_proto.get("name") or "手动方案"),
                "procedure": "",
                "labware": lw_dict,
                "constraints": {},
            }
            runs.insert_experiment(
                conn,
                id=exp_id,
                mission_id=mid,
                n=1,
                task=task,
                status="running",
            )

    exp = runs.get_experiment(conn, str(exp_id or ""))
    if exp is None:
        raise CommandError(f"unknown experiment: {exp_id}")
    if exp["status"] in ("completed", "failed", "stopped"):
        raise CommandError(f"experiment {exp['id']} is already {exp['status']}")

    version = runs.next_protocol_version(conn, exp["id"])
    if version > MAX_PROTOCOL_VERSIONS:
        runs.finish_experiment(conn, exp["id"], status="failed", notes=MAX_VERSIONS_NOTE)
        fin_payload = {
            "experiment_id": exp["id"],
            "mission_id": exp["mission_id"],
            "status": "failed",
            "notes": MAX_VERSIONS_NOTE,
        }
        inbox.append_event(
            conn,
            at=now,
            kind="experiment_finished",
            ref_id=exp["id"],
            payload=fin_payload,
        )
        return {
            "ok": False,
            "experiment_id": exp["id"],
            "status": "failed",
            "error": MAX_VERSIONS_NOTE,
        }

    proto_id = f"{exp['id']}-v{version}"
    rehearsal_res = rehearse(conn, raw_proto)
    try:
        proto = coerce_protocol(raw_proto)
        fp = fingerprint(proto)
        text_md = render(proto, rehearsal_res, conn=conn)
        content_dict = proto.model_dump(mode="json")
    except ValidationError:
        proto = None
        fp = ""
        content_dict = raw_proto
        fail_info = rehearsal_res.get("failure") or {}
        fail_reason = fail_info.get("reason", "方案格式不合法")
        text_md = f"# 方案校验未通过\n\n- {fail_reason}\n"

    md_path = runs_dir / exp["mission_id"] / f"{proto_id}.md"

    if rehearsal_res["ok"] and proto is not None:
        runs.insert_protocol(
            conn,
            id=proto_id,
            experiment_id=exp["id"],
            version=version,
            content=content_dict,
            fingerprint=fp,
            status="submitted",
            submitted_at=now,
            rehearsal=rehearsal_res,
            text_md=text_md,
        )
        step_rows = [
            {"n": i, "kind": s.kind, "content": s.to_step_dict(), "status": "pending"}
            for i, s in enumerate(proto.steps, start=1)
        ]
        runs.insert_steps(conn, proto_id, step_rows)
        sub_payload = {
            "protocol_id": proto_id,
            "experiment_id": exp["id"],
            "mission_id": exp["mission_id"],
            "fingerprint": fp,
        }
        inbox.append_event(
            conn,
            at=now,
            kind="protocol_submitted",
            ref_id=proto_id,
            payload=sub_payload,
        )
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(text_md, encoding="utf-8")
        return {"ok": True, "protocol_id": proto_id, "fingerprint": fp, "status": "submitted"}

    fail = rehearsal_res.get("failure") or {}
    if fail.get("step_n"):
        reason = f"第 {fail['step_n']} 步：{fail.get('reason')}"
    else:
        reason = str(fail.get("reason", "预演未通过"))
    runs.insert_protocol(
        conn,
        id=proto_id,
        experiment_id=exp["id"],
        version=version,
        content=content_dict,
        fingerprint=fp,
        status="rejected",
        submitted_at=now,
        rehearsal=rehearsal_res,
        text_md=text_md,
        decided_at=now,
        reject_comment=reason,
    )
    rej_payload = {
        "protocol_id": proto_id,
        "experiment_id": exp["id"],
        "mission_id": exp["mission_id"],
        "reason": reason,
        "rehearsal": rehearsal_res,
    }
    inbox.append_event(conn, at=now, kind="protocol_rejected", ref_id=proto_id, payload=rej_payload)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(text_md, encoding="utf-8")
    return {"ok": False, "protocol_id": proto_id, "status": "rejected", "error": reason}


def handle_approve(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: ApprovePayload,
    now: float,
) -> dict[str, Any]:
    """Verify fingerprint and re-rehearse against current site state before starting protocol."""
    proto = resolve_protocol(
        conn,
        model.protocol_id,
        cmd["target_id"],
        expected_status="submitted",
    )
    if proto is None or proto["status"] != "submitted":
        raise CommandError("protocol is not in submitted status")
    if model.fingerprint != proto["fingerprint"]:
        raise CommandError("fingerprint mismatch")
    if runs.list_protocols(conn, status="running") or runs.list_protocols(conn, status="paused"):
        raise CommandError("another protocol is already active")

    rehearsal_res = rehearse(conn, proto["content"])
    exp = runs.get_experiment(conn, proto["experiment_id"])
    if not rehearsal_res["ok"]:
        fail = rehearsal_res.get("failure") or {}
        reason = f"批准前重新预演未通过：{fail.get('reason', '未知原因')}"
        runs.decide_protocol(
            conn,
            proto["id"],
            status="rejected",
            decided_by=model.decided_by,
            decided_at=now,
            reject_comment=reason,
            rehearsal=rehearsal_res,
        )
        rej_payload = {
            "protocol_id": proto["id"],
            "experiment_id": proto["experiment_id"],
            "mission_id": exp["mission_id"] if exp else None,
            "reason": reason,
            "rehearsal": rehearsal_res,
        }
        inbox.append_event(
            conn,
            at=now,
            kind="protocol_rejected",
            ref_id=proto["id"],
            payload=rej_payload,
        )
        return {"ok": False, "protocol_id": proto["id"], "status": "rejected", "error": reason}

    advance.start_approved_protocol(
        conn,
        proto["id"],
        decided_by=model.decided_by,
        decided_at=now,
        rehearsal=rehearsal_res,
        positions_hash=positions_hash(),
    )
    return {"ok": True, "protocol_id": proto["id"], "status": "running"}


def handle_reject(
    conn: sqlite3.Connection,
    cmd: dict[str, Any],
    model: RejectPayload,
    now: float,
) -> dict[str, Any]:
    """Reject a submitted protocol with human feedback comment."""
    proto = resolve_protocol(
        conn,
        model.protocol_id,
        cmd["target_id"],
        expected_status="submitted",
    )
    if proto is None or proto["status"] != "submitted":
        raise CommandError("reject requires a submitted protocol")
    exp = runs.get_experiment(conn, proto["experiment_id"])
    runs.decide_protocol(
        conn,
        proto["id"],
        status="rejected",
        decided_by=model.decided_by,
        decided_at=now,
        reject_comment=model.comment,
    )
    payload = {
        "protocol_id": proto["id"],
        "experiment_id": proto["experiment_id"],
        "mission_id": exp["mission_id"] if exp else None,
        "comment": model.comment,
        "reason": model.comment,
    }
    inbox.append_event(conn, at=now, kind="protocol_rejected", ref_id=proto["id"], payload=payload)
    return {"ok": True, "protocol_id": proto["id"], "status": "rejected"}
