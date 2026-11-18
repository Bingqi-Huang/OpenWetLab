"""Tests for unanswered question rule (#7) and `command_failed` wakeup (`04` section 6.4)."""

from pathlib import Path

from wetlab.agents.handoff import (
    COORDINATOR_TERMINAL_COMMANDS,
    OPERATOR_SUBMIT_OR_FINISH,
    classify_event,
)
from wetlab.agents.worker import AUTOFAIL_REASON, AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, runs

from .conftest import ScriptedChatModel, ai_tool_call


def test_unanswered_question_requires_command_while_regular_message_allows_text_only(
    tmp_path: Path,
) -> None:
    """v0.6 #7:
    1) Coordinator asks question -> human replies -> Coordinator only replies text:
       reminded once, still only text -> mission marked `failed`.
    2) No unanswered question -> human sends `message` -> text-only reply is allowed.
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()

    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="m1",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-Q1", "title": "提问必回命令", "input_text": "请确认样本量"},
        )
    svc.step()

    coord_model = ScriptedChatModel(
        [
            ai_tool_call("ask_researcher", {"question": "请问孔内体积是多少微升？"}, call_id="ask1"),
            "收到，50 微升。",
            "还是只回复文字不发命令。",
        ]
    )
    worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, coordinator_model=coord_model)
    try:
        worker.run_until_idle()
        svc.step()
        all_evs = inbox.list_events_after(svc.conn, 0)
        q_events = [e for e in all_evs if e["kind"] == "question_asked"]
        assert len(q_events) == 1

        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ans1",
                at=svc.backend.now(),
                who="ui",
                kind="message",
                target_id="M-Q1",
                payload={"mission_id": "M-Q1", "text": "孔内体积为 50 微升。"},
            )
        svc.step()

        worker.run_until_idle()
        assert len(coord_model.seen_calls) == 3
        assert "必须调用" in str(coord_model.seen_calls[2][-1].content)

        svc.step()
        m1 = runs.get_mission(svc.conn, "M-Q1")
        assert m1 is not None
        assert m1["status"] == "failed"
        assert m1["conclusion"] == AUTOFAIL_REASON
    finally:
        worker.close()

    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="m2",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-Q2", "title": "问答后普通留言", "input_text": "请先确认"},
        )
    svc.step()

    m2_ev = [
        e
        for e in inbox.list_events_after(svc.conn, 0)
        if e["kind"] == "mission_created" and e["ref_id"] == "M-Q2"
    ][0]
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", int(m2_ev["id"]) - 1)

    coord_model_2 = ScriptedChatModel(
        [
            ai_tool_call("ask_researcher", {"question": "确认用 37℃ 吗？"}, call_id="ask2"),
            ai_tool_call(
                "create_experiment",
                {"objective": "37℃恒温", "procedure": "37℃ 30秒", "labware": {}},
                call_id="ce2",
            ),
            "实验已经下发给操作员，请稍候。",
        ]
    )
    op_model_2 = ScriptedChatModel(
        [ai_tool_call("finish_experiment", {"status": "stopped", "notes": "待板"}, call_id="fe2")]
    )
    worker2 = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=coord_model_2,
        operator_model=op_model_2,
    )
    try:
        worker2.run_until_idle()
        svc.step()
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="ans2",
                at=svc.backend.now(),
                who="ui",
                kind="message",
                target_id="M-Q2",
                payload={"mission_id": "M-Q2", "text": "确认 37℃。"},
            )
        svc.step()
        worker2.run_until_idle()
        svc.step()

        msg_cursor = inbox.list_events_after(svc.conn, 0)[-1]["id"]
        with svc.conn:
            inbox.set_cursor(svc.conn, "agents", msg_cursor)
            inbox.enqueue_command(
                svc.conn,
                key="casual_msg",
                at=svc.backend.now(),
                who="ui",
                kind="message",
                target_id="M-Q2",
                payload={"mission_id": "M-Q2", "text": "现在进展如何？"},
            )
        svc.step()
        worker2.run_until_idle()
        svc.step()

        assert len(coord_model_2.seen_calls) == 3
        m2_row = runs.get_mission(svc.conn, "M-Q2")
        assert m2_row is not None and m2_row["status"] == "running"
        replies = [
            e for e in inbox.list_events_after(svc.conn, msg_cursor) if e["kind"] == "reply"
        ]
        assert len(replies) == 1
        assert "实验已经下发" in replies[0]["payload"]["text"]
    finally:
        worker2.close()
        svc.conn.close()


def test_command_failed_event_wakes_sender_role_with_reason(tmp_path: Path) -> None:
    """`command_failed` routes back to `coordinator` or `operator` with terminal command rules."""
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"
    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="m",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-CF", "title": "命令失败唤醒", "input_text": "测试"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-CF", "task": {"objective": "一", "procedure": "一"}},
        )
        ev_c_id = inbox.append_event(
            svc.conn,
            at=svc.backend.now(),
            kind="command_failed",
            ref_id="M-CF",
            payload={
                "who": "coordinator",
                "kind": "create_experiment",
                "error": "任务单必须为对象",
                "mission_id": "M-CF",
            },
        )
        ev_o_id = inbox.append_event(
            svc.conn,
            at=svc.backend.now(),
            kind="command_failed",
            ref_id="E-001",
            payload={
                "who": "operator",
                "kind": "finish_experiment",
                "error": "状态非法",
                "experiment_id": "E-001",
                "mission_id": "M-CF",
            },
        )
    svc.step()

    events_by_id = {e["id"]: e for e in inbox.list_events_after(svc.conn, 0)}
    t_coord = classify_event(svc.conn, events_by_id[ev_c_id])
    assert t_coord is not None
    assert t_coord.role == "coordinator"
    assert t_coord.thread_id == "M-CF"
    assert t_coord.required_commands == COORDINATOR_TERMINAL_COMMANDS

    t_op = classify_event(svc.conn, events_by_id[ev_o_id])
    assert t_op is not None
    assert t_op.role == "operator"
    assert t_op.thread_id == "E-001"
    assert t_op.required_commands == OPERATOR_SUBMIT_OR_FINISH

    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", ev_c_id - 1)
    coord_m = ScriptedChatModel(
        [
            ai_tool_call(
                "finish_mission",
                {"conclusion": "已收到命令失败原因并结题", "status": "failed"},
                call_id="cf1",
            )
        ]
    )
    op_m = ScriptedChatModel(
        [ai_tool_call("finish_experiment", {"status": "failed", "notes": "已修正"}, call_id="cf2")]
    )
    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=coord_m,
        operator_model=op_m,
    )
    try:
        assert worker.step_once() is True
        assert "任务单必须为对象" in str(coord_m.seen_calls[0][-1].content)
        assert worker.step_once() is True
        assert "状态非法" in str(op_m.seen_calls[0][-1].content)
    finally:
        worker.close()
        svc.conn.close()
