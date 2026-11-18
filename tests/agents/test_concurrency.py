"""Concurrency isolation test: 30s slow Agent turn does not delay Service 1s loop (`pause` < 2s)."""

from pathlib import Path
import threading
import time

from wetlab.agents.worker import AgentWorker
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.service.main import Service
from wetlab.lab.store import inbox, open_db, runs

from .conftest import ScriptedChatModel

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_service_1s_loop_unaffected_during_30s_slow_agent_turn_and_pause_takes_effect_within_2s(
    tmp_path: Path,
) -> None:
    """04 section 11 Step 9 row 3: Agent stalls for 30s in a turn while Service runs 1s rounds
    and processes a `pause` command within 2 seconds.
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"

    setup_conn = open_db(db_path)
    sim_backend = SimBackend(Scenario(labware={"LW-001": "shaker"}))
    svc = Service(sim_backend, setup_conn, runs_dir=runs_dir)
    svc.startup()

    # Register plate, mission, experiment, and a running protocol in a 600s wait step
    with setup_conn:
        inbox.enqueue_command(
            setup_conn,
            key="lw",
            at=sim_backend.now(),
            who="ui",
            kind="place_labware",
            payload={"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
        )
        inbox.enqueue_command(
            setup_conn,
            key="m",
            at=sim_backend.now(),
            who="ui",
            kind="new_mission",
            payload={
                "mission_id": "M-20261118-030",
                "title": "并发测试",
                "input_text": "测试",
            },
        )
        inbox.enqueue_command(
            setup_conn,
            key="e",
            at=sim_backend.now(),
            who="coordinator",
            kind="create_experiment",
            payload={
                "mission_id": "M-20261118-030",
                "task": {
                    "objective": "等待",
                    "procedure": "等待",
                    "labware": {"LW-001": "shaker"},
                    "constraints": {},
                },
            },
        )
        inbox.enqueue_command(
            setup_conn,
            key="sub",
            at=sim_backend.now(),
            who="operator",
            kind="submit_protocol",
            target_id="E-001",
            payload={
                "experiment_id": "E-001",
                "protocol": {
                    "name": "长等待方案",
                    "labware": {"LW-001": "shaker"},
                    "steps": [{"wait": {"seconds": 600}}],
                },
            },
        )
    svc.step()
    proto = runs.get_protocol(setup_conn, "E-001-v1")
    assert proto is not None
    with setup_conn:
        inbox.enqueue_command(
            setup_conn,
            key="app",
            at=sim_backend.now(),
            who="ui",
            kind="approve",
            target_id="E-001-v1",
            payload={
                "protocol_id": "E-001-v1",
                "decided_by": "王老师",
                "fingerprint": proto["fingerprint"],
            },
        )
    svc.step()
    svc.step()
    assert runs.get_protocol(setup_conn, "E-001-v1")["status"] == "running"  # type: ignore[index]

    # Position `agents` cursor just before a researcher `message` event
    head_id = inbox.list_events_after(setup_conn, 0)[-1]["id"]
    with setup_conn:
        inbox.set_cursor(setup_conn, "agents", head_id)
        inbox.enqueue_command(
            setup_conn,
            key="msg:slow",
            at=sim_backend.now(),
            who="ui",
            kind="message",
            target_id="M-20261118-030",
            payload={"mission_id": "M-20261118-030", "text": "请详细思考 30 秒后回答。"},
        )
    svc.step()

    agent_entered_llm = threading.Event()
    stop_service = threading.Event()
    service_rounds = 0
    service_errors: list[BaseException] = []

    def slow_30s_hook(_idx: int, _msgs: list) -> None:
        agent_entered_llm.set()
        time.sleep(30.0)

    slow_model = ScriptedChatModel(["思考 30 秒后的回答：当前等待步骤正常。"], before_call=slow_30s_hook)

    def run_service_wall_loop() -> None:
        nonlocal service_rounds
        thread_svc = Service(sim_backend, open_db(db_path), runs_dir=runs_dir)
        thread_svc.reconciled = True
        thread_svc.gateway_was_online = True
        try:
            while not stop_service.is_set():
                t0 = time.monotonic()
                thread_svc.step()
                service_rounds += 1
                remaining = 1.0 - (time.monotonic() - t0)
                if remaining > 0:
                    stop_service.wait(timeout=remaining)
        except BaseException as exc:  # pragma: no cover
            service_errors.append(exc)
        finally:
            thread_svc.conn.close()

    worker = AgentWorker(
        db_path=db_path,
        runs_dir=runs_dir,
        sops_dir=REPO_ROOT / "sops",
        lab_md_path=REPO_ROOT / "LAB.md",
        coordinator_model=slow_model,
    )
    agent_thread = threading.Thread(target=worker.step_once)
    service_thread = threading.Thread(target=run_service_wall_loop)

    observer_conn = open_db(db_path)
    try:
        service_thread.start()
        agent_thread.start()
        assert agent_entered_llm.wait(timeout=5.0)

        # Agent is now mid-turn inside its 30s model call! Enqueue `pause` and measure latency:
        pause_sent_at = time.monotonic()
        with observer_conn:
            inbox.enqueue_command(
                observer_conn,
                key="ui:pause:live",
                at=time.time(),
                who="ui",
                kind="pause",
                target_id="E-001-v1",
                payload={"protocol_id": "E-001-v1"},
            )

        paused_at: float | None = None
        while time.monotonic() - pause_sent_at < 3.0:
            p_now = runs.get_protocol(observer_conn, "E-001-v1")
            if p_now is not None and p_now["status"] == "paused":
                paused_at = time.monotonic()
                break
            time.sleep(0.05)

        assert paused_at is not None, "pause did not take effect within 3s"
        pause_latency_s = paused_at - pause_sent_at
        assert pause_latency_s < 2.0
        assert agent_thread.is_alive(), "Agent turn should still be in its 30s sleep"

        agent_thread.join(timeout=35.0)
        assert not agent_thread.is_alive()
        # Let service process the Agent's `reply` command on the next 1s tick
        time.sleep(1.2)
        stop_service.set()
        service_thread.join(timeout=3.0)

        assert service_errors == []
        assert service_rounds >= 28
        obs_events = inbox.list_events_after(observer_conn, head_id)
        replies = [e for e in obs_events if e["kind"] == "reply"]
        assert len(replies) == 1
        assert "思考 30 秒后的回答" in replies[0]["payload"]["text"]
    finally:
        stop_service.set()
        observer_conn.close()
        worker.close()
        setup_conn.close()
