"""Tests for ShakerSim behaviors and physical accident detection."""

import pytest

from wetlab.lab.sim import Scenario, SimBackend


def test_set_clamp_timing_and_elm_state():
    backend = SimBackend()
    t0 = backend.now()
    backend.call("T002001", "SetClamp", {"clamp": True}, 15)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 0.6
    assert backend.state()["T002001"]["ElmState"] == 1

    t1 = backend.now()
    backend.call("T002001", "SetClamp", {"clamp": False}, 15)
    assert pytest.approx(backend.now() - t1, abs=0.01) == 0.6
    assert backend.state()["T002001"]["ElmState"] == 2


def test_start_shake_timed_duration_and_auto_release():
    backend = SimBackend(Scenario(labware={"LW-001": "shaker"}))
    backend.call("T002001", "SetClamp", {"clamp": True}, 15)
    backend.call("T002001", "SetTargetSpeed", {"rpm": 300}, 15)

    t0 = backend.now()
    backend.call("T002001", "StartShakeTimed", {"seconds": 5}, 30)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 8.6

    st = backend.state()["T002001"]
    assert st["ActualSpeed"] == 0
    assert st["ShakeState"] == 1
    assert st["ElmState"] == 2
    assert backend.world.accidents == []
    assert backend.world.history["LW-001"][-1]["seconds"] == 5
    assert backend.world.history["LW-001"][-1]["rpm"] == 300


def test_continuous_start_shake_and_stop_shake():
    backend = SimBackend(Scenario(labware={"LW-001": "shaker"}))
    backend.call("T002001", "SetClamp", {"clamp": True}, 15)
    backend.call("T002001", "SetTargetSpeed", {"rpm": 800}, 15)

    backend.call("T002001", "StartShake", {}, 15)
    st_running = backend.state()["T002001"]
    assert st_running["ShakeState"] == 0
    assert st_running["ActualSpeed"] == 800

    backend.sleep(10.0)
    backend.call("T002001", "StopShake", {}, 20)
    st_stopped = backend.state()["T002001"]
    assert st_stopped["ActualSpeed"] == 0
    assert st_stopped["ShakeState"] == 1
    assert st_stopped["ElmState"] == 2
    assert backend.world.history["LW-001"][-1]["seconds"] == 12


def test_accidents_shaking_unclamped_and_releasing_while_shaking():
    # 1. Starting shake when plate on shaker is not clamped.
    b1 = SimBackend(Scenario(labware={"LW-001": "shaker"}))
    b1.call("T002001", "StartShakeTimed", {"seconds": 5}, 30)
    assert len(b1.world.accidents) == 1

    # 2. Releasing clamp while shaking.
    b2 = SimBackend(Scenario(labware={"LW-001": "shaker"}))
    b2.call("T002001", "SetClamp", {"clamp": True}, 15)
    b2.call("T002001", "StartShake", {}, 15)
    b2.call("T002001", "SetClamp", {"clamp": False}, 15)
    assert len(b2.world.accidents) == 1
