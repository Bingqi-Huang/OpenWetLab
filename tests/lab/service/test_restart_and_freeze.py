"""Step 6 tests: cold restarts across steps/waits/PCR, `sent` crash freeze, transfer unfreeze, 16h wait."""

import json
from pathlib import Path

from wetlab.lab.service import Service, create_virtual_service
from wetlab.lab.store import inbox, island, records, runs

REPO_ROOT = Path(__file__).resolve().parents[3]
SOP_37C = json.loads((REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8"))


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


def test_restart_between_steps_during_waiting_and_during_running_pcr_program(tmp_path: Path) -> None:
    """Step 6 row 12: Restart between steps, during `waiting`, and while PCR program is running
    resumes cleanly with zero duplicate device commands and non-decreasing timestamps.
    """
    db_path = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"

    proto_dict = {
        "name": "跨重启综合方案",
        "labware": {"LW-001": "pcr"},
        "steps": [
            {"skill": "pcr_close", "args": {}},
            {"wait": {"seconds": 10}},
            {"skill": "pcr_start_program", "args": {"stages": [{"temperature_c": 37, "hold_seconds": 30}], "volume_ul": 50, "lid_c": None}},
            {"wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 600}},
            {"skill": "pcr_confirm_finished", "args": {}},
            {"skill": "pcr_open", "args": {}},
        ],
    }

    svc1 = create_virtual_service(db_path, runs_dir=runs_dir)
    svc1.startup()
    _enqueue(svc1, "place_labware", {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"}, key="lw")
    _enqueue(svc1, "new_mission", {"mission_id": "M-20261118-020", "title": "重启恢复", "input_text": "restart", "mode": "virtual"}, key="m")
    _enqueue(svc1, "submit_protocol", {"mission_id": "M-20261118-020", "protocol": proto_dict}, key="sub")
    svc1.step()
    p = runs.get_protocol(svc1.conn, "E-001-v1")
    assert p is not None
    _enqueue(svc1, "approve", {"protocol_id": "E-001-v1", "decided_by": "林老师", "fingerprint": p["fingerprint"]}, key="app")

    # Round 1 finishes Step 1 (`pcr_close`). Restart #1: between Step 1 (`done`) and Step 2 (`pending`)!
    svc1.step()
    assert runs.get_step(svc1.conn, "E-001-v1", 1)["status"] == "done"  # type: ignore[index]
    assert runs.get_step(svc1.conn, "E-001-v1", 2)["status"] == "pending"  # type: ignore[index]
    t_after_svc1 = svc1.backend.now()
    svc1.conn.close()
    del svc1

    svc2 = create_virtual_service(db_path, runs_dir=runs_dir)
    assert svc2.backend.now() >= t_after_svc1
    svc2.startup()
    # Advance 3 rounds -> Step 2 (`wait 10s`) is now `waiting` mid-countdown. Restart #2: during `waiting`!
    svc2.run(max_rounds=3)
    assert runs.get_step(svc2.conn, "E-001-v1", 2)["status"] == "waiting"  # type: ignore[index]
    t_after_svc2 = svc2.backend.now()
    svc2.conn.close()
    del svc2

    svc3 = create_virtual_service(db_path, runs_dir=runs_dir)
    assert svc3.backend.now() >= t_after_svc2
    svc3.startup()
    # Finish 10s wait + Step 3 (`pcr_start_program`) + enter Step 4 (`wait_until` while PCR is Running)
    for _ in range(20):
        svc3.step()
        if runs.get_step(svc3.conn, "E-001-v1", 4)["status"] == "waiting":  # type: ignore[index]
            break
    assert svc3.backend.state()["PCR002"]["PcrState"] == "Running"
    t_after_svc3 = svc3.backend.now()
    svc3.conn.close()
    del svc3

    # Restart #3: while PCR program is actively Running on device!
    svc4 = create_virtual_service(db_path, runs_dir=runs_dir)
    try:
        assert svc4.backend.now() >= t_after_svc3
        assert svc4.backend.state()["PCR002"]["PcrState"] == "Running"
        svc4.startup()
        for _ in range(60):
            svc4.step()
            if runs.get_protocol(svc4.conn, "E-001-v1")["status"] == "done":  # type: ignore[index]
                break

        assert runs.get_protocol(svc4.conn, "E-001-v1")["status"] == "done"  # type: ignore[index]
        # Verify zero duplicate protocol commands!
        proto_ops = records.list_operations(svc4.conn, protocol_id="E-001-v1")
        action_counts = [op["action"] for op in proto_ops]
        assert action_counts.count("CloseDoor") == 1
        assert action_counts.count("RunProgram") == 1
        assert action_counts.count("OpenDoor") == 1

        # Verify operation timestamps are strictly non-decreasing
        starts = [float(op["started_at"]) for op in records.list_operations(svc4.conn)]
        assert starts == sorted(starts)
    finally:
        svc4.conn.close()


def test_restart_with_sent_step_freezes_and_unfreeze_step_done_or_step_failed(tmp_path: Path) -> None:
    """Step 6 row 13: Restart when a step is `sent` freezes the island; `unfreeze(step_done)` continues
    from the next step without re-executing the `sent` step; `unfreeze(step_failed)` fails protocol.
    """
    # Part A: `step_done` resolution
    db_a = tmp_path / "a.sqlite"
    runs_a = tmp_path / "runs_a"
    svc1 = create_virtual_service(db_a, runs_dir=runs_a)
    svc1.startup()
    _enqueue(svc1, "new_mission", {"mission_id": "M-20261118-021", "title": "sent恢复", "input_text": "sent", "mode": "virtual"}, key="m")
    two_step = {"name": "开门再关门", "labware": {}, "steps": [{"skill": "pcr_open", "args": {}}, {"skill": "pcr_close", "args": {}}]}
    _enqueue(svc1, "submit_protocol", {"mission_id": "M-20261118-021", "protocol": two_step}, key="sub")
    svc1.step()
    p = runs.get_protocol(svc1.conn, "E-001-v1")
    assert p is not None
    _enqueue(svc1, "approve", {"protocol_id": "E-001-v1", "decided_by": "郑老师", "fingerprint": p["fingerprint"]}, key="app")
    # Simulate process crash right after Step 1 was persisted as `sent` (door physically opened on island)
    with svc1.conn:
        runs.decide_protocol(svc1.conn, "E-001-v1", status="approved", decided_by="郑老师", decided_at=svc1.backend.now())
        runs.update_protocol_status(svc1.conn, "E-001-v1", "running")
        runs.update_step(svc1.conn, "E-001-v1", 1, status="sent", started_at=svc1.backend.now())
    svc1.conn.close()

    svc2 = create_virtual_service(db_a, runs_dir=runs_a)
    try:
        info = svc2.startup()
        assert info["frozen_sent_steps"] == [("E-001-v1", 1)]
        assert island.get_freeze(svc2.conn)["frozen"] is True
        assert any(e["kind"] == "frozen" for e in inbox.list_events_after(svc2.conn, 0))

        # Unfreeze with `step_done`: Step 1 MUST NOT execute `OpenDoor`, Step 2 (`CloseDoor`) executes to `done`
        _enqueue(svc2, "unfreeze", {"conclusion": "step_done"}, key="unf-done")
        svc2.run(max_rounds=3)

        assert island.get_freeze(svc2.conn)["frozen"] is False
        assert runs.get_protocol(svc2.conn, "E-001-v1")["status"] == "done"  # type: ignore[index]
        proto_ops = [op["action"] for op in records.list_operations(svc2.conn, protocol_id="E-001-v1")]
        assert "OpenDoor" not in proto_ops
        assert proto_ops == ["CloseDoor"]
    finally:
        svc2.conn.close()

    # Part B: `step_failed` resolution
    db_b = tmp_path / "b.sqlite"
    runs_b = tmp_path / "runs_b"
    svc3 = create_virtual_service(db_b, runs_dir=runs_b)
    svc3.startup()
    _enqueue(svc3, "new_mission", {"mission_id": "M-20261118-022", "title": "sent失败", "input_text": "sent", "mode": "virtual"}, key="m")
    _enqueue(svc3, "submit_protocol", {"mission_id": "M-20261118-022", "protocol": two_step}, key="sub")
    svc3.step()
    with svc3.conn:
        runs.update_protocol_status(svc3.conn, "E-001-v1", "running")
        runs.update_step(svc3.conn, "E-001-v1", 1, status="sent", started_at=svc3.backend.now())
    svc3.conn.close()

    svc4 = create_virtual_service(db_b, runs_dir=runs_b)
    try:
        svc4.startup()
        assert island.get_freeze(svc4.conn)["frozen"] is True
        _enqueue(svc4, "unfreeze", {"conclusion": "step_failed"}, key="unf-fail")
        svc4.run(max_rounds=2)

        assert island.get_freeze(svc4.conn)["frozen"] is False
        assert runs.get_protocol(svc4.conn, "E-001-v1")["status"] == "failed"  # type: ignore[index]
        assert runs.get_step(svc4.conn, "E-001-v1", 2)["status"] == "pending"  # type: ignore[index]
        assert any(e["kind"] == "protocol_failed" for e in inbox.list_events_after(svc4.conn, 0))
    finally:
        svc4.conn.close()


def test_frozen_transfer_step_requires_labware_position_and_corrects_ledger(tmp_path: Path) -> None:
    """Step 6 row 14: Unfreezing a `transfer` step without `labware_position` is rejected;
    supplying `labware_position` updates the labware ledger and clears freeze.
    """
    db_path = tmp_path / "transfer.sqlite"
    svc1 = create_virtual_service(db_path, runs_dir=tmp_path / "runs")
    svc1.startup()
    _enqueue(svc1, "place_labware", {"labware_id": "LW-001", "type": "96_plate", "position": "rack:1-3"}, key="lw")
    _enqueue(svc1, "new_mission", {"mission_id": "M-20261118-023", "title": "搬运解冻", "input_text": "xfer", "mode": "virtual"}, key="m")
    xfer_proto = {
        "name": "料架搬到振荡模组",
        "labware": {"LW-001": "rack:1-3"},
        "steps": [{"skill": "transfer", "args": {"labware_id": "LW-001", "to_position": "shaker"}}],
    }
    _enqueue(svc1, "submit_protocol", {"mission_id": "M-20261118-023", "protocol": xfer_proto}, key="sub")
    svc1.step()
    with svc1.conn:
        runs.update_protocol_status(svc1.conn, "E-001-v1", "running")
        runs.update_step(svc1.conn, "E-001-v1", 1, status="sent", started_at=svc1.backend.now())
    svc1.conn.close()

    svc2 = create_virtual_service(db_path, runs_dir=tmp_path / "runs")
    try:
        svc2.startup()
        assert island.get_freeze(svc2.conn)["frozen"] is True

        # Missing `labware_position` -> rejected, stays frozen, plate stays at `rack:1-3`
        _enqueue(svc2, "unfreeze", {"conclusion": "step_done"}, key="unf-no-pos")
        svc2.step()
        bad_cmd = inbox.get_command_by_key(svc2.conn, "unf-no-pos")
        assert bad_cmd is not None and json.loads(bad_cmd["outcome"])["ok"] is False
        assert island.get_freeze(svc2.conn)["frozen"] is True
        assert records.get_labware(svc2.conn, "LW-001")["position"] == "rack:1-3"  # type: ignore[index]

        # With `labware_position="shaker"` -> ledger corrected, freeze cleared, protocol done
        _enqueue(svc2, "unfreeze", {"conclusion": "step_done", "labware_position": "shaker"}, key="unf-with-pos")
        svc2.step()
        assert island.get_freeze(svc2.conn)["frozen"] is False
        assert records.get_labware(svc2.conn, "LW-001")["position"] == "shaker"  # type: ignore[index]
        corr_events = [e for e in records.list_labware_events(svc2.conn, "LW-001") if e["what"] == "corrected"]
        assert len(corr_events) == 1 and corr_events[0]["by"] == "human"
        assert runs.get_protocol(svc2.conn, "E-001-v1")["status"] == "done"  # type: ignore[index]
    finally:
        svc2.conn.close()


def test_sixteen_hour_wait_protocol_restarted_three_times_completes_accurately(tmp_path: Path) -> None:
    """Step 6 row 15: Protocol with a 16-hour (57,600s) wait restarted 3 times mid-wait completes
    with an accurate, monotonic timeline.
    """
    db_path = tmp_path / "wait16h.sqlite"
    runs_dir = tmp_path / "runs"
    wait_s = 16 * 3600  # 57,600 seconds

    svc = create_virtual_service(db_path, runs_dir=runs_dir)
    svc.startup()
    _enqueue(svc, "new_mission", {"mission_id": "M-20261118-024", "title": "16小时等待", "input_text": "16h", "mode": "virtual"}, key="m")
    proto_16h = {"name": "过夜16小时等待", "labware": {}, "steps": [{"wait": {"seconds": wait_s}}, {"skill": "pcr_open", "args": {}}]}
    _enqueue(svc, "submit_protocol", {"mission_id": "M-20261118-024", "protocol": proto_16h}, key="sub")
    svc.step()
    p = runs.get_protocol(svc.conn, "E-001-v1")
    assert p is not None
    _enqueue(svc, "approve", {"protocol_id": "E-001-v1", "decided_by": "林老师", "fingerprint": p["fingerprint"]}, key="app")
    svc.step()

    step1 = runs.get_step(svc.conn, "E-001-v1", 1)
    assert step1 is not None and step1["status"] == "waiting"
    started_at = float(step1["started_at"])
    assert step1["wait_deadline"] == started_at + wait_s

    checkpoints: list[float] = [started_at]
    # Advance in 4-hour chunks, cold-restarting 3 times at +4h, +8h, and +12h
    for _restart_idx in range(3):
        svc.backend.sleep(4 * 3600 - 1.0)
        svc.step()
        assert runs.get_step(svc.conn, "E-001-v1", 1)["status"] == "waiting"  # type: ignore[index]
        t_before = svc.backend.now()
        svc.conn.close()
        del svc

        svc = create_virtual_service(db_path, runs_dir=runs_dir)
        assert svc.backend.now() >= t_before
        checkpoints.append(svc.backend.now())
        svc.startup()
        assert runs.get_step(svc.conn, "E-001-v1", 1)["status"] == "waiting"  # type: ignore[index]

    try:
        # Final 4-hour stretch crosses the 16-hour deadline and executes Step 2 (`pcr_open`)
        svc.backend.sleep(4 * 3600)
        svc.run(max_rounds=5)

        assert runs.get_protocol(svc.conn, "E-001-v1")["status"] == "done"  # type: ignore[index]
        done_step1 = runs.get_step(svc.conn, "E-001-v1", 1)
        done_step2 = runs.get_step(svc.conn, "E-001-v1", 2)
        assert done_step1 is not None and done_step2 is not None
        assert float(done_step1["finished_at"]) - started_at >= wait_s
        assert float(done_step2["started_at"]) >= float(done_step1["finished_at"])
        assert checkpoints == sorted(checkpoints)
        assert checkpoints[-1] - checkpoints[0] >= 12 * 3600
    finally:
        svc.conn.close()
