"""Tests for live researcher `message` reply, `stop_all` Operator wakeup, and `abort` wakeup."""

from pathlib import Path

from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, runs

from .conftest import ScriptedChatModel, ai_tool_call
from .test_worker_events import seed_mission_and_experiment


def test_message_during_execution_and_stop_all_and_abort_wakeups(tmp_path: Path) -> None:
    """Covers Step 9 rows 8 (`message` -> text reply), 9 (`stop_all` -> Operator),
    and 10 (`abort` -> Coordinator finishes mission with valid `'failed'` enum).
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    svc.backend.world.plates["LW-001"] = "shaker"  # type: ignore[attr-defined]
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="lw",
            at=svc.backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
        )
    exp_id = seed_mission_and_experiment(svc, mission_id="M-20261118-020")

    long_shake = {
        "name": "长振荡",
        "labware": {"LW-001": "shaker"},
        "steps": [
            {"skill": "shake_start", "args": {"rpm": 400}},
            {"wait": {"seconds": 120}},
            {"skill": "shake_stop", "args": {}},
        ],
    }
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="sub1",
            at=svc.backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id=exp_id,
            payload={"experiment_id": exp_id, "protocol": long_shake},
        )
    svc.step()
    proto = runs.get_protocol(svc.conn, f"{exp_id}-v1")
    assert proto is not None
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="app1",
            at=svc.backend.now(),
            who="ui",
            kind="approve",
            target_id=proto["id"],
            payload={
                "protocol_id": proto["id"],
                "decided_by": "陈老师",
                "fingerprint": proto["fingerprint"],
            },
        )
    svc.step()
    svc.step()
    assert runs.get_protocol(svc.conn, proto["id"])["status"] == "running"  # type: ignore[index]

    head_id = inbox.list_events_after(svc.conn, 0)[-1]["id"]

    coord_model = ScriptedChatModel(
        [
            # Row 8: Coordinator answers `message` with plain text ONLY
            "实验 E-001 正在以 400 rpm 恒速振荡等待中，一切正常。",
            # Coordinator creates E-002 after E-001 stops so we also test Row 10 (`abort`)
            ai_tool_call(
                "create_experiment",
                {"objective": "第二轮实验", "procedure": "待定", "labware": {"LW-001": "shaker"}},
                call_id="ce2",
            ),
            # Row 10: Coordinator woken after human `abort` on E-002 -> `status="failed"` (#17)
            ai_tool_call(
                "finish_mission",
                {"conclusion": "实验已按研究人员要求人工终止。", "status": "failed"},
                call_id="fm",
            ),
        ]
    )
    op_model = ScriptedChatModel(
        [
            # Row 9: Operator woken after `stop_all` (`protocol_stopped`) -> finishes E-001
            ai_tool_call(
                "finish_experiment",
                {"status": "stopped", "notes": "被人工全部停止。"},
                call_id="fe1",
            ),
            ai_tool_call("submit_protocol", {"protocol": long_shake}, call_id="sub2"),
        ]
    )

    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=coord_model,
        operator_model=op_model,
    )
    try:
        with svc.conn:
            inbox.set_cursor(svc.conn, "agents", head_id)
            inbox.enqueue_command(
                svc.conn,
                key="ui:msg:1",
                at=svc.backend.now(),
                who="ui",
                kind="message",
                target_id="M-20261118-020",
                payload={"mission_id": "M-20261118-020", "text": "现在振荡情况怎么样？"},
            )
        svc.step()
        worker.run_until_idle()
        svc.step()

        m_cur = runs.get_mission(svc.conn, "M-20261118-020")
        assert m_cur is not None and m_cur["status"] == "running"
        assert runs.get_experiment(svc.conn, exp_id)["status"] == "running"  # type: ignore[index]
        p_cur = runs.get_protocol(svc.conn, proto["id"])
        assert p_cur is not None and p_cur["status"] == "running"
        replies = [e for e in inbox.list_events_after(svc.conn, head_id) if e["kind"] == "reply"]
        assert len(replies) == 1
        assert "400 rpm" in replies[0]["payload"]["text"]

        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:stop:1",
                at=svc.backend.now(),
                who="ui",
                kind="stop_all",
                payload={},
            )
        svc.step()
        p_stopped = runs.get_protocol(svc.conn, proto["id"])
        assert p_stopped is not None and p_stopped["status"] == "stopped"

        worker.run_until_idle()
        svc.step()
        assert runs.get_experiment(svc.conn, exp_id)["status"] == "stopped"  # type: ignore[index]

        worker.run_until_idle()
        svc.step()
        worker.run_until_idle()
        svc.step()
        p_e2 = runs.get_protocol(svc.conn, "E-002-v1")
        assert p_e2 is not None
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:app:e2",
                at=svc.backend.now(),
                who="ui",
                kind="approve",
                target_id="E-002-v1",
                payload={
                    "protocol_id": "E-002-v1",
                    "decided_by": "陈老师",
                    "fingerprint": p_e2["fingerprint"],
                },
            )
        svc.step()

        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:abort:e2",
                at=svc.backend.now(),
                who="ui",
                kind="abort",
                target_id="E-002",
                payload={"experiment_id": "E-002", "protocol_id": "E-002-v1"},
            )
        svc.step()
        assert runs.get_experiment(svc.conn, "E-002")["status"] == "stopped"  # type: ignore[index]

        worker.run_until_idle()
        svc.step()
        m_end = runs.get_mission(svc.conn, "M-20261118-020")
        assert m_end is not None and m_end["status"] == "failed"
    finally:
        worker.close()
        svc.conn.close()
