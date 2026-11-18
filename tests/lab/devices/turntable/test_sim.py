"""Tests for TurntableSim string Angles behavior and moving-arm collision detection."""

from wetlab.lab.sim import Scenario, SimBackend


def test_single_loop_sets_angles_string():
    backend = SimBackend()
    assert backend.state()["STEERING_PLATFORM001"]["Angles"] == "0"
    assert "Angle" not in backend.state()["STEERING_PLATFORM001"]

    backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 90}, 15)
    st = backend.state()["STEERING_PLATFORM001"]
    assert st["Angles"] == "90"
    assert isinstance(st["Angles"], str)
    assert backend.world.accidents == []


def test_accident_turntable_rotates_while_arm_moving():
    backend = SimBackend(Scenario(devices={"arm": {"RobotMode": "运行中"}}))
    backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 180}, 15)
    assert len(backend.world.accidents) == 1

    # Also verify during live concurrent SimPy arm motion.
    concurrent = SimBackend(Scenario(labware={"LW-001": "rack:low"}))
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
