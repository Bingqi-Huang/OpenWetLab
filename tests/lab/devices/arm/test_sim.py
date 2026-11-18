"""Tests for ArmSim motion timing, plate transfer truth, and handoff accidents (#28)."""

import pytest

from wetlab.lab.sim import Scenario, SimBackend

RACK_LOW = 2118246861233086466
PCR_POINT = 2118260185060499457
SHAKER_POINT = 2120763995201499138


def _transfer_params(from_pt: int, to_pt: int) -> dict:
    return {
        "Tasks": [
            {"PositionId": from_pt, "ClawSwitch": 0},
            {"PositionId": to_pt, "ClawSwitch": 1},
        ],
        "Material": {"Index": "1-3", "Speed": "低速"},
    }


def test_movmulti_timing_running_mode_and_plate_transfer() -> None:
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted", "PcrState": "Idle"}},
        )
    )
    arm_sim = backend.world.device("arm")
    t0 = backend.now()
    proc = backend.world.env.process(arm_sim.MovMulti(_transfer_params(RACK_LOW, PCR_POINT)))
    backend.sleep(10.0)
    assert backend.state()["TYPE_ROBOT_ARM001"]["RobotMode"] == "运行中"

    backend.world.env.run(until=proc)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 41.0
    assert backend.state()["TYPE_ROBOT_ARM001"]["RobotMode"] == "使能空闲"
    assert backend.world.plates["LW-001"] == "pcr"
    assert backend.world.history["LW-001"][-1]["to"] == "pcr"
    assert backend.world.accidents == []


def test_accident_arm_into_pcr_when_door_closed() -> None:
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={"pcr": {"DoorState": "Closed", "LidMotorState": "Lifted", "PcrState": "Idle"}},
        )
    )
    backend.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, PCR_POINT), 90)
    assert len(backend.world.accidents) == 1
    assert "DoorState=Closed" in backend.world.accidents[0]["what"]


def test_accident_arm_into_pcr_when_lid_not_lifted() -> None:
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={"pcr": {"DoorState": "Open", "LidMotorState": "Pressed", "PcrState": "Idle"}},
        )
    )
    backend.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, PCR_POINT), 90)
    assert len(backend.world.accidents) == 1
    assert "LidMotorState=Pressed" in backend.world.accidents[0]["what"]


def test_accident_arm_into_pcr_when_program_running() -> None:
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={
                "pcr": {
                    "DoorState": "Open",
                    "LidMotorState": "Lifted",
                    "PcrState": "Running",
                    "PcrRemainingSeconds": 120,
                }
            },
        )
    )
    backend.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, PCR_POINT), 90)
    assert len(backend.world.accidents) == 1
    assert "PcrState=Running" in backend.world.accidents[0]["what"]


def test_accident_arm_into_shaker_when_shaking() -> None:
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={"shaker": {"ActualSpeed": 300, "ShakeState": 0, "ElmState": 2}},
        )
    )
    backend.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, SHAKER_POINT), 90)
    assert len(backend.world.accidents) == 1
    assert "ShakeState=0" in backend.world.accidents[0]["what"]


def test_accident_arm_into_shaker_when_clamped() -> None:
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={"shaker": {"ActualSpeed": 0, "ShakeState": 1, "ElmState": 1}},
        )
    )
    backend.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, SHAKER_POINT), 90)
    assert len(backend.world.accidents) == 1
    assert "ElmState=1" in backend.world.accidents[0]["what"]


def test_accidents_pick_from_empty_and_place_onto_occupied() -> None:
    empty = SimBackend()
    empty.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, SHAKER_POINT), 90)
    assert len(empty.world.accidents) == 1

    occupied = SimBackend(Scenario(labware={"LW-001": "rack:1-3", "LW-002": "shaker"}))
    occupied.call("TYPE_ROBOT_ARM001", "MovMulti", _transfer_params(RACK_LOW, SHAKER_POINT), 90)
    assert len(occupied.world.accidents) == 1
