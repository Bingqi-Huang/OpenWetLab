"""Event-handling tests for step failure wakeup and protocol rejection feedback."""

from pathlib import Path

from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import Service, create_virtual_service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, open_db, runs

from .conftest import ScriptedChatModel, ai_tool_call


def seed_mission_and_experiment(svc: Service, *, mission_id: str = "M-20261118-010") -> str:
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=f"m:{mission_id}",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": mission_id, "title": "测试任务", "input_text": "测试"},
        )
        inbox.enqueue_command(
            svc.conn,
            key=f"e:{mission_id}",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={
                "mission_id": mission_id,
                "task": {"objective": "测试实验", "procedure": "测试步骤", "labware": {}},
            },
        )
    svc.step()
    exps = runs.list_experiments(svc.conn, mission_id=mission_id)
    return exps[-1]["id"]


def test_step_failure_wakes_operator_with_step_facts_to_submit_revision(tmp_path: Path) -> None:
    """Step 9 row 4 + v0.6 #6: step failure clears `WorkingStatus=2` and wakes Operator."""
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    conn = open_db(db_path)
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "shaker"},
            faults=[
                Fault(
                    device="pcr",
                    action="ListFiles",
                    nth=1,
                    kind="fail",
                    message="读取程序列表失败",
                )
            ],
        )
    )
    svc = Service(backend, conn, runs_dir=runs_dir)
    svc.startup()

    with conn:
        inbox.enqueue_command(
            conn,
            key="lw1",
            at=backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
        )
    exp_id = seed_mission_and_experiment(svc)

    v1_proto = {
        "name": "振荡后列程序并关门",
        "labware": {"LW-001": "shaker"},
        "steps": [
            {"skill": "shake", "args": {"seconds": 5, "rpm": 300}},
            {"skill": "pcr_list_programs", "args": {}},
            {"skill": "pcr_close", "args": {}},
        ],
    }
    revised_v2_proto = {
        "name": "仅剩余列程序与关门步骤",
        "labware": {"LW-001": "shaker"},
        "steps": [
            {"skill": "pcr_list_programs", "args": {}},
            {"skill": "pcr_close", "args": {}},
        ],
    }

    op_model = ScriptedChatModel(
        [
            ai_tool_call("rehearse", {"protocol": v1_proto}, call_id="s1"),
            ai_tool_call("submit_protocol", {"protocol": v1_proto}, call_id="s2"),
            ai_tool_call("rehearse", {"protocol": revised_v2_proto}, call_id="r1"),
            ai_tool_call("submit_protocol", {"protocol": revised_v2_proto}, call_id="r2"),
        ]
    )
    worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, operator_model=op_model)
    try:
        with svc.conn:
            inbox.set_cursor(svc.conn, "agents", 1)
        worker.run_until_idle()
        svc.step()

        p1 = runs.get_protocol(conn, f"{exp_id}-v1")
        assert p1 is not None and p1["status"] == "submitted"
        with conn:
            inbox.enqueue_command(
                conn,
                key="app:v1",
                at=backend.now(),
                who="ui",
                kind="approve",
                target_id=p1["id"],
                payload={
                    "protocol_id": p1["id"],
                    "decided_by": "李老师",
                    "fingerprint": p1["fingerprint"],
                },
            )
        for _ in range(5):
            svc.step()
            if runs.get_protocol(conn, p1["id"])["status"] == "failed":  # type: ignore[index]
                break
        assert runs.get_protocol(conn, p1["id"])["status"] == "failed"  # type: ignore[index]

        steps_v1 = runs.list_steps(conn, p1["id"])
        assert [s["status"] for s in steps_v1] == ["done", "failed", "pending"]

        worker.run_until_idle()
        svc.step()

        prompt_text = str(op_model.seen_calls[2][-1].content)
        assert "方案执行失败" in prompt_text
        p2 = runs.get_protocol(conn, f"{exp_id}-v2")
        assert p2 is not None and p2["status"] == "submitted"
        v2_skills = [s["skill"] for s in p2["content"]["steps"]]
        assert v2_skills == ["pcr_list_programs", "pcr_close"]
    finally:
        worker.close()
        conn.close()


def test_protocol_rejected_delivers_comment_to_operator(tmp_path: Path) -> None:
    """Step 9 row 5: human rejection comment appears in Operator's wakeup message."""
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
    exp_id = seed_mission_and_experiment(svc)

    v1 = {
        "name": "振荡 10 秒",
        "labware": {"LW-001": "shaker"},
        "steps": [{"skill": "shake", "args": {"seconds": 10, "rpm": 300}}],
    }
    v2 = {
        "name": "振荡 20 秒",
        "labware": {"LW-001": "shaker"},
        "steps": [{"skill": "shake", "args": {"seconds": 20, "rpm": 500}}],
    }

    op_model = ScriptedChatModel(
        [
            ai_tool_call("submit_protocol", {"protocol": v1}, call_id="s1"),
            ai_tool_call("rehearse", {"protocol": v2}, call_id="rj1"),
            ai_tool_call("submit_protocol", {"protocol": v2}, call_id="rj2"),
        ]
    )
    worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, operator_model=op_model)
    try:
        with svc.conn:
            inbox.set_cursor(svc.conn, "agents", 1)
        worker.run_until_idle()
        svc.step()

        comment = "请把振荡时长调整为 20 秒，转速提高到 500 rpm"
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ui:rej",
                at=svc.backend.now(),
                who="ui",
                kind="reject",
                target_id=f"{exp_id}-v1",
                payload={"protocol_id": f"{exp_id}-v1", "decided_by": "张博士", "comment": comment},
            )
        svc.step()

        worker.run_until_idle()
        svc.step()

        delivered_msg = str(op_model.seen_calls[1][-1].content)
        assert comment in delivered_msg
        p2 = runs.get_protocol(svc.conn, f"{exp_id}-v2")
        assert p2 is not None and p2["content"]["steps"][0]["args"] == {"seconds": 20, "rpm": 500}
    finally:
        worker.close()
        svc.conn.close()
