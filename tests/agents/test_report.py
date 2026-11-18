"""Tests verifying `wetlab/agents/report.py` is deleted and Service writes `report.md` (#8)."""

from pathlib import Path

import wetlab.agents
from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, records, runs

from .conftest import ScriptedChatModel

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_agents_has_no_report_writer_and_service_writes_mission_scoped_report(
    tmp_path: Path,
) -> None:
    """v0.6 #8: `wetlab/agents/report.py` is deleted; `Service` writes `report.md` on
    `finish_mission` scoped strictly to that mission's operations, labware, and interventions.
    """
    assert not (REPO_ROOT / "wetlab" / "agents" / "report.py").exists()
    assert not hasattr(wetlab.agents, "generate_report")
    assert not hasattr(wetlab.agents, "write_report")

    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    svc.backend.world.plates["LW-OLD"] = "shaker"  # type: ignore[attr-defined]
    svc.backend.world.plates["LW-001"] = "rack:low"  # type: ignore[attr-defined]

    # Mission 1 (`M-OLD`): has `stop_all` and `correct_labware` on `LW-OLD`
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="lw_old",
            at=svc.backend.now(),
            who="researcher",
            kind="place_labware",
            payload={"labware_id": "LW-OLD", "type": "96_plate", "position": "shaker"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="lw_new",
            at=svc.backend.now(),
            who="researcher",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "rack:low"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="m_old",
            at=svc.backend.now(),
            who="researcher",
            kind="new_mission",
            payload={"mission_id": "M-OLD", "title": "旧任务", "input_text": "旧任务"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e_old",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-OLD", "task": {"objective": "旧振荡", "procedure": "振荡"}},
        )
        inbox.enqueue_command(
            svc.conn,
            key="sub_old",
            at=svc.backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-001",
            payload={
                "experiment_id": "E-001",
                "protocol": {
                    "name": "旧长振荡",
                    "labware": {"LW-OLD": "shaker"},
                    "steps": [
                        {"skill": "shake_start", "args": {"rpm": 350}},
                        {"wait": {"seconds": 60}},
                    ],
                },
            },
        )
    svc.step()
    p_old = runs.get_protocol(svc.conn, "E-001-v1")
    assert p_old is not None
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="app_old",
            at=svc.backend.now(),
            who="researcher",
            kind="approve",
            target_id="E-001-v1",
            payload={
                "protocol_id": "E-001-v1",
                "decided_by": "前审批人",
                "fingerprint": p_old["fingerprint"],
            },
        )
    svc.step()
    svc.step()
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="stop_old",
            at=svc.backend.now(),
            who="researcher",
            kind="stop_all",
            payload={},
        )
        inbox.enqueue_command(
            svc.conn,
            key="corr_old",
            at=svc.backend.now(),
            who="researcher",
            kind="correct_labware",
            payload={"labware_id": "LW-OLD", "position": "rack:high"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="fe_old",
            at=svc.backend.now(),
            who="operator",
            kind="finish_experiment",
            target_id="E-001",
            payload={"experiment_id": "E-001", "status": "stopped", "notes": "旧任务实验停止"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="fm_old",
            at=svc.backend.now(),
            who="coordinator",
            kind="finish_mission",
            target_id="M-OLD",
            payload={"mission_id": "M-OLD", "status": "failed", "conclusion": "旧任务结论"},
        )
    svc.step()
    svc.backend.sleep(5.0)

    # Mission 2 (`M-NEW`): runs cleanly on `LW-001` (shaker -> open/close PCR)
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="corr_move_new",
            at=svc.backend.now(),
            who="researcher",
            kind="correct_labware",
            payload={"labware_id": "LW-001", "position": "shaker"},
        )
    svc.step()
    svc.backend.sleep(2.0)

    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="m_new",
            at=svc.backend.now(),
            who="researcher",
            kind="new_mission",
            payload={"mission_id": "M-NEW", "title": "新报告任务", "input_text": "振荡与开关门"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e_new",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={
                "mission_id": "M-NEW",
                "task": {"objective": "振荡", "procedure": "振荡后开关门", "labware": {"LW-001": "shaker"}},
            },
        )
        inbox.enqueue_command(
            svc.conn,
            key="sub_new",
            at=svc.backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-002",
            payload={
                "experiment_id": "E-002",
                "protocol": {
                    "name": "新任务方案",
                    "labware": {"LW-001": "shaker"},
                    "steps": [
                        {"skill": "shake", "args": {"seconds": 8, "rpm": 450}},
                        {"skill": "pcr_open", "args": {}},
                        {"skill": "pcr_close", "args": {}},
                    ],
                },
            },
        )
    svc.step()
    p_new = runs.get_protocol(svc.conn, "E-002-v1")
    assert p_new is not None
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="app_new",
            at=svc.backend.now(),
            who="researcher",
            kind="approve",
            target_id="E-002-v1",
            payload={
                "protocol_id": "E-002-v1",
                "decided_by": "刘研究员",
                "fingerprint": p_new["fingerprint"],
            },
        )
    for _ in range(10):
        svc.step()
        if runs.get_protocol(svc.conn, "E-002-v1")["status"] == "done":  # type: ignore[index]
            break

    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="fe_new",
            at=svc.backend.now(),
            who="operator",
            kind="finish_experiment",
            target_id="E-002",
            payload={"experiment_id": "E-002", "status": "completed", "notes": "操作员核实：新任务全部完成。"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="fm_new",
            at=svc.backend.now(),
            who="coordinator",
            kind="finish_mission",
            target_id="M-NEW",
            payload={"mission_id": "M-NEW", "status": "completed", "conclusion": "协调者结论：新任务圆满达成。"},
        )
    svc.step()

    report_path = runs_dir / "M-NEW" / "report.md"
    assert report_path.is_file()
    before_worker_text = report_path.read_text(encoding="utf-8")

    # Advancing `AgentWorker` past `mission_finished` does NOT touch or rewrite `report.md`!
    head_before_finish = inbox.list_events_after(svc.conn, 0)[-1]["id"] - 1
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", head_before_finish)
    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=ScriptedChatModel([]),
        operator_model=ScriptedChatModel([]),
    )
    try:
        assert worker.step_once() is True
        assert report_path.read_text(encoding="utf-8") == before_worker_text

        part1, part2 = before_worker_text.split("## 第二部分")
        assert "## 第一部分" in part1
        ops_new = records.list_operations(svc.conn, protocol_id="E-002-v1")
        assert len(ops_new) >= 5
        for op in ops_new:
            assert op["id"] in part1

        assert "刘研究员" in part1
        assert "操作员核实：新任务全部完成。" in part2
        assert "协调者结论：新任务圆满达成。" in part2

        # Zero leakage from `M-OLD` (`stop_all`, `LW-OLD`, `E-001`, `前审批人`)
        assert "LW-OLD" not in before_worker_text
        assert "E-001" not in before_worker_text
        assert "前审批人" not in before_worker_text
        assert "stop_all" not in before_worker_text
    finally:
        worker.close()
        svc.conn.close()
