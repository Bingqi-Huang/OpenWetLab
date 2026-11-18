"""Tests for shaker skills: shake, shake_start, and shake_stop."""

from wetlab.lab import registry
from wetlab.lab.lab import Lab
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.store import open_db, records


def _make_lab() -> tuple[Lab, SimBackend]:
    conn = open_db(":memory:")
    backend = SimBackend(Scenario(labware={"LW-001": "shaker"}))
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "96_plate", "shaker")
    return lab, backend


def test_shake_succeeds_and_blocks_on_61s_or_error():
    lab, backend = _make_lab()
    res = registry.call(lab, "shake", seconds=10, rpm=600)
    assert res["ok"] is True
    assert res["state_after"]["shaker"]["ActualSpeed"] == 0
    assert res["state_after"]["shaker"]["ElmState"] == 2
    assert lab.materials.history("LW-001")[-1]["what"] == "shaken"
    assert backend.world.accidents == []

    # 61 seconds exceeds skill limit -> blocked / INVALID_ARGUMENT, zero commands sent.
    before_count = len(records.list_operations(lab.conn))
    over = registry.call(lab, "shake", seconds=61, rpm=600)
    assert over["status"] == "blocked"
    assert over["error"]["code"] == "INVALID_ARGUMENT"
    assert over["operation_ids"] == []
    assert len(records.list_operations(lab.conn)) == before_count

    # Device ErrorCode blocks shake.
    backend.world.device("shaker").props["ErrorCode"] = 2
    blocked = registry.call(lab, "shake", seconds=5, rpm=600)
    assert blocked["status"] == "blocked"
    assert backend.world.accidents == []


def test_shake_start_and_shake_stop_normal_and_early_stop():
    lab, backend = _make_lab()
    started = registry.call(lab, "shake_start", rpm=1000)
    assert started["ok"] is True
    assert started["state_after"]["shaker"]["ShakeState"] == 0

    # Calling shake_start again while already spinning is blocked by interlocks.
    again = registry.call(lab, "shake_start", rpm=1000)
    assert again["status"] == "blocked"
    assert backend.world.accidents == []

    backend.sleep(25.0)
    stopped = registry.call(lab, "shake_stop")
    assert stopped["ok"] is True
    assert stopped["result"]["seconds"] >= 25.0
    last_ev = lab.materials.history("LW-001")[-1]
    assert last_ev["what"] == "shake_stopped"
    assert last_ev["detail"]["seconds"] >= 25.0
    assert last_ev["detail"]["ended_early"] is False

    # Start again, then stop externally before shake_stop -> failed / SHAKE_ENDED_EARLY + records duration.
    registry.call(lab, "shake_start", rpm=800)
    backend.sleep(12.0)
    backend.call("T002001", "StopShake", {}, 15)
    backend.sleep(30.0)

    early = registry.call(lab, "shake_stop")
    assert early["ok"] is False
    assert early["status"] == "failed"
    assert early["error"]["code"] == "SHAKE_ENDED_EARLY"
    ev_early = lab.materials.history("LW-001")[-1]
    assert ev_early["what"] == "shake_stopped"
    assert ev_early["detail"]["ended_early"] is True

    # Offline gateway blocks shake_stop.
    backend.gateway_online = False
    assert registry.call(lab, "shake_stop")["status"] == "blocked"
