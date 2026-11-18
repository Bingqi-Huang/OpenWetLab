"""v0.6 Section 16 tests: #1 frozen `stop_all` & `stop_failed`, #2 `closed_skills` enforcement."""

import json
from pathlib import Path

from wetlab.lab.service import Service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, records, runs

REPO_ROOT = Path(__file__).resolve().parents[3]
SOP_37C = json.loads((REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8"))


def _enqueue(
    svc: Service,
    kind: str,
    payload: dict,
    *,
    key: str,
    who: str = "researcher",
    target_id: str | None = None,
) -> None:
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=key,
            at=svc.backend.now(),
            who=who,
            kind=kind,
            target_id=target_id,
            payload=payload,
        )


def test_stop_all_works_when_frozen_and_reports_stop_failed_honestly(tmp_path: Path) -> None:
    """§16.1 #1: (A) `stop_all` while frozen stops PCR & turns off lid; (B) if a stop skill fails,
    command outcome is `ok=False` naming the device, `stop_failed` event is emitted, and protocol
    is still marked `stopped`.
    """
    conn_a = open_db(tmp_path / "a.sqlite", mode="virtual")
    sim_a = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    svc_a = Service(sim_a, conn_a, runs_dir=tmp_path / "runs_a")
    try:
        svc_a.startup()
        _enqueue(
            svc_a,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            key="lw-a",
        )
        _enqueue(svc_a, "new_mission", {"mission_id": "M-001", "title": "冻结停止"}, key="m-a")
        proto_a = {
            "name": "高温运行",
            "labware": {"LW-001": "pcr"},
            "steps": [
                {"skill": "pcr_close", "args": {}},
                {
                    "skill": "pcr_start_program",
                    "args": {
                        "stages": [{"temperature_c": 95, "hold_seconds": 300}],
                        "volume_ul": 50,
                        "lid_c": 105,
                    },
                },
                {"wait": {"seconds": 300}},
            ],
        }
        _enqueue(svc_a, "submit_protocol", {"mission_id": "M-001", "protocol": proto_a}, key="s-a")
        svc_a.step()
        p_a = runs.get_protocol(conn_a, "E-001-v1")
        assert p_a is not None
        _enqueue(
            svc_a,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "王老师", "fingerprint": p_a["fingerprint"]},
            key="app-a",
        )
        svc_a.run(max_rounds=15)
        assert sim_a.state()["PCR002"]["PcrState"] == "Running"
        assert sim_a.state()["PCR002"]["HeaterLidEnabled"] is True

        with conn_a:
            island.set_frozen(conn_a, reason="测试冻结", at=sim_a.now())
        _enqueue(svc_a, "stop_all", {}, key="stop-frozen")
        svc_a.step()

        cmd_a = inbox.get_command_by_key(conn_a, "stop-frozen")
        assert cmd_a is not None
        out_a = json.loads(cmd_a["outcome"])
        assert out_a["ok"] is True
        assert sim_a.state()["PCR002"]["PcrState"] == "Idle"
        assert sim_a.state()["PCR002"]["HeaterLidEnabled"] is False
        assert runs.get_protocol(conn_a, "E-001-v1")["status"] == "stopped"  # type: ignore[index]
    finally:
        conn_a.close()

    conn_b = open_db(tmp_path / "b.sqlite", mode="virtual")
    fault = Fault(device="shaker", action="StopShake", at_call=1, kind="fail", message="制动卡死")
    sim_b = SimBackend(Scenario(labware={"LW-001": "shaker"}, faults=[fault]))
    svc_b = Service(sim_b, conn_b, runs_dir=tmp_path / "runs_b")
    try:
        svc_b.startup()
        _enqueue(
            svc_b,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
            key="lw-b",
        )
        _enqueue(svc_b, "new_mission", {"mission_id": "M-002", "title": "停止失败"}, key="m-b")
        proto_b = {
            "name": "振荡运行",
            "labware": {"LW-001": "shaker"},
            "steps": [
                {"skill": "shake_start", "args": {"rpm": 600}},
                {"wait": {"seconds": 300}},
            ],
        }
        _enqueue(svc_b, "submit_protocol", {"mission_id": "M-002", "protocol": proto_b}, key="s-b")
        svc_b.step()
        p_b = runs.get_protocol(conn_b, "E-001-v1")
        assert p_b is not None
        _enqueue(
            svc_b,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "王老师", "fingerprint": p_b["fingerprint"]},
            key="app-b",
        )
        svc_b.run(max_rounds=2)
        assert sim_b.state()["T002001"]["ShakeState"] == 0

        _enqueue(svc_b, "stop_all", {}, key="stop-fail")
        svc_b.step()

        cmd_b = inbox.get_command_by_key(conn_b, "stop-fail")
        assert cmd_b is not None
        out_b = json.loads(cmd_b["outcome"])
        assert out_b["ok"] is False
        assert out_b["failed_devices"] == ["shaker"]
        assert "shaker" in out_b["error"]
        assert runs.get_protocol(conn_b, "E-001-v1")["status"] == "stopped"  # type: ignore[index]
        stop_ev = [e for e in inbox.list_events_after(conn_b, 0) if e["kind"] == "stop_failed"]
        assert len(stop_ev) == 1
        assert stop_ev[0]["payload"]["failed_devices"] == ["shaker"]
        assert stop_ev[0]["payload"]["protocol_ids"] == ["E-001-v1"]
    finally:
        conn_b.close()


def test_closed_skills_block_at_submit_and_on_approve_when_closed_later(tmp_path: Path) -> None:
    """§16.1 #2: Closed skill rejected at `submit_protocol` with zero ACT ops; if open at submit
    and added to `closed_skills` before `approve` (including across restart), `approve` rejects.
    """
    conn1 = open_db(tmp_path / "closed1.sqlite", mode="virtual")
    sim1 = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    sim1.closed_skills = {"pcr_start_program"}
    svc1 = Service(sim1, conn1, runs_dir=tmp_path / "runs1")
    try:
        svc1.startup()
        assert island.get_closed_skills(conn1) == {"pcr_start_program"}
        ops_after_startup = len(records.list_operations(conn1))
        _enqueue(
            svc1,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            key="lw1",
        )
        _enqueue(svc1, "new_mission", {"mission_id": "M-010", "title": "未开放测试"}, key="m1")
        _enqueue(svc1, "submit_protocol", {"mission_id": "M-010", "protocol": SOP_37C}, key="sub1")
        svc1.step()

        proto1 = runs.get_protocol(conn1, "E-001-v1")
        assert proto1 is not None
        assert proto1["status"] == "rejected"
        assert "pcr_start_program" in (proto1["reject_comment"] or "")
        assert len(records.list_operations(conn1)) == ops_after_startup
    finally:
        conn1.close()

    db2 = tmp_path / "closed2.sqlite"
    conn2a = open_db(db2, mode="virtual")
    sim2a = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    svc2a = Service(sim2a, conn2a, runs_dir=tmp_path / "runs2")
    svc2a.startup()
    _enqueue(
        svc2a,
        "place_labware",
        {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
        key="lw2",
    )
    _enqueue(svc2a, "new_mission", {"mission_id": "M-011", "title": "批准前关闭"}, key="m2")
    _enqueue(svc2a, "submit_protocol", {"mission_id": "M-011", "protocol": SOP_37C}, key="sub2")
    svc2a.step()
    proto2 = runs.get_protocol(conn2a, "E-001-v1")
    assert proto2 is not None and proto2["status"] == "submitted"
    saved_fp = proto2["fingerprint"]
    conn2a.close()

    conn2b = open_db(db2, mode="virtual")
    sim2b = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    sim2b.closed_skills = {"pcr_start_program"}
    svc2b = Service(sim2b, conn2b, runs_dir=tmp_path / "runs2")
    try:
        svc2b.startup()
        assert island.get_closed_skills(conn2b) == {"pcr_start_program"}
        _enqueue(
            svc2b,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "李老师", "fingerprint": saved_fp},
            key="app2",
        )
        svc2b.step()

        proto2_after = runs.get_protocol(conn2b, "E-001-v1")
        assert proto2_after is not None and proto2_after["status"] == "rejected"
        assert "pcr_start_program" in (proto2_after["reject_comment"] or "")
        assert records.list_operations(conn2b, protocol_id="E-001-v1") == []
    finally:
        conn2b.close()
