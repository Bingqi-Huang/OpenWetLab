"""Step 6 tests: stop_all, actual shake duration, abort, warmup reconcile, step failure, offline & loop error."""

import json
from pathlib import Path

from wetlab.lab.service import Service, create_virtual_service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, records, runs


def _enqueue(svc: Service, kind: str, payload: dict, *, key: str, target_id: str | None = None) -> None:
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=key,
            at=svc.backend.now(),
            who="test",
            kind=kind,
            target_id=target_id,
            payload=payload,
        )


def test_stop_all_stops_pcr_lid_and_shaker_bypasses_closed_skills_and_rejects_resume(tmp_path: Path) -> None:
    """Step 6 row 6: `stop_all` sends `StopRun` + lid off and `StopShake` even when closed, marks `stopped`,
    emits `protocol_stopped`, and rejects subsequent `resume`.
    """
    conn = open_db(tmp_path / "wetlab.sqlite")
    sim = SimBackend(Scenario(labware={"LW-001": "pcr", "LW-002": "shaker"}))
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(svc, "place_labware", {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"}, key="lw1")
        _enqueue(svc, "place_labware", {"labware_id": "LW-002", "type": "96_plate", "position": "shaker"}, key="lw2")
        _enqueue(svc, "new_mission", {"mission_id": "M-20261118-010", "title": "停止测试", "input_text": "stop", "mode": "virtual"}, key="m")
        proto_dict = {
            "name": "PCR 与振荡同时运行",
            "labware": {"LW-001": "pcr", "LW-002": "shaker"},
            "steps": [
                {"skill": "pcr_close", "args": {}},
                {"skill": "pcr_start_program", "args": {"stages": [{"temperature_c": 95, "hold_seconds": 600}], "volume_ul": 50, "lid_c": 105}},
                {"skill": "shake_start", "args": {"rpm": 600}},
                {"wait": {"seconds": 600}},
                {"skill": "shake_stop", "args": {}},
            ],
        }
        _enqueue(svc, "submit_protocol", {"mission_id": "M-20261118-010", "protocol": proto_dict}, key="sub")
        svc.step()
        proto = runs.get_protocol(conn, "E-001-v1")
        assert proto is not None
        _enqueue(svc, "approve", {"protocol_id": "E-001-v1", "decided_by": "周老师", "fingerprint": proto["fingerprint"]}, key="app")

        # Advance through pcr_close, pcr_start_program, shake_start, and enter wait(600)
        svc.run(max_rounds=4)
        assert sim.state()["PCR002"]["PcrState"] == "Running"
        assert sim.state()["PCR002"]["HeaterLidEnabled"] is True
        assert sim.state()["T002001"]["ShakeState"] == 0

        # Lock `pcr_stop` and `shake_stop` in `closed_skills` -> `stop_all` MUST still execute both!
        sim.closed_skills.update({"pcr_stop", "shake_stop"})
        _enqueue(svc, "stop_all", {}, key="stop-1")
        svc.step()

        assert sim.closed_skills == {"pcr_stop", "shake_stop"}
        assert sim.state()["PCR002"]["PcrState"] == "Idle"
        assert sim.state()["PCR002"]["HeaterLidEnabled"] is False
        assert sim.state()["T002001"]["ShakeState"] == 1
        assert sim.state()["T002001"]["ActualSpeed"] == 0

        op_actions = [(op["device"], op["action"], op["status"]) for op in records.list_operations(conn)]
        assert ("pcr", "StopRun", "succeeded") in op_actions
        assert ("shaker", "StopShake", "succeeded") in op_actions

        assert runs.get_protocol(conn, "E-001-v1")["status"] == "stopped"  # type: ignore[index]
        step4 = runs.get_step(conn, "E-001-v1", 4)
        assert step4 is not None and step4["status"] == "failed"
        assert step4["result"]["error"]["message"] == "被人工停止"  # type: ignore[index]
        assert any(e["kind"] == "protocol_stopped" and e["ref_id"] == "E-001-v1" for e in inbox.list_events_after(conn, 0))

        # Subsequent `resume` MUST be rejected
        _enqueue(svc, "resume", {"protocol_id": "E-001-v1"}, key="res-after-stop")
        svc.step()
        res_cmd = inbox.get_command_by_key(conn, "res-after-stop")
        assert res_cmd is not None and json.loads(res_cmd["outcome"])["ok"] is False
        assert runs.get_protocol(conn, "E-001-v1")["status"] == "stopped"  # type: ignore[index]
    finally:
        conn.close()


def test_stop_all_during_long_shake_records_actual_seconds_not_planned(tmp_path: Path) -> None:
    """Step 6 row 7: `stop_all` mid-way through a 600s shake wait records actual elapsed seconds (~25s)."""
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(svc, "place_labware", {"labware_id": "LW-001", "type": "96_plate", "position": "shaker"}, key="lw")
        _enqueue(svc, "new_mission", {"mission_id": "M-20261118-011", "title": "振荡停止计时", "input_text": "shake", "mode": "virtual"}, key="m")
        proto_dict = {
            "name": "600 秒连续振荡",
            "labware": {"LW-001": "shaker"},
            "steps": [
                {"skill": "shake_start", "args": {"rpm": 800}},
                {"wait": {"seconds": 600}},
                {"skill": "shake_stop", "args": {}},
            ],
        }
        _enqueue(svc, "submit_protocol", {"mission_id": "M-20261118-011", "protocol": proto_dict}, key="sub")
        svc.step()
        proto = runs.get_protocol(svc.conn, "E-001-v1")
        assert proto is not None
        _enqueue(svc, "approve", {"protocol_id": "E-001-v1", "decided_by": "吴老师", "fingerprint": proto["fingerprint"]}, key="app")

        # Round 1 runs shake_start (~2.8s); rounds 2..24 wait 23s -> ~25s total shaking
        svc.run(max_rounds=24)
        _enqueue(svc, "stop_all", {}, key="stop-shake")
        svc.step()

        lw_events = [ev for ev in records.list_labware_events(svc.conn, "LW-001") if ev["what"] == "shake_stopped"]
        assert len(lw_events) == 1
        actual_s = float(lw_events[0]["detail"]["seconds"])
        assert 20.0 <= actual_s <= 35.0
        assert actual_s < 600.0
    finally:
        svc.conn.close()


def test_abort_command_and_unfreeze_abort_both_stop_experiment_with_event(tmp_path: Path) -> None:
    """Step 6 row 8: Both `abort` and `unfreeze(conclusion='abort')` mark protocol `aborted`,
    experiment `stopped`, and emit `experiment_finished`.
    """
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(svc, "new_mission", {"mission_id": "M-20261118-012", "title": "终止测试", "input_text": "abort", "mode": "virtual"}, key="m")
        for idx, exp_key in ((1, "e1"), (2, "e2")):
            _enqueue(
                svc,
                "create_experiment",
                {"mission_id": "M-20261118-012", "task": {"objective": f"实验{idx}", "procedure": "wait", "labware": {}, "constraints": {}}},
                key=exp_key,
            )
        svc.step()

        wait_proto = {"name": "等待方案", "labware": {}, "steps": [{"wait": {"seconds": 120}}]}
        # Case A: direct `abort` command on E-001
        _enqueue(svc, "submit_protocol", {"experiment_id": "E-001", "protocol": wait_proto}, key="sub1")
        svc.step()
        p1 = runs.get_protocol(svc.conn, "E-001-v1")
        assert p1 is not None
        _enqueue(svc, "approve", {"protocol_id": "E-001-v1", "decided_by": "郑老师", "fingerprint": p1["fingerprint"]}, key="app1")
        svc.run(max_rounds=2)
        _enqueue(svc, "abort", {"experiment_id": "E-001"}, key="abort1")
        svc.step()

        assert runs.get_protocol(svc.conn, "E-001-v1")["status"] == "aborted"  # type: ignore[index]
        assert runs.get_experiment(svc.conn, "E-001")["status"] == "stopped"  # type: ignore[index]
        assert runs.get_experiment(svc.conn, "E-001")["notes"] == "人工终止"  # type: ignore[index]

        # Case B: `unfreeze(conclusion='abort')` on E-002
        _enqueue(svc, "submit_protocol", {"experiment_id": "E-002", "protocol": wait_proto}, key="sub2")
        svc.step()
        p2 = runs.get_protocol(svc.conn, "E-002-v1")
        assert p2 is not None
        _enqueue(svc, "approve", {"protocol_id": "E-002-v1", "decided_by": "郑老师", "fingerprint": p2["fingerprint"]}, key="app2")
        svc.run(max_rounds=2)
        with svc.conn:
            island.set_frozen(svc.conn, reason="模拟现场冻结", at=svc.backend.now())
        _enqueue(svc, "unfreeze", {"conclusion": "abort"}, key="unf-abort")
        svc.step()

        assert island.get_freeze(svc.conn)["frozen"] is False
        assert runs.get_protocol(svc.conn, "E-002-v1")["status"] == "aborted"  # type: ignore[index]
        assert runs.get_experiment(svc.conn, "E-002")["status"] == "stopped"  # type: ignore[index]
        fin_events = [e["ref_id"] for e in inbox.list_events_after(svc.conn, 0) if e["kind"] == "experiment_finished"]
        assert fin_events == ["E-001", "E-002"]
    finally:
        svc.conn.close()


def test_startup_warmup_waits_for_telemetry_without_false_offline_and_probes_reads(tmp_path: Path) -> None:
    """Step 6 row 9: Incomplete telemetry for the first 3s does not trigger offline/freeze; once ready
    within 10s, `reconcile` probes read-only actions (skipping turntable) and enters loop normally.
    """
    conn = open_db(tmp_path / "wetlab.sqlite")
    sim = SimBackend()
    t0 = sim.now()
    orig_state = sim.state

    def delayed_state() -> dict:
        if sim.now() - t0 < 3.0:
            return {"_gateway": {"online": False, "busy_by_others": False}}
        return orig_state()

    sim.state = delayed_state  # type: ignore[method-assign]
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        info = svc.startup(warmup_s=10.0)
        assert info["gateway_online"] is True
        assert island.get_freeze(conn)["frozen"] is False
        assert inbox.list_events_after(conn, 0) == []
        assert info["probed"] == ["pcr.GetPcrStatus", "shaker.GetStatus", "arm.RobotMode", "rack.GetStatus"]
        svc.step()
        assert island.get_freeze(conn)["frozen"] is False
    finally:
        conn.close()


def test_step_failure_gateway_offline_and_unexpected_loop_exception(tmp_path: Path) -> None:
    """Step 6 rows 10, 11, & 16: Step failure stops subsequent steps; gateway offline freezes;
    unexpected exception in loop freezes and emits event without exiting service.
    """
    conn = open_db(tmp_path / "wetlab.sqlite")
    list_fault = Fault(device="pcr", action="ListFiles", nth=1, kind="fail", message="读取程序失败")
    sim = SimBackend(Scenario(faults=[list_fault]))
    notified: list[str] = []
    svc = Service(
        sim,
        conn,
        runs_dir=tmp_path / "runs",
        notify_fn=lambda ev: notified.append(ev["kind"]),
    )
    try:
        svc.startup()
        # Row 10: Non-moving step 1 fails -> protocol `failed` & Step 2 `pending`
        mission_payload = {
            "mission_id": "M-20261118-015",
            "title": "异常测试",
            "input_text": "err",
            "mode": "virtual",
        }
        _enqueue(svc, "new_mission", mission_payload, key="m")
        fail_proto = {
            "name": "读取程序失败测试",
            "labware": {},
            "steps": [
                {"skill": "pcr_list_programs", "args": {}},
                {"skill": "pcr_close", "args": {}},
            ],
        }
        _enqueue(svc, "submit_protocol", {"mission_id": "M-20261118-015", "protocol": fail_proto}, key="sub")
        svc.step()
        proto = runs.get_protocol(conn, "E-001-v1")
        assert proto is not None
        _enqueue(svc, "approve", {"protocol_id": "E-001-v1", "decided_by": "孙老师", "fingerprint": proto["fingerprint"]}, key="app")
        svc.run(max_rounds=3)

        assert runs.get_protocol(conn, "E-001-v1")["status"] == "failed"  # type: ignore[index]
        assert runs.get_step(conn, "E-001-v1", 1)["status"] == "failed"  # type: ignore[index]
        assert runs.get_step(conn, "E-001-v1", 2)["status"] == "pending"  # type: ignore[index]
        assert "protocol_failed" in [e["kind"] for e in inbox.list_events_after(conn, 0)]
        assert "protocol_failed" in notified

        # Row 11: Gateway goes offline -> island freezes, `gateway_offline` + `frozen` events emitted
        sim.gateway_online = False
        svc.step()
        assert island.get_freeze(conn)["frozen"] is True
        ev_kinds = [e["kind"] for e in inbox.list_events_after(conn, 0)]
        assert "gateway_offline" in ev_kinds and "frozen" in ev_kinds

        # Restore gateway and unfreeze, then Row 16: inject unexpected RuntimeError inside tick
        sim.gateway_online = True
        svc.step()
        _enqueue(svc, "unfreeze", {"conclusion": "step_done"}, key="unf-1")
        svc.step()
        assert island.get_freeze(conn)["frozen"] is False

        orig_state = sim.state

        def boom() -> dict:
            raise RuntimeError("模拟循环内部未知异常")

        sim.state = boom  # type: ignore[method-assign]
        svc.tick()  # MUST NOT raise!
        frz = island.get_freeze(conn)
        assert frz["frozen"] is True and "RuntimeError" in (frz["reason"] or "")
        assert any(e["kind"] == "frozen" and e["payload"].get("unexpected_error") for e in inbox.list_events_after(conn, 0))

        # Service stays alive and processes subsequent rounds once `boom` is cleared
        sim.state = orig_state  # type: ignore[method-assign]
        _enqueue(svc, "unfreeze", {"conclusion": "step_done"}, key="unf-2")
        svc.step()
        assert island.get_freeze(conn)["frozen"] is False
    finally:
        conn.close()
