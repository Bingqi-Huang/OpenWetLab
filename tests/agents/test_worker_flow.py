"""End-to-end scripted virtual-island workflow and Service report verification."""

import json
from pathlib import Path

from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, records, runs

from .conftest import ScriptedChatModel, ai_tool_call

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_full_virtual_island_mission_flow_and_report_operations_match(tmp_path: Path) -> None:
    """New mission -> Coordinator creates experiment -> Operator submits protocol ->
    Human approves & Service executes -> Operator finishes experiment ->
    Coordinator finishes mission -> Service writes `report.md` matching `operations`.
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    sop_proto = json.loads((REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8"))

    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()

    coord_model = ScriptedChatModel(
        [
            # Turn 1 (mission_created): inspect -> write_todos -> create_experiment (return_direct)
            ai_tool_call("get_lab_state", {}, call_id="c1"),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "执行 37℃ 30 秒恒温验证", "status": "in_progress"},
                        {"content": "汇总结果并结题", "status": "pending"},
                    ]
                },
                call_id="c2",
            ),
            ai_tool_call(
                "create_experiment",
                {
                    "objective": "PCR 仪内孔板执行 37℃ 保持 30 秒",
                    "procedure": "关门，启动 37℃ 30 秒程序（不加热热盖），等待回到 Idle，确认完成并开门",
                    "labware": {"LW-001": "pcr"},
                    "max_lab_minutes": 30,
                },
                call_id="c3",
            ),
            # Turn 2 (experiment_finished): update todos -> finish_mission (return_direct)
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "执行 37℃ 30 秒恒温验证", "status": "completed"},
                        {"content": "汇总结果并结题", "status": "completed"},
                    ]
                },
                call_id="c4",
            ),
            ai_tool_call(
                "finish_mission",
                {
                    "conclusion": "LW-001 已按要求完成 37℃ 30 秒恒温程序，全流程无偏差。",
                    "status": "completed",
                },
                call_id="c5",
            ),
        ]
    )

    op_model = ScriptedChatModel(
        [
            # Turn 1 (experiment_created): inspect -> rehearse -> submit_protocol (return_direct)
            ai_tool_call("get_lab_state", {}, call_id="o1"),
            ai_tool_call("rehearse", {"protocol": sop_proto}, call_id="o2"),
            ai_tool_call("submit_protocol", {"protocol": sop_proto}, call_id="o3"),
            # Turn 2 (protocol_done): finish_experiment (return_direct)
            ai_tool_call(
                "finish_experiment",
                {
                    "status": "completed",
                    "notes": "五步全部按预期完成，PCR 程序已完成且仓门已打开。",
                },
                call_id="o4",
            ),
        ]
    )

    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=coord_model,
        operator_model=op_model,
    )

    try:
        svc.backend.world.plates["LW-001"] = "pcr"  # type: ignore[attr-defined]
        pcr_sim = svc.backend.world.device("pcr")  # type: ignore[attr-defined]
        pcr_sim.props["LidMotorState"] = "Pressed"
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:place:1",
                at=svc.backend.now(),
                who="researcher",
                kind="place_labware",
                payload={"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            )
            inbox.enqueue_command(
                svc.conn,
                key="ui:mission:1",
                at=svc.backend.now(),
                who="researcher",
                kind="new_mission",
                payload={
                    "mission_id": "M-20261118-001",
                    "title": "37℃ 恒温验证",
                    "input_text": "请对 PCR 仪内的 LW-001 执行 37℃ 保持 30 秒。",
                },
            )
        svc.step()

        worker.run_until_idle()
        plan_md = (runs_dir / "M-20261118-001" / "plan.md").read_text(encoding="utf-8")
        assert "[-] 执行 37℃ 30 秒恒温验证" in plan_md

        svc.step()
        worker.run_until_idle()
        svc.step()

        protos = runs.list_protocols(svc.conn, experiment_id="E-001")
        assert len(protos) == 1
        assert protos[0]["status"] == "submitted"

        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:approve:1",
                at=svc.backend.now(),
                who="researcher",
                kind="approve",
                target_id="E-001-v1",
                payload={
                    "protocol_id": "E-001-v1",
                    "decided_by": "王研究员",
                    "fingerprint": protos[0]["fingerprint"],
                },
            )
        for _ in range(80):
            svc.step()
            if runs.get_protocol(svc.conn, "E-001-v1")["status"] == "done":  # type: ignore[index]
                break
        assert runs.get_protocol(svc.conn, "E-001-v1")["status"] == "done"  # type: ignore[index]

        worker.run_until_idle()
        svc.step()

        worker.run_until_idle()
        # Service writes `report.md` directly when handling `finish_mission` (#8)!
        svc.step()

        mission = runs.get_mission(svc.conn, "M-20261118-001")
        assert mission is not None and mission["status"] == "completed"

        agent_cmds = [
            row["kind"]
            for row in svc.conn.execute(
                "SELECT kind FROM commands WHERE who IN ('coordinator', 'operator') ORDER BY id ASC"
            ).fetchall()
        ]
        assert agent_cmds == [
            "create_experiment",
            "submit_protocol",
            "finish_experiment",
            "finish_mission",
        ]

        report_path = runs_dir / "M-20261118-001" / "report.md"
        assert report_path.is_file()
        report_text = report_path.read_text(encoding="utf-8")
        assert "## 第一部分" in report_text
        assert "## 第二部分" in report_text
        assert "五步全部按预期完成" in report_text
        assert "LW-001 已按要求完成 37℃ 30 秒恒温程序" in report_text

        proto_ops = records.list_operations(svc.conn, protocol_id="E-001-v1")
        assert len(proto_ops) > 0
        for op in proto_ops:
            assert op["id"] in report_text
            assert op["device"] in report_text
            assert op["action"] in report_text
    finally:
        worker.close()
        svc.conn.close()
