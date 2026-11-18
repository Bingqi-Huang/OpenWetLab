"""Tests for TurntableSim string Angles, 1-2s timing, fault-after-act, and arm collision."""

import pytest

from wetlab.lab.errors import DeviceFailed
from wetlab.lab.sim import Fault, Scenario, SimBackend


def test_single_loop_sets_angles_string_and_takes_about_1_to_2_seconds():
    backend = SimBackend()
    assert backend.state()["STEERING_PLATFORM001"]["Angles"] == "0"
    assert "Angle" not in backend.state()["STEERING_PLATFORM001"]

    t0 = backend.now()
    res = backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 90}, 15)
    elapsed = backend.now() - t0
    assert 1.0 <= elapsed <= 2.0
    assert res["success"] is True
    st = backend.state()["STEERING_PLATFORM001"]
    assert st["Angles"] == "90"
    assert isinstance(st["Angles"], str)
    assert backend.world.accidents == []


def test_single_loop_injected_failed_after_act_rotates_to_target_then_raises():
    scen = Scenario(
        faults=[
            Fault(
                device="turntable",
                action="SingleLoop",
                nth=1,
                kind="failed_after_act",
                message="转向命令未收到电机应答（串口无响应）",
            )
        ]
    )
    backend = SimBackend(scen)
    t0 = backend.now()
    with pytest.raises(DeviceFailed, match="未收到电机应答"):
        backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 90}, 15)
    assert 1.0 <= (backend.now() - t0) <= 2.0
    st = backend.state()["STEERING_PLATFORM001"]
    assert st["Angles"] == "90"
    assert st["WorkingStatus"] == 2


def test_accident_turntable_rotates_while_arm_moving():
    backend = SimBackend(Scenario(devices={"arm": {"RobotMode": "运行中"}}))
    backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 180}, 15)
    assert len(backend.world.accidents) == 1

    # Also verify during live concurrent SimPy arm motion.
    concurrent = SimBackend(Scenario(labware={"LW-001": "rack:1-3"}))
    arm_sim = concurrent.world.device("arm")
    tt_sim = concurrent.world.device("turntable")
    arm_proc = concurrent.world.env.process(
        arm_sim.MovMulti(
            {
                "Tasks": [
                    {"PositionId": 2118246861233086466, "ClawSwitch": 0},
                    {"PositionId": 2120763995201499138, "ClawSwitch": 1},
                ]
            }
        )
    )
    concurrent.sleep(5.0)
    concurrent.world.env.run(until=concurrent.world.env.process(tt_sim.SingleLoop({"Angle": 90})))
    concurrent.world.env.run(until=arm_proc)
    assert len(concurrent.world.accidents) == 1
