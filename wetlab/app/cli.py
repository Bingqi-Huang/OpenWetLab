"""Developer command-line interface (docs/spec/04 section 5.13 & section 8)."""

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any
import uuid

from wetlab.agents.worker import run_worker
from wetlab.app.cli_parser import build_cli_parser
from wetlab.app.doctor import clock_now, run_doctor
from wetlab.lab import registry
from wetlab.lab.protocol import fingerprint, rehearse, render
from wetlab.lab.service.main import run_daemon
from wetlab.lab.snapshot import read_snapshot
from wetlab.lab.store import db as store_db, inbox, island, open_db, runs


def _mode(args: argparse.Namespace) -> str:
    return store_db.resolve_mode(getattr(args, "mode", None))


def _explicit_db(args: argparse.Namespace) -> str | None:
    env_db = os.environ.get("WETLAB_DB_PATH") or os.environ.get("WETLAB_DB")
    return getattr(args, "db", None) or env_db


def _explicit_runs_dir(args: argparse.Namespace) -> str | None:
    return getattr(args, "runs_dir", None) or os.environ.get("WETLAB_RUNS_DIR")


def _db_file(args: argparse.Namespace, mode: str) -> Path:
    db_override = _explicit_db(args)
    if db_override:
        return Path(db_override)
    runs_override = _explicit_runs_dir(args)
    if runs_override:
        return store_db.default_db_path(mode, base_dir=runs_override)
    return store_db.default_db_path(mode)


def _runs_folder(args: argparse.Namespace, mode: str) -> Path:
    runs_override = _explicit_runs_dir(args)
    db_override = _explicit_db(args)
    if runs_override and db_override:
        return Path(runs_override)
    if runs_override:
        return store_db.island_dir(mode, base_dir=runs_override)
    if db_override:
        return Path(db_override).parent
    return store_db.island_dir(mode)


@contextmanager
def _db(args: argparse.Namespace) -> Iterator[sqlite3.Connection]:
    mode = _mode(args)
    conn = open_db(_db_file(args, mode), mode=mode)
    try:
        yield conn
    finally:
        conn.close()


def _enqueue(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    key: str | None = None,
    target_id: str | None = None,
) -> tuple[str, bool]:
    cmd_key = key or f"cli:{kind}:{uuid.uuid4().hex[:12]}"
    with conn:
        ins = inbox.enqueue_command(
            conn, key=cmd_key, at=clock_now(conn), who="cli", kind=kind,
            payload=payload, target_id=target_id,
        )
    return cmd_key, ins


def _enqueue_and_print(
    args: argparse.Namespace, kind: str, payload: dict[str, Any], *, target_id: str | None = None,
) -> int:
    with _db(args) as conn:
        cmd_key, inserted = _enqueue(conn, kind, payload, key=args.key, target_id=target_id)
    keep = ("protocol_id", "experiment_id", "labware_id")
    extra = {k: v for k, v in payload.items() if k in keep and v}
    print(json.dumps({"enqueued": inserted, "key": cmd_key, **extra}, ensure_ascii=False))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    mode = _mode(args)
    db_path = _db_file(args, mode)
    runs_dir = _runs_folder(args, mode)
    max_rounds = 1 if args.once else args.rounds
    run_daemon(mode=mode, db_path=db_path, runs_dir=runs_dir, max_rounds=max_rounds)
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    mode = _mode(args)
    db_path = _db_file(args, mode)
    runs_dir = _runs_folder(args, mode)
    run_worker(
        mode=mode,
        db_path=db_path,
        runs_dir=runs_dir,
        once=args.once,
        poll_interval_s=args.poll_interval,
    )
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    proto = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if args.experiment_id:
        payload = {"experiment_id": args.experiment_id, "protocol": proto}
        return _enqueue_and_print(args, "submit_protocol", payload, target_id=args.experiment_id)
    mid = args.mission_id or f"M-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:4]}"
    title = args.title or proto.get("name", "CLI 提交任务")
    m_payload = {"mission_id": mid, "title": title, "input_text": args.file}
    m_key = f"{args.key}:mission" if args.key else None
    p_key = f"{args.key}:protocol" if args.key else None
    with _db(args) as conn:
        _enqueue(conn, "new_mission", m_payload, key=m_key, target_id=mid)
        p_payload = {"mission_id": mid, "protocol": proto}
        _, inserted = _enqueue(conn, "submit_protocol", p_payload, key=p_key, target_id=mid)
    out = {"enqueued": inserted, "mission_id": mid, "fingerprint": fingerprint(proto)}
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    proto = json.loads(Path(args.file).read_text(encoding="utf-8"))
    with _db(args) as conn:
        res = rehearse(conn, proto)
    print(render(proto, res), end="")
    return 0 if res["ok"] else 1


def cmd_approve(args: argparse.Namespace) -> int:
    payload = {
        "protocol_id": args.protocol_id,
        "decided_by": args.by,
        "fingerprint": args.fingerprint,
    }
    return _enqueue_and_print(args, "approve", payload, target_id=args.protocol_id)


def cmd_reject(args: argparse.Namespace) -> int:
    payload = {
        "protocol_id": args.protocol_id,
        "decided_by": args.by,
        "comment": args.comment,
    }
    return _enqueue_and_print(args, "reject", payload, target_id=args.protocol_id)


def cmd_pause(args: argparse.Namespace) -> int:
    payload = {"protocol_id": args.protocol_id}
    return _enqueue_and_print(args, "pause", payload, target_id=args.protocol_id)


def cmd_resume(args: argparse.Namespace) -> int:
    payload = {"protocol_id": args.protocol_id}
    return _enqueue_and_print(args, "resume", payload, target_id=args.protocol_id)


def cmd_stop(args: argparse.Namespace) -> int:
    return _enqueue_and_print(args, "stop_all", {})


def cmd_unfreeze(args: argparse.Namespace) -> int:
    payload: dict[str, Any] = {"conclusion": args.conclusion}
    if args.labware_id:
        payload["labware_id"] = args.labware_id
    if args.position is not None:
        payload["labware_position"] = args.position
    return _enqueue_and_print(args, "unfreeze", payload)


def cmd_abort(args: argparse.Namespace) -> int:
    payload = {"experiment_id": args.experiment_id, "protocol_id": args.protocol_id}
    target = args.experiment_id or args.protocol_id
    return _enqueue_and_print(args, "abort", payload, target_id=target)


def cmd_labware(args: argparse.Namespace) -> int:
    if args.lw_action == "place":
        kind = "place_labware"
        payload: dict[str, Any] = {
            "labware_id": args.labware_id,
            "type": args.type,
            "position": args.position,
        }
    elif args.lw_action == "remove":
        kind = "remove_labware"
        payload = {"labware_id": args.labware_id}
    else:
        kind = "correct_labware"
        pos = None if args.position == "removed" else args.position
        payload = {"labware_id": args.labware_id, "position": pos}
    return _enqueue_and_print(args, kind, payload, target_id=args.labware_id)


def cmd_status(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        mode = island.get_island_mode(conn)
        snap = read_snapshot(conn)
        missions = runs.list_missions(conn)
        protocols = runs.list_protocols(conn)
    if args.json:
        body = {"mode": mode, "snapshot": snap, "missions": missions, "protocols": protocols}
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    frz = snap["freeze"]
    reason_suffix = f" ({frz['reason']})" if frz["frozen"] else ""
    gw_online = snap["gateway"]["online"]
    print(f"实验岛模式: {mode} | 网关在线: {gw_online} | 冻结: {frz['frozen']}{reason_suffix}")
    print(f"台面器皿: {snap['labware'] or '无'}")
    for proto in protocols[-5:]:
        print(f"方案 {proto['id']}: status={proto['status']} fp={proto['fingerprint'][:12]}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    with _db(args) as conn:
        now = time.time() if args.realtime else None
        report = run_doctor(conn, now=now)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def cmd_tools_export(args: argparse.Namespace) -> int:
    _mode(args)
    text = json.dumps(registry.catalog(), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    return build_cli_parser(
        {
            "serve": cmd_serve,
            "agents": cmd_agents,
            "submit": cmd_submit,
            "rehearse": cmd_rehearse,
            "approve": cmd_approve,
            "reject": cmd_reject,
            "pause": cmd_pause,
            "resume": cmd_resume,
            "stop": cmd_stop,
            "unfreeze": cmd_unfreeze,
            "abort": cmd_abort,
            "labware": cmd_labware,
            "status": cmd_status,
            "doctor": cmd_doctor,
            "tools": cmd_tools_export,
        }
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code = int(args.func(args))
    if argv is None:
        sys.exit(code)
    return code


if __name__ == "__main__":
    main()
