"""Tests for model invocation error 1-hour exponential backoff and autofail (`04` v0.6 #19)."""

from pathlib import Path
from typing import Any

import pytest

from wetlab.agents.worker import AUTOFAIL_REASON, MODEL_ERROR_REASON, AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, runs

from .conftest import ScriptedChatModel, ai_tool_call


def test_model_error_1h_backoff_emits_agent_error_autofails_and_continues(tmp_path: Path) -> None:
    """v0.6 #19: consecutive LLM errors enqueue `agent_error` on first failure, back off
    5s -> 10s -> ... -> 300s up to 3600s on virtual clock, autofail with `'模型接口出错'`,
    and continue processing the next event.
    """
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
            payload={"mission_id": "M-19", "title": "接口容错", "input_text": "测试"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-19", "task": {"objective": "一", "procedure": "一"}},
        )
    svc.step()

    all_evs = inbox.list_events_after(svc.conn, 0)
    exp_ev = [e for e in all_evs if e["kind"] == "experiment_created"][0]
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", int(exp_ev["id"]) - 1)

    clock = 1000.0
    slept: list[float] = []

    def fake_now() -> float:
        return clock

    def fake_sleep(dt: float) -> None:
        nonlocal clock
        slept.append(dt)
        clock += dt

    def always_raise(_idx: int, _msgs: list[Any]) -> None:
        raise ConnectionError("503 Service Unavailable")

    broken_op = ScriptedChatModel(["unused"], before_call=always_raise)
    healthy_coord = ScriptedChatModel(
        [
            ai_tool_call(
                "finish_mission",
                {"conclusion": "模型故障实验已收敛结题", "status": "failed"},
                call_id="fm_after_err",
            )
        ]
    )
    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=healthy_coord,
        operator_model=broken_op,
        now_fn=fake_now,
        sleep_fn=fake_sleep,
    )
    try:
        assert worker.step_once() is True
        assert slept[:7] == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0]
        assert max(slept) == 300.0
        assert sum(slept) == pytest.approx(3600.0)

        err_cmd = inbox.get_command_by_key(svc.conn, f"agent_error:E-001:ev{exp_ev['id']}")
        assert err_cmd is not None and err_cmd["kind"] == "agent_error"

        svc.step()
        exp = runs.get_experiment(svc.conn, "E-001")
        assert exp is not None
        assert exp["status"] == "failed"
        assert exp["notes"] == MODEL_ERROR_REASON

        err_events = [e for e in inbox.list_events_after(svc.conn, 0) if e["kind"] == "agent_error"]
        assert len(err_events) == 1

        assert worker.run_until_idle() >= 1
        svc.step()
        mission = runs.get_mission(svc.conn, "M-19")
        assert mission is not None
        assert mission["status"] == "failed"
        assert mission["conclusion"] == "模型故障实验已收敛结题"
    finally:
        worker.close()
        svc.conn.close()


def test_missing_terminal_command_reminded_once_via_state_then_autofails(tmp_path: Path) -> None:
    """Step 9 row 7 + #18 `reminded_event_id`: reminded once, then autofails if still missing."""
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
            payload={"mission_id": "M-070", "title": "未发命令测试", "input_text": "测试"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e1",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={"mission_id": "M-070", "task": {"objective": "测试1", "procedure": "无"}},
        )
    svc.step()

    all_evs = inbox.list_events_after(svc.conn, 0)
    exp_ev = [e for e in all_evs if e["kind"] == "experiment_created"][0]
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", int(exp_ev["id"]) - 1)

    lazy_op = ScriptedChatModel(["先不调用结束工具。", "收到提醒但仍不调工具。"])
    recovering_coord = ScriptedChatModel(
        [
            "先只说一句话忘记调工具。",
            ai_tool_call(
                "finish_mission",
                {"conclusion": "实验失败已结题", "status": "failed"},
                call_id="fm_ok",
            ),
        ]
    )
    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        coordinator_model=recovering_coord,
        operator_model=lazy_op,
    )
    try:
        worker.step_once()
        assert len(lazy_op.seen_calls) == 2
        tup = worker.saver.get_tuple({"configurable": {"thread_id": "E-001"}})
        assert tup is not None
        assert tup.checkpoint["channel_values"]["reminded_event_id"] == int(exp_ev["id"])

        svc.step()
        exp = runs.get_experiment(svc.conn, "E-001")
        assert exp is not None and exp["status"] == "failed" and exp["notes"] == AUTOFAIL_REASON

        worker.step_once()
        svc.step()
        mission = runs.get_mission(svc.conn, "M-070")
        assert mission is not None
        assert mission["status"] == "failed"
        assert mission["conclusion"] == "实验失败已结题"
    finally:
        worker.close()
        svc.conn.close()

