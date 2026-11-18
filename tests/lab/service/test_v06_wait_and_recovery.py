"""v0.6 Section 16 tests: #5 30s/61s `wait_until` stale freeze & unfreeze, #6 WS=2 recovery."""

from pathlib import Path

from wetlab.lab.service import Service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, records, runs


def _enqueue(svc: Service, kind: str, payload: dict, *, key: str) -> None:
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=key,
            at=svc.backend.now(),
            who="researcher",
            kind=kind,
            payload=payload,
        )


def _start_pcr_wait_protocol(
    svc: Service,
    mission_id: str,
    *,
    hold_s: int = 120,
    max_wait_s: int = 600,
) -> str:
    proto_dict = {
        "name": "PCR 等待状态监控",
        "labware": {"LW-001": "pcr"},
        "steps": [
            {"skill": "pcr_close", "args": {}},
            {
                "skill": "pcr_start_program",
                "args": {
                    "stages": [{"temperature_c": 37, "hold_seconds": hold_s}],
                    "volume_ul": 50,
                    "lid_c": None,
                },
            },
            {
                "wait_until": {
                    "device": "pcr",
                    "state": {"PcrState": "Idle"},
                    "max_seconds": max_wait_s,
                }
            },
            {"skill": "pcr_confirm_finished", "args": {}},
            {"skill": "pcr_open", "args": {}},
        ],
    }
    _enqueue(
        svc,
        "place_labware",
        {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
        key=f"lw-{mission_id}",
    )
    _enqueue(
        svc,
        "new_mission",
        {"mission_id": mission_id, "title": "等待遥测丢失"},
        key=f"m-{mission_id}",
    )
    _enqueue(
        svc,
        "submit_protocol",
        {"mission_id": mission_id, "protocol": proto_dict},
        key=f"s-{mission_id}",
    )
    svc.step()
    p = runs.get_protocol(svc.conn, "E-001-v1")
    assert p is not None
    _enqueue(
        svc,
        "approve",
        {"protocol_id": "E-001-v1", "decided_by": "林老师", "fingerprint": p["fingerprint"]},
        key=f"a-{mission_id}",
    )
    svc.run(max_rounds=3)
    step3 = runs.get_step(svc.conn, "E-001-v1", 3)
    assert step3 is not None and step3["status"] == "waiting"
    return "E-001-v1"


def test_wait_until_30s_stale_recovers_while_61s_stale_freezes_and_resumes_waiting(
    tmp_path: Path,
) -> None:
    """§16.1 #5: (A) 30s stale telemetry stays `waiting` without freezing and finishes normally;
    (B) 61s stale telemetry freezes the island while keeping step `waiting` and deadline intact;
    `unfreeze(step_done)` resumes waiting until target state matches (or fails if deadline passed).
    """
    conn_a = open_db(tmp_path / "wait30.sqlite", mode="virtual")
    sim_a = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    svc_a = Service(sim_a, conn_a, runs_dir=tmp_path / "runs_a")
    try:
        svc_a.startup()
        proto_id_a = _start_pcr_wait_protocol(svc_a, "M-W30", hold_s=60)
        stale_start_a = sim_a.now()
        orig_state_a = sim_a.state

        def stale_30s() -> dict:
            raw = orig_state_a()
            elapsed = sim_a.now() - stale_start_a
            if elapsed <= 30.0:
                raw = {**raw, "PCR002": {**raw["PCR002"], "_age_s": 11.0 + elapsed}}
            return raw

        sim_a.state = stale_30s  # type: ignore[method-assign]
        svc_a.run(max_rounds=30)
        assert island.get_freeze(conn_a)["frozen"] is False
        assert runs.get_step(conn_a, proto_id_a, 3)["status"] == "waiting"  # type: ignore[index]

        svc_a.run(max_rounds=70)
        assert island.get_freeze(conn_a)["frozen"] is False
        assert runs.get_protocol(conn_a, proto_id_a)["status"] == "done"  # type: ignore[index]
    finally:
        conn_a.close()

    conn_b = open_db(tmp_path / "wait61.sqlite", mode="virtual")
    sim_b = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    svc_b = Service(sim_b, conn_b, runs_dir=tmp_path / "runs_b")
    try:
        svc_b.startup()
        proto_id_b = _start_pcr_wait_protocol(svc_b, "M-W61", hold_s=150)
        step3_init = runs.get_step(conn_b, proto_id_b, 3)
        assert step3_init is not None
        deadline_before = step3_init["wait_deadline"]
        stale_flag = [True]
        stale_t0 = sim_b.now()
        orig_state_b = sim_b.state

        def stale_until_cleared() -> dict:
            raw = orig_state_b()
            if stale_flag[0]:
                age = 11.0 + (sim_b.now() - stale_t0)
                return {**raw, "PCR002": {**raw["PCR002"], "_age_s": age}}
            return raw

        sim_b.state = stale_until_cleared  # type: ignore[method-assign]
        svc_b.run(max_rounds=55)

        frz = island.get_freeze(conn_b)
        assert frz["frozen"] is True
        assert frz["reason"] == "等待期间失去 pcr 的状态"
        step3_frozen = runs.get_step(conn_b, proto_id_b, 3)
        assert step3_frozen is not None
        assert step3_frozen["status"] == "waiting"
        assert step3_frozen["wait_deadline"] == deadline_before
        assert runs.get_protocol(conn_b, proto_id_b)["status"] == "running"  # type: ignore[index]
        assert any(e["kind"] == "frozen" for e in inbox.list_events_after(conn_b, 0))

        stale_flag[0] = False
        assert sim_b.state()["PCR002"]["PcrState"] == "Running"
        _enqueue(svc_b, "unfreeze", {"conclusion": "step_done"}, key="unf-wait")
        svc_b.step()
        assert island.get_freeze(conn_b)["frozen"] is False
        assert runs.get_step(conn_b, proto_id_b, 3)["status"] == "waiting"  # type: ignore[index]

        for _ in range(130):
            svc_b.step()
            if runs.get_protocol(conn_b, proto_id_b)["status"] == "done":  # type: ignore[index]
                break
        assert runs.get_protocol(conn_b, proto_id_b)["status"] == "done"  # type: ignore[index]
    finally:
        conn_b.close()


def test_failed_step_clears_working_status_2_via_readonly_probe(tmp_path: Path) -> None:
    """§16.1 #6: When a shaker command fails (`WorkingStatus=2`), step and protocol are marked
    `failed`, followed by a `shaker.GetStatus` read-only operation restoring `WorkingStatus=0`
    so the next protocol's shaker action is NOT blocked.
    """
    conn = open_db(tmp_path / "ws2.sqlite", mode="virtual")
    fault = Fault(
        device="shaker",
        action="StartShakeTimed",
        at_call=1,
        kind="fail",
        message="偶发通信错误",
    )
    sim = SimBackend(Scenario(labware={"LW-001": "shaker"}, faults=[fault]))
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "shaker"},
            key="lw",
        )
        _enqueue(svc, "new_mission", {"mission_id": "M-WS2", "title": "恢复WS2"}, key="m")
        shake_proto = {
            "name": "短时振荡",
            "labware": {"LW-001": "shaker"},
            "steps": [{"skill": "shake", "args": {"seconds": 5, "rpm": 500}}],
        }
        _enqueue(svc, "submit_protocol", {"mission_id": "M-WS2", "protocol": shake_proto}, key="s1")
        svc.step()
        p1 = runs.get_protocol(conn, "E-001-v1")
        assert p1 is not None
        _enqueue(
            svc,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "赵老师", "fingerprint": p1["fingerprint"]},
            key="a1",
        )
        svc.step()

        assert runs.get_step(conn, "E-001-v1", 1)["status"] == "failed"  # type: ignore[index]
        assert runs.get_protocol(conn, "E-001-v1")["status"] == "failed"  # type: ignore[index]

        shaker_ops = records.list_operations(conn, device="shaker")
        fail_idx = next(
            i
            for i, op in enumerate(shaker_ops)
            if op["action"] == "StartShakeTimed" and op["status"] == "failed"
        )
        assert fail_idx + 1 < len(shaker_ops)
        recovery_op = shaker_ops[fail_idx + 1]
        assert recovery_op["action"] == "GetStatus"
        assert recovery_op["status"] == "succeeded"
        assert recovery_op["protocol_id"] is None
        assert sim.state()["T002001"]["WorkingStatus"] == 0

        _enqueue(
            svc,
            "submit_protocol",
            {"experiment_id": "E-001", "protocol": shake_proto},
            key="s2",
        )
        svc.step()
        p2 = runs.get_protocol(conn, "E-001-v2")
        assert p2 is not None and p2["status"] == "submitted"
        _enqueue(
            svc,
            "approve",
            {"protocol_id": "E-001-v2", "decided_by": "赵老师", "fingerprint": p2["fingerprint"]},
            key="a2",
        )
        svc.step()
        assert runs.get_protocol(conn, "E-001-v2")["status"] == "done"  # type: ignore[index]
    finally:
        conn.close()
