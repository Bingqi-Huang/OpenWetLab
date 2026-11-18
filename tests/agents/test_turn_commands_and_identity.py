"""Tests for single terminal command per turn (#16) and strict status enums (#17)."""

import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from wetlab.agents.tools import TurnContext, build_coordinator_tools, build_operator_tools
from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, runs

from .conftest import ScriptedChatModel, ai_parallel_tool_calls, ai_tool_call, make_runtime


def test_single_terminal_command_per_turn_sequential_parallel_and_crash_replay(
    tmp_path: Path,
) -> None:
    """v0.6 #16:
    1) Sequential 3 terminal calls -> `return_direct=True` stops after A, 1 LLM call, only A in DB.
    2) Single-message parallel `submit_protocol` + `finish_experiment` -> 1 DB row, 1 tool error.
    3) Crash replay (identical kind & payload) -> returns ok=True; different payload -> ok=False.
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
        inbox.enqueue_command(
            svc.conn,
            key="m",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-16", "title": "单命令测试", "input_text": "测试"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e1",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={
                "mission_id": "M-16",
                "task": {"objective": "一", "procedure": "一", "labware": {"LW-001": "shaker"}},
            },
        )
        inbox.enqueue_command(
            svc.conn,
            key="e2",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={
                "mission_id": "M-16",
                "task": {"objective": "二", "procedure": "二", "labware": {"LW-001": "shaker"}},
            },
        )
    svc.step()

    events = inbox.list_events_after(svc.conn, 0)
    exp_events = [e for e in events if e["kind"] == "experiment_created"]
    ev_e1, ev_e2 = exp_events[0], exp_events[1]
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", int(ev_e1["id"]) - 1)

    proto_a = {
        "name": "方案A",
        "labware": {"LW-001": "shaker"},
        "steps": [{"skill": "shake", "args": {"seconds": 5, "rpm": 300}}],
    }
    proto_b = {
        "name": "方案B",
        "labware": {"LW-001": "shaker"},
        "steps": [{"skill": "shake", "args": {"seconds": 10, "rpm": 500}}],
    }

    # Case 1: Sequential 3 terminal calls on E-001; Case 2: Parallel 2 calls on E-002
    op_model = ScriptedChatModel(
        [
            ai_tool_call("submit_protocol", {"protocol": proto_a}, call_id="seq_a"),
            ai_tool_call("submit_protocol", {"protocol": proto_b}, call_id="seq_b"),
            ai_tool_call("finish_experiment", {"status": "failed", "notes": "多调"}, call_id="seq_c"),
        ]
    )
    worker1 = AgentWorker(db_path=db_path, runs_dir=runs_dir, operator_model=op_model)
    try:
        assert worker1.step_once() is True
        # `return_direct=True` terminated the turn immediately after `seq_a` -> 1 LLM call!
        assert len(op_model.seen_calls) == 1
        cmd_e1 = inbox.get_command_by_key(svc.conn, f"turn:E-001:ev{ev_e1['id']}")
        assert cmd_e1 is not None
        assert cmd_e1["kind"] == "submit_protocol"
        assert cmd_e1["payload"]["protocol"]["name"] == "方案A"
    finally:
        worker1.close()

    # Case 2: Single AIMessage calling BOTH `submit_protocol` and `finish_experiment` concurrently
    parallel_model = ScriptedChatModel(
        [
            ai_parallel_tool_calls(
                [
                    ("submit_protocol", {"protocol": proto_a}, "par_1"),
                    ("finish_experiment", {"status": "failed", "notes": "并发第二条"}, "par_2"),
                ]
            )
        ]
    )
    worker2 = AgentWorker(db_path=db_path, runs_dir=runs_dir, operator_model=parallel_model)
    try:
        assert worker2.step_once() is True
        assert len(parallel_model.seen_calls) == 1

        e2_cmds = svc.conn.execute(
            "SELECT key, kind, payload_json FROM commands WHERE key LIKE ?",
            (f"%:ev{ev_e2['id']}%",),
        ).fetchall()
        assert len(e2_cmds) == 1
        assert e2_cmds[0]["key"] == f"turn:E-002:ev{ev_e2['id']}"

        tup = worker2.saver.get_tuple({"configurable": {"thread_id": "E-002"}})
        assert tup is not None
        tool_msgs = [
            json.loads(m.content)
            for m in tup.checkpoint["channel_values"]["messages"]
            if m.type == "tool"
        ]
        assert len(tool_msgs) == 2
        oks = [m["ok"] for m in tool_msgs]
        assert sorted(oks) == [False, True]
        err_msg = [m["error"] for m in tool_msgs if not m["ok"]][0]
        assert "这一轮已经发出过" in err_msg

        # Case 3: Crash replay vs conflicting second call on the same `(thread_id, event_id)`
        replay_ctx = TurnContext(
            event_id=int(ev_e1["id"]),
            mission_id="M-16",
            thread_id="E-001",
            db_path=db_path,
            experiment_id="E-001",
        )
        op_tools = {t.name: t for t in build_operator_tools()}
        replay_ok = op_tools["submit_protocol"].invoke(
            {"protocol": proto_a, "runtime": make_runtime(replay_ctx)}
        )
        assert replay_ok["ok"] is True
        assert replay_ok["newly_enqueued"] is False

        conflict_b = op_tools["submit_protocol"].invoke(
            {"protocol": proto_b, "runtime": make_runtime(replay_ctx)}
        )
        assert conflict_b == {"ok": False, "error": "这一轮已经发出过 submit_protocol 命令"}
    finally:
        worker2.close()
        svc.conn.close()


def test_status_enum_validation_in_tools_and_service(tmp_path: Path) -> None:
    """v0.6 #17: `finish_experiment` and `finish_mission` enforce strict status enums."""
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
            payload={"mission_id": "M-17", "title": "枚举校验", "input_text": "测试"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-17", "task": {"objective": "一", "procedure": "一"}},
        )
    svc.step()

    all_evs = inbox.list_events_after(svc.conn, 0)
    exp_ev = [e for e in all_evs if e["kind"] == "experiment_created"][0]
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", int(exp_ev["id"]) - 1)

    # Model passes invalid `status="great success"` -> ToolErrorMiddleware returns validation error,
    # 0 rows written for `"great success"`, then model corrects to `"completed"`.
    op_model = ScriptedChatModel(
        [
            ai_tool_call(
                "finish_experiment",
                {"status": "great success", "notes": "非法状态"},
                call_id="bad_st",
            ),
            ai_tool_call(
                "finish_experiment",
                {"status": "completed", "notes": "修正为合法枚举"},
                call_id="good_st",
            ),
        ]
    )
    worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, operator_model=op_model)
    try:
        unit_ctx = TurnContext(
            event_id=999,
            mission_id="M-17",
            thread_id="E-001",
            db_path=db_path,
            experiment_id="E-001",
        )
        op_tools = {t.name: t for t in build_operator_tools()}
        coord_tools = {t.name: t for t in build_coordinator_tools()}
        with pytest.raises(ValidationError):
            op_tools["finish_experiment"].invoke(
                {"status": "great success", "notes": "x", "runtime": make_runtime(unit_ctx)}
            )
        with pytest.raises(ValidationError):
            coord_tools["finish_mission"].invoke(
                {"conclusion": "x", "status": "stopped", "runtime": make_runtime(unit_ctx)}
            )

        assert worker.step_once() is True
        assert len(op_model.seen_calls) == 2
        seen_second_turn = "\n".join(str(m.content) for m in op_model.seen_calls[1])
        assert "Input should be 'completed', 'stopped' or 'failed'" in seen_second_turn
        cmd = inbox.get_command_by_key(svc.conn, f"turn:E-001:ev{exp_ev['id']}")
        assert cmd is not None and cmd["payload"]["status"] == "completed"

        # Direct DB injection of invalid status into `commands` is rejected by Service
        with svc.conn:
            inbox.enqueue_command(
                svc.conn,
                key="raw_bad_fe",
                at=svc.backend.now(),
                who="operator",
                kind="finish_experiment",
                target_id="E-001",
                payload={"experiment_id": "E-001", "status": "great success", "notes": "绕过工具"},
            )
        svc.step()
        bad_row = inbox.get_command_by_key(svc.conn, "raw_bad_fe")
        assert bad_row is not None and bad_row["handled_at"] is not None
        outcome = json.loads(bad_row["outcome"])
        assert outcome["ok"] is False
        e1_row = runs.get_experiment(svc.conn, "E-001")
        assert e1_row is not None and e1_row["status"] == "completed"
    finally:
        worker.close()
        svc.conn.close()
