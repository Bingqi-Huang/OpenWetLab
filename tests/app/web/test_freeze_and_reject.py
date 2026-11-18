"""Freeze verdict, protocol rejection, plate fixes, and web write-isolation tests."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from wetlab.app.web import MAIN_PATH, MISSIONS_PAGE
from wetlab.lab.service.main import Service, create_virtual_service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, records, runs

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


def _drain_service(svc: Service, max_ticks: int = 10) -> None:
    for _ in range(max_ticks):
        svc.tick()
        no_cmds = not inbox.list_unhandled_commands(svc.conn)
        no_running = not runs.list_protocols(svc.conn, status="running")
        if no_cmds and no_running:
            break


def test_freeze_verdict_requires_conclusion_and_transfer_plate_position(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "freeze.sqlite"
    runs_dir = tmp_path / "runs"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    monkeypatch.setenv("WETLAB_RUNS_DIR", str(runs_dir))
    conn = open_db(db_file, mode="virtual")
    sim = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            faults=[Fault(device="arm", action="MovMulti", nth=1, kind="no_result")],
        )
    )
    svc = Service(sim, conn, runs_dir=runs_dir, on_labware_change=sim.sync_human_labware)
    svc.startup()
    try:
        with conn:
            inbox.enqueue_command(
                conn,
                key="place:1",
                at=sim.now(),
                who="web",
                kind="place_labware",
                payload={"labware_id": "LW-001", "type": "96孔板", "position": "rack:1-3"},
            )
            inbox.enqueue_command(
                conn,
                key="mis:1",
                at=sim.now(),
                who="web",
                kind="new_mission",
                payload={"mission_id": "M-20261118-001", "title": "搬运测试", "input_text": "搬板后振荡"},
            )
        svc.tick()
        proto = {
            "name": "搬运至振荡并混匀",
            "labware": {"LW-001": "rack:1-3"},
            "steps": [
                {"skill": "transfer", "args": {"labware_id": "LW-001", "to_position": "shaker"}},
                {"skill": "shake", "args": {"seconds": 5, "rpm": 300}},
            ],
        }
        with conn:
            inbox.enqueue_command(
                conn,
                key="sub:1",
                at=sim.now(),
                who="operator",
                kind="submit_protocol",
                payload={"mission_id": "M-20261118-001", "protocol": proto},
            )
        svc.tick()
        sub = runs.list_protocols(conn, status="submitted")[0]
        with conn:
            inbox.enqueue_command(
                conn,
                key="app:1",
                at=sim.now(),
                who="王老师",
                kind="approve",
                payload={
                    "protocol_id": sub["id"],
                    "decided_by": "王老师",
                    "fingerprint": sub["fingerprint"],
                },
            )
        svc.tick()
        svc.tick()
        assert island.get_freeze(conn)["frozen"] is True

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20)
        at.switch_page(str(MISSIONS_PAGE)).run()
        assert not at.exception
        assert "实验岛已冻结，设备不会再动作" in _html(at)
        assert "已冻结" in _html(at)

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert "机械臂将 LW-001 搬运至" in _html(at)
        at.button(key="unfreeze_submit").click().run()
        assert any("请先选择一个结论" in w.value for w in at.warning)
        at.radio(key="unfreeze_conclusion").set_value("step_done")
        at.button(key="unfreeze_submit").click().run()
        assert any("必须先选择这块板现在的实际位置" in w.value for w in at.warning)
        assert inbox.list_unhandled_commands(conn) == []

        sim.world.move_plate("LW-001", "shaker")
        at.radio(key="unfreeze_conclusion").set_value("step_done")
        at.selectbox(key="unfreeze_position").set_value("shaker")
        at.button(key="unfreeze_submit").click().run()
        assert not at.exception
        _drain_service(svc, max_ticks=5)
        assert island.get_freeze(conn)["frozen"] is False
        lw_now = records.get_labware(conn, "LW-001")
        assert lw_now is not None
        assert lw_now["position"] == "shaker"
        proto_now = runs.get_protocol(conn, sub["id"])
        assert proto_now is not None
        assert proto_now["status"] == "done"
        at.run()
        assert "实验岛已冻结" not in _html(at)
    finally:
        conn.close()


def test_send_back_protocol_then_correct_and_remove_plate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "reject.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    svc = create_virtual_service(db_file, runs_dir=tmp_path / "runs")
    svc.startup()
    try:
        sop = json.loads((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="p1",
                at=svc.backend.now(),
                who="web",
                kind="place_labware",
                payload={"labware_id": "LW-001", "type": "96孔板", "position": "pcr"},
            )
            inbox.enqueue_command(
                svc.conn,
                key="m1",
                at=svc.backend.now(),
                who="web",
                kind="new_mission",
                payload={"mission_id": "M-1", "title": "T", "input_text": "I"},
            )
            inbox.enqueue_command(
                svc.conn,
                key="s1",
                at=svc.backend.now(),
                who="operator",
                kind="submit_protocol",
                payload={"mission_id": "M-1", "protocol": sop},
            )
        svc.tick()
        pid = runs.list_protocols(svc.conn, status="submitted")[0]["id"]

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert "待审阅" in _html(at)
        assert f"方案 {pid}" in _html(at)

        at.switch_page(str(MISSIONS_PAGE)).run()
        assert at.radio(key="mission").value == "M-1"
        at.button(key=f"reject_{pid}").click().run()
        assert any("请写下修改意见" in t.value for t in at.toast)
        assert inbox.list_unhandled_commands(svc.conn) == []
        at.text_input(key=f"approver_{pid}").set_value("陈老师")
        at.text_area(key=f"reject_note_{pid}").set_value("请增加一步 4℃ 保温")
        at.button(key=f"reject_{pid}").click().run()
        svc.tick()
        rejected = runs.get_protocol(svc.conn, pid)
        assert rejected is not None
        assert rejected["status"] == "rejected"
        assert rejected["reject_comment"] == "请增加一步 4℃ 保温"
        assert rejected["decided_by"] == "陈老师"
        at.run()
        assert "请增加一步 4℃ 保温" in _html(at)

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert "pcr" not in at.selectbox(key="correct_pos").options
        at.selectbox(key="correct_id").set_value("LW-001")
        at.selectbox(key="correct_pos").set_value("rack:1-2")
        at.button(key="correct_submit").click().run()
        svc.tick()
        lw_high = records.get_labware(svc.conn, "LW-001")
        assert lw_high is not None
        assert lw_high["position"] == "rack:1-2"
        at.run()
        at.selectbox(key="remove_id").set_value("LW-001")
        at.button(key="remove_submit").click().run()
        svc.tick()
        lw_gone = records.get_labware(svc.conn, "LW-001")
        assert lw_gone is not None
        assert lw_gone["position"] is None
    finally:
        svc.conn.close()
