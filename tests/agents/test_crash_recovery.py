"""Tests for `last_event_id` summarization (#18) and `durability='sync'` + SIGKILL (#20)."""

import json
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

from wetlab.agents.handoff import classify_event
from wetlab.agents.tools import TurnContext
from wetlab.agents.worker import AgentWorker
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, runs

from .conftest import ScriptedChatModel, ai_tool_call

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_summarization_triggered_then_restart_makes_zero_llm_calls(tmp_path: Path) -> None:
    """v0.6 #18: `SummarizationMiddleware` truncates `event-<id>` out of `messages`, yet
    `last_event_id` in `WetLabAgentState` prevents any duplicate LLM call on restart!
    """
    db_path = tmp_path / "wetlab.sqlite"
    agents_db = tmp_path / "agents.sqlite"
    runs_dir = tmp_path / "runs"

    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    sop_proto = json.loads((REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8"))
    svc.backend.world.plates["LW-001"] = "pcr"  # type: ignore[attr-defined]
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="lw",
            at=svc.backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
        )
        inbox.enqueue_command(
            svc.conn,
            key="m",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-18", "title": "压缩重启", "input_text": "压缩测试 " * 80},
        )
        inbox.enqueue_command(
            svc.conn,
            key="e",
            at=svc.backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={
                "mission_id": "M-18",
                "task": {
                    "objective": "长文本任务单触发对话压缩 " * 40,
                    "procedure": "详细步骤说明 " * 40,
                    "labware": {"LW-001": "pcr"},
                },
            },
        )
    svc.step()

    all_evs = inbox.list_events_after(svc.conn, 0)
    exp_ev = [e for e in all_evs if e["kind"] == "experiment_created"][0]
    ev_id = int(exp_ev["id"])
    with svc.conn:
        inbox.set_cursor(svc.conn, "agents", ev_id - 1)

    op_model = ScriptedChatModel(
        [
            ai_tool_call("get_lab_state", {}, call_id="s_read"),
            "已提取历史关键信息摘要：LW-001 在 PCR 仪内等待提交方案。",
            ai_tool_call("submit_protocol", {"protocol": sop_proto}, call_id="s_sub"),
        ]
    )
    worker1 = AgentWorker(
        db_path=db_path,
        agents_db_path=agents_db,
        runs_dir=runs_dir,
        operator_model=op_model,
        summarize_trigger_tokens=10,
        summarize_keep_messages=1,
    )
    target = classify_event(svc.conn, exp_ev)
    assert target is not None
    worker1._run_turn(exp_ev, target)

    tup = worker1.saver.get_tuple({"configurable": {"thread_id": "E-001"}})
    assert tup is not None
    ch_vals = tup.checkpoint["channel_values"]
    msg_ids = [getattr(m, "id", None) for m in ch_vals["messages"]]
    assert f"event-{ev_id}" not in msg_ids
    assert ch_vals["last_event_id"] == ev_id
    worker1.close()

    trap_model = ScriptedChatModel(
        ["should never be called"],
        before_call=lambda _i, _m: (_ for _ in ()).throw(AssertionError("0 LLM calls expected!")),
    )
    worker2 = AgentWorker(
        db_path=db_path,
        agents_db_path=agents_db,
        runs_dir=runs_dir,
        operator_model=trap_model,
        summarize_trigger_tokens=10,
        summarize_keep_messages=1,
    )
    try:
        assert worker2.step_once() is True
        assert len(trap_model.seen_calls) == 0
        assert inbox.get_cursor(svc.conn, "agents") == ev_id
        svc.step()
        assert len(runs.list_protocols(svc.conn, experiment_id="E-001")) == 1
    finally:
        worker2.close()
        svc.conn.close()


def test_durability_sync_passed_and_real_sigkill_subprocess_resumes_cleanly(
    tmp_path: Path,
) -> None:
    """v0.6 #20:
    1) Assert `durability='sync'` and `TurnContext` passed to every `graph.invoke`.
    2) Kill a real child `subprocess` with `SIGKILL` mid-turn after `create_experiment`,
       restart in parent process -> resumes with 0 duplicate commands or experiments.
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"

    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key="m_spy",
            at=svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-20A", "title": "同步落盘断言", "input_text": "测试"},
        )
    svc.step()

    spy_model = ScriptedChatModel(
        [ai_tool_call("finish_mission", {"conclusion": "完结", "status": "completed"}, call_id="s1")]
    )
    spy_worker = AgentWorker(db_path=db_path, runs_dir=runs_dir, coordinator_model=spy_model)
    seen_kwargs: list[dict[str, Any]] = []
    graph = spy_worker._get_agent("coordinator")
    orig_invoke = graph.invoke

    def wrapped_invoke(inp: Any, config: Any = None, **kwargs: Any) -> Any:
        seen_kwargs.append(kwargs)
        return orig_invoke(inp, config, **kwargs)

    graph.invoke = wrapped_invoke
    try:
        assert spy_worker.step_once() is True
        assert len(seen_kwargs) == 1
        assert seen_kwargs[0]["durability"] == "sync"
        assert isinstance(seen_kwargs[0]["context"], TurnContext)
        svc.step()
    finally:
        spy_worker.close()

    kill_db = tmp_path / "kill" / "wetlab.sqlite"
    kill_agents = tmp_path / "kill" / "agents.sqlite"
    kill_runs = tmp_path / "kill" / "runs"
    kill_svc = create_virtual_service(kill_db, runs_dir=kill_runs)
    kill_svc.startup()
    with kill_svc.conn:
        inbox.enqueue_command(
            kill_svc.conn,
            key="m_kill",
            at=kill_svc.backend.now(),
            who="ui",
            kind="new_mission",
            payload={"mission_id": "M-SIGKILL", "title": "真杀进程恢复", "input_text": "测试"},
        )
    kill_svc.step()

    child_script = """
import os, signal, sys
from pathlib import Path
from wetlab.agents.worker import AgentWorker
from tests.agents.conftest import ScriptedChatModel, ai_parallel_tool_calls

db_p, ag_p, runs_p = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])

def kill_on_second_model_call(call_idx, _msgs):
    if call_idx == 1:
        os.kill(os.getpid(), signal.SIGKILL)

model = ScriptedChatModel(
    [
        ai_parallel_tool_calls([
            ("create_experiment", {"objective": "真杀", "procedure": "步骤", "labware": {}}, "k1"),
            ("get_lab_state", {}, "k2"),
        ]),
        "unreachable",
    ],
    before_call=kill_on_second_model_call,
)
w = AgentWorker(db_path=db_p, agents_db_path=ag_p, runs_dir=runs_p, coordinator_model=model)
w.step_once()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", child_script, str(kill_db), str(kill_agents), str(kill_runs)],
        cwd=REPO_ROOT,
    )
    rc = proc.wait(timeout=20.0)
    assert rc == -signal.SIGKILL

    kill_svc.step()

    resumed_model = ScriptedChatModel(["从 SIGKILL 断点恢复完成本轮。"])
    worker_resumed = AgentWorker(
        db_path=kill_db,
        agents_db_path=kill_agents,
        runs_dir=kill_runs,
        coordinator_model=resumed_model,
    )
    try:
        assert worker_resumed.step_once() is True
        kill_svc.step()

        exps = runs.list_experiments(kill_svc.conn, mission_id="M-SIGKILL")
        assert len(exps) == 1
        coord_cmds = kill_svc.conn.execute(
            "SELECT kind FROM commands WHERE who = 'coordinator'"
        ).fetchall()
        assert len(coord_cmds) == 1

        tup = worker_resumed.saver.get_tuple({"configurable": {"thread_id": "M-SIGKILL"}})
        assert tup is not None
        msgs = tup.checkpoint["channel_values"]["messages"]
        assert [getattr(m, "id", None) for m in msgs].count("event-1") == 1
    finally:
        worker_resumed.close()
        kill_svc.conn.close()

