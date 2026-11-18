"""E2E: task gives only sample name → coordinator finds plate → 9-step PCR → report."""

from pathlib import Path

from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, records, runs

from .conftest import ScriptedChatModel, ai_tool_call

PCR_STAGES = [{"temperature_c": 95, "hold_seconds": 10}]


def _place_labware(svc, *, lw_id, position, contents=None):
    payload = {
        "labware_id": lw_id,
        "type": "96_plate",
        "position": position,
    }
    if contents:
        payload["contents"] = contents
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=f"ui:place:{lw_id}",
            at=svc.backend.now(),
            who="researcher",
            kind="place_labware",
            payload=payload,
        )
    svc.step()


def test_sample_name_only_pcr_e2e(tmp_path: Path) -> None:
    """Coordinator locates plate by sample name, operator submits 9-step PCR plan."""
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()

    # -- place distractor and target plates --------------------------------
    _place_labware(
        svc,
        lw_id="LW-001",
        position="rack:1-3",
        contents=[{"name": "NegativeControl", "wells": ["A1"]}],
    )
    _place_labware(
        svc,
        lw_id="LW-003",
        position="rack:1-2",
        contents=[
            {"name": "X", "wells": ["A1"], "volume_ul": 25.0, "note": None},
        ],
    )

    # -- verify get_lab_state contains the two plates ----------------------
    from wetlab.lab.snapshot import read_snapshot

    snap = read_snapshot(svc.conn)
    assert "LW-001" in snap["labware"]
    assert "LW-003" in snap["labware"]
    lw003 = snap["labware"]["LW-003"]
    assert any(c["name"] == "X" for c in lw003["contents"])

    # -- nine-step PCR protocol (spec §6.4) --------------------------------
    nine_steps = {
        "name": "PCR for sample X",
        "labware": {"LW-003": "rack:1-2"},
        "steps": [
            {"skill": "pcr_open", "args": {}},
            {
                "skill": "transfer",
                "args": {
                    "labware_id": "LW-003",
                    "to_position": "pcr",
                },
            },
            {"skill": "pcr_close", "args": {}},
            {
                "skill": "pcr_start_program",
                "args": {
                    "stages": PCR_STAGES,
                    "volume_ul": 25,
                    "lid_c": 105,
                },
            },
            {
                "wait_until": {
                    "device": "pcr",
                    "state": {"PcrState": "Idle"},
                    "max_seconds": 600,
                },
            },
            {"skill": "pcr_confirm_finished", "args": {}},
            {"skill": "pcr_open", "args": {}},
            {
                "skill": "transfer",
                "args": {
                    "labware_id": "LW-003",
                    "to_position": "rack:1-2",
                },
            },
            {"skill": "pcr_close", "args": {}},
        ],
    }

    # -- scripted coordinator model ----------------------------------------
    coord_model = ScriptedChatModel(
        [
            # Turn 1 (mission_created): get_lab_state → create_experiment
            ai_tool_call("get_lab_state", {}, call_id="c1"),
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "把样品 X 做 PCR", "status": "in_progress"},
                    ]
                },
                call_id="c2",
            ),
            ai_tool_call(
                "create_experiment",
                {
                    "objective": "对样品 X 执行 PCR 程序",
                    "procedure": (
                        "开门搬入板,关门,启动PCR,"
                        "等Idle,确认完成,开门搬回,关门"
                    ),
                    "labware": {"LW-003": "rack:1-2"},
                    "max_lab_minutes": 30,
                },
                call_id="c3",
            ),
            # Turn 2 (experiment_finished): finish_mission
            ai_tool_call(
                "write_todos",
                {
                    "todos": [
                        {"content": "把样品 X 做 PCR", "status": "completed"},
                    ]
                },
                call_id="c4",
            ),
            ai_tool_call(
                "finish_mission",
                {
                    "conclusion": "样品 X 已完成 PCR，LW-003 归还 rack:1-2。",
                    "status": "completed",
                },
                call_id="c5",
            ),
        ]
    )

    # -- scripted operator model -------------------------------------------
    op_model = ScriptedChatModel(
        [
            # Turn 1 (experiment_created): get_lab_state → rehearse → submit
            ai_tool_call("get_lab_state", {}, call_id="o1"),
            ai_tool_call("rehearse", {"protocol": nine_steps}, call_id="o2"),
            ai_tool_call(
                "submit_protocol", {"protocol": nine_steps}, call_id="o3",
            ),
            # Turn 2 (protocol_done): finish_experiment
            ai_tool_call(
                "finish_experiment",
                {
                    "status": "completed",
                    "notes": "九步全部按预期完成，LW-003 已归还 rack:1-2。",
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
        # -- enqueue mission (task text gives only sample name) ------------
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:mission:1",
                at=svc.backend.now(),
                who="researcher",
                kind="new_mission",
                payload={
                    "mission_id": "M-20261118-001",
                    "title": "样品 X 做 PCR",
                    "input_text": "请对样品 X 执行以下 PCR 程序：95℃ 10 秒。",
                },
            )
        svc.step()

        # coordinator turn 1
        worker.run_until_idle()

        # Verify coordinator called get_lab_state and saw LW-003
        c_calls = coord_model.seen_calls
        assert len(c_calls) >= 1
        first_msgs = c_calls[0]
        tool_result_texts = [
            m.content for m in first_msgs if hasattr(m, "content")
        ]
        combined = " ".join(str(t) for t in tool_result_texts)
        assert "LW-003" in combined
        assert "X" in combined

        # process create_experiment
        svc.step()
        # operator turn 1
        worker.run_until_idle()
        svc.step()

        # -- verify rehearsal and submission ---
        protos = runs.list_protocols(svc.conn, experiment_id="E-001")
        assert len(protos) == 1
        p = protos[0]
        assert p["status"] == "submitted"
        reh = p["rehearsal"]
        assert reh["ok"] is True
        assert len(reh.get("accidents", [])) == 0

        # -- verify rendered text contains sample name X ---
        text_md = p.get("text_md", "")
        assert "X" in text_md, "rendered review text must mention sample X"

        # -- human approves ---
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
                    "fingerprint": p["fingerprint"],
                },
            )

        # -- execute all 9 steps ---
        for _ in range(120):
            svc.step()
            pr = runs.get_protocol(svc.conn, "E-001-v1")
            if pr and pr["status"] == "done":
                break
        assert runs.get_protocol(svc.conn, "E-001-v1")["status"] == "done"

        # operator turn 2 (protocol_done → finish_experiment)
        worker.run_until_idle()
        svc.step()

        # coordinator turn 2 (experiment_finished → finish_mission)
        worker.run_until_idle()
        svc.step()

        # -- assert mission completed ---
        mission = runs.get_mission(svc.conn, "M-20261118-001")
        assert mission is not None and mission["status"] == "completed"

        # -- LW-003 back at rack:1-2 ---
        lw_rows = records.list_labware(svc.conn, active_only=True)
        lw003_row = next(r for r in lw_rows if r["id"] == "LW-003")
        assert lw003_row["position"] == "rack:1-2"

        # -- LW-003 history contains transfer and PCR events ---
        events = records.list_labware_events(svc.conn, "LW-003")
        whats = [e["what"] for e in events]
        assert "moved" in whats, "LW-003 must have been moved"
        assert "pcr_program_started" in whats
        assert "pcr_program_completed" in whats

        ops = records.list_operations(svc.conn, protocol_id="E-001-v1")
        actions = [op["action"] for op in ops]
        assert "RunProgram" in actions, "PCR program must run"

        # -- report.md contains sample name X ---
        report_path = runs_dir / "M-20261118-001" / "report.md"
        assert report_path.is_file()
        report_text = report_path.read_text(encoding="utf-8")
        assert "LW-003" in report_text
        assert "X" in report_text
        assert "## 第一部分" in report_text
        assert "## 第二部分" in report_text

    finally:
        worker.close()
        svc.conn.close()
