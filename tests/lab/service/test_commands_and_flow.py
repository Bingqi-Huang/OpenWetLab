"""Step 6 tests: approval gates, site-change re-rehearsal, normal flow, pause/resume, key dedup."""

import json
from pathlib import Path
import sqlite3

from wetlab.lab.service import Service, create_virtual_service
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.store import inbox, open_db, records, runs

REPO_ROOT = Path(__file__).resolve().parents[3]
SOP_37C = json.loads((REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8"))


def _enqueue(
    svc: Service, kind: str, payload: dict, *, key: str, target_id: str | None = None
) -> bool:
    with svc.conn:
        return inbox.enqueue_command(
            svc.conn,
            key=key,
            at=svc.backend.now(),
            who="test",
            kind=kind,
            target_id=target_id,
            payload=payload,
        )


def _setup_submitted_pcr(tmp_path: Path) -> tuple[Service, str, str]:
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    svc.startup()
    _enqueue(
        svc,
        "place_labware",
        {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
        key="k-lw",
    )
    _enqueue(
        svc,
        "new_mission",
        {"mission_id": "M-20261118-001", "title": "恒温测试", "input_text": "37℃ 30s"},
        key="k-m",
    )
    task = {"objective": "37℃", "procedure": "30s", "labware": {"LW-001": "pcr"}, "constraints": {}}
    _enqueue(
        svc,
        "create_experiment",
        {"mission_id": "M-20261118-001", "task": task},
        key="k-e",
    )
    svc.step()
    _enqueue(
        svc,
        "submit_protocol",
        {"experiment_id": "E-001", "protocol": SOP_37C},
        key="k-sub",
        target_id="E-001",
    )
    svc.step()
    proto = runs.get_protocol(svc.conn, "E-001-v1")
    assert proto is not None and proto["status"] == "submitted"
    return svc, proto["id"], proto["fingerprint"]


def test_unapproved_protocol_never_executes_and_bad_approvals_rejected(tmp_path: Path) -> None:
    """Step 6 rows 1 & 2: Unapproved protocol stays idle; bad fingerprint/decided_by rejected."""
    setup = _setup_submitted_pcr(tmp_path)
    svc = setup[0]
    proto_id = setup[1]
    fp = setup[2]
    try:
        svc.run(max_rounds=5)
        assert runs.get_protocol(svc.conn, proto_id)["status"] == "submitted"  # type: ignore[index]
        assert all(s["status"] == "pending" for s in runs.list_steps(svc.conn, proto_id))
        assert records.list_operations(svc.conn, protocol_id=proto_id) == []

        _enqueue(
            svc,
            "approve",
            {"protocol_id": proto_id, "decided_by": "李老师", "fingerprint": "deadbeef"},
            key="bad-fp",
        )
        svc.step()
        cmd_bad_fp = inbox.get_command_by_key(svc.conn, "bad-fp")
        assert cmd_bad_fp is not None and json.loads(cmd_bad_fp["outcome"])["ok"] is False
        assert runs.get_protocol(svc.conn, proto_id)["status"] == "submitted"  # type: ignore[index]

        _enqueue(
            svc,
            "approve",
            {"protocol_id": proto_id, "decided_by": "   ", "fingerprint": fp},
            key="no-by",
        )
        svc.step()
        cmd_no_by = inbox.get_command_by_key(svc.conn, "no-by")
        assert cmd_no_by is not None and json.loads(cmd_no_by["outcome"])["ok"] is False
        svc.run(max_rounds=3)
        assert runs.get_protocol(svc.conn, proto_id)["status"] == "submitted"  # type: ignore[index]
        assert records.list_operations(svc.conn, protocol_id=proto_id) == []
    finally:
        svc.conn.close()


def test_site_changed_after_submission_fails_rerehearsal_on_approve(tmp_path: Path) -> None:
    """Step 6 row 3: Removing plate after submission rejects on approve re-rehearsal."""
    setup = _setup_submitted_pcr(tmp_path)
    svc = setup[0]
    proto_id = setup[1]
    fp = setup[2]
    try:
        _enqueue(svc, "remove_labware", {"labware_id": "LW-001"}, key="rm-lw")
        svc.step()

        _enqueue(
            svc,
            "approve",
            {"protocol_id": proto_id, "decided_by": "王老师", "fingerprint": fp},
            key="app-1",
        )
        svc.step()

        proto = runs.get_protocol(svc.conn, proto_id)
        assert proto is not None
        assert proto["status"] == "rejected"
        assert proto["reject_comment"] and "批准前重新预演未通过" in proto["reject_comment"]
        assert "LW-001" in proto["reject_comment"]

        rej_events = [
            e for e in inbox.list_events_after(svc.conn, 0) if e["kind"] == "protocol_rejected"
        ]
        assert len(rej_events) == 1
        assert rej_events[0]["ref_id"] == proto_id
        assert "LW-001" in rej_events[0]["payload"]["reason"]
        assert records.list_operations(svc.conn, protocol_id=proto_id) == []
    finally:
        svc.conn.close()


def test_normal_execution_step_transitions_events_and_mission_report(tmp_path: Path) -> None:
    """Step 6 row 4: Verify `pending -> sent -> done`, `waiting -> done`, and `report.md`."""
    db_file = tmp_path / "wetlab.sqlite"
    conn = open_db(db_file)
    sim = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    seen_during_call: list[str] = []
    orig_call = sim.call

    def spy_call(
        device_code: str,
        action: str,
        params: dict,
        timeout_s: float,
        operation_id: str | None = None,
    ) -> dict:
        if action == "CloseDoor":
            with sqlite3.connect(db_file) as second_conn:
                row = second_conn.execute(
                    "SELECT status FROM steps WHERE protocol_id = 'E-001-v1' AND n = 1"
                ).fetchone()
                if row is not None:
                    seen_during_call.append(str(row[0]))
        return orig_call(device_code, action, params, timeout_s, operation_id=operation_id)

    sim.call = spy_call  # type: ignore[method-assign]
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            key="lw",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-20261118-002", "title": "正常流转", "input_text": "测试"},
            key="m",
        )
        _enqueue(
            svc,
            "submit_protocol",
            {"mission_id": "M-20261118-002", "protocol": SOP_37C},
            key="sub",
        )
        svc.step()

        md_file = tmp_path / "runs" / "M-20261118-002" / "E-001-v1.md"
        assert md_file.is_file()
        proto = runs.get_protocol(conn, "E-001-v1")
        assert proto is not None
        _enqueue(
            svc,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "赵博士", "fingerprint": proto["fingerprint"]},
            key="app",
        )

        svc.step()
        assert seen_during_call == ["sent"]
        assert runs.get_step(conn, "E-001-v1", 1)["status"] == "done"  # type: ignore[index]

        svc.step()
        assert runs.get_step(conn, "E-001-v1", 2)["status"] == "done"  # type: ignore[index]

        svc.step()
        step3 = runs.get_step(conn, "E-001-v1", 3)
        assert step3 is not None and step3["status"] == "waiting"

        for _ in range(60):
            svc.step()
            if runs.get_protocol(conn, "E-001-v1")["status"] == "done":  # type: ignore[index]
                break

        assert runs.get_protocol(conn, "E-001-v1")["status"] == "done"  # type: ignore[index]
        assert all(s["status"] == "done" for s in runs.list_steps(conn, "E-001-v1"))

        _enqueue(svc, "ask", {"mission_id": "M-20261118-002", "question": "需要封膜吗？"}, key="a1")
        _enqueue(svc, "message", {"mission_id": "M-20261118-002", "text": "不需要"}, key="msg1")
        _enqueue(svc, "reply", {"mission_id": "M-20261118-002", "text": "收到"}, key="rep1")
        _enqueue(
            svc,
            "finish_experiment",
            {"experiment_id": "E-001", "status": "completed", "notes": "正常完成"},
            key="fe1",
        )
        _enqueue(
            svc,
            "finish_mission",
            {"mission_id": "M-20261118-002", "status": "completed", "conclusion": "恒温验证通过"},
            key="fm1",
        )
        svc.step()

        report_path = tmp_path / "runs" / "M-20261118-002" / "report.md"
        assert report_path.is_file()
        report_md = report_path.read_text(encoding="utf-8")
        assert "正常完成" in report_md and "恒温验证通过" in report_md
        ev_kinds = {e["kind"] for e in inbox.list_events_after(conn, 0)}
        expected = {"question_asked", "message", "reply", "experiment_finished", "mission_finished"}
        assert expected.issubset(ev_kinds)
    finally:
        conn.close()


def test_pause_keeps_pcr_running_without_new_commands_and_resume_completes(tmp_path: Path) -> None:
    """Step 6 row 5: While paused, no new commands are sent; resume finishes the protocol."""
    setup = _setup_submitted_pcr(tmp_path)
    svc = setup[0]
    proto_id = setup[1]
    fp = setup[2]
    try:
        _enqueue(
            svc,
            "approve",
            {"protocol_id": proto_id, "decided_by": "陈老师", "fingerprint": fp},
            key="app",
        )
        svc.run(max_rounds=3)
        assert svc.backend.state()["PCR002"]["PcrState"] == "Running"

        ops_before = len(records.list_operations(svc.conn, protocol_id=proto_id))
        rem_before = svc.backend.state()["PCR002"]["PcrRemainingSeconds"]

        _enqueue(svc, "pause", {"protocol_id": proto_id}, key="pause-1")
        svc.run(max_rounds=65)

        assert runs.get_protocol(svc.conn, proto_id)["status"] == "paused"  # type: ignore[index]
        assert svc.backend.state()["PCR002"]["PcrRemainingSeconds"] < rem_before
        assert svc.backend.state()["PCR002"]["PcrState"] == "Idle"
        assert len(records.list_operations(svc.conn, protocol_id=proto_id)) == ops_before
        assert runs.get_step(svc.conn, proto_id, 4)["status"] == "pending"  # type: ignore[index]

        _enqueue(svc, "resume", {"protocol_id": proto_id}, key="resume-1")
        svc.run(max_rounds=10)
        assert runs.get_protocol(svc.conn, proto_id)["status"] == "done"  # type: ignore[index]
        assert svc.backend.state()["PCR002"]["DoorState"] == "Open"
    finally:
        svc.conn.close()


def test_duplicate_command_key_handled_only_once(tmp_path: Path) -> None:
    """Step 6 row 17: Enqueueing a command with the same `key` twice is stored and handled once."""
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        ins1 = _enqueue(svc, "new_mission", {"title": "第一次", "input_text": "A"}, key="dup-key-1")
        ins2 = _enqueue(svc, "new_mission", {"title": "第二次", "input_text": "B"}, key="dup-key-1")
        assert ins1 is True
        assert ins2 is False

        svc.step()
        missions = runs.list_missions(svc.conn)
        assert len(missions) == 1
        assert missions[0]["title"] == "第一次"
    finally:
        svc.conn.close()
