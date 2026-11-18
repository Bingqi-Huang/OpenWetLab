"""Tests for PCR device specification and interlock rules."""

import json

from wetlab.lab import safety
from wetlab.lab.devices.pcr import rules, spec

GATEWAY_OK = {"online": True, "busy_by_others": False}
PCR_READY = {
    "DoorState": "Closed",
    "LidMotorState": "Pressed",
    "PcrState": "Idle",
    "FaultText": "无故障",
    "WorkingStatus": 0,
    "_age_s": 2,
}


def island(**overrides) -> dict:
    state = {"_gateway": dict(GATEWAY_OK), "pcr": dict(PCR_READY)}
    state.update(overrides)
    return state


def test_pcr_spec_props_and_positions():
    assert spec.NAME == "pcr"
    assert spec.CODE == "PCR002"
    assert spec.STATE_PROPS == ["DoorState", "LidMotorState", "PcrState", "FaultText"]
    assert spec.KEY_PROPS == [
        "DoorState",
        "LidMotorState",
        "PcrState",
        "FaultText",
        "PcrRemainingSeconds",
        "LidTemperature",
        "HeaterLidEnabled",
    ]
    assert not hasattr(spec, "POSITIONS")
    moving = {name for name, act in spec.ACTIONS.items() if act.moves}
    assert moving == {"OpenDoor", "CloseDoor", "SetLidMotor"}


def test_heating_needs_closed_door_and_pressed_lid():
    assert safety.check("pcr", "RunFile", {"Folder": "Monad", "File": "TAQ"}, island())[0]
    assert not safety.check(
        "pcr", "RunFile", {}, island(pcr=dict(PCR_READY, LidMotorState="Lifted"))
    )[0]
    assert not safety.check(
        "pcr", "RunFile", {}, island(pcr=dict(PCR_READY, DoorState="Open"))
    )[0]
    assert not safety.check(
        "pcr", "RunFile", {}, island(pcr=dict(PCR_READY, PcrState="Running"))
    )[0]
    assert safety.check(
        "pcr", "StartRun", {}, island(pcr=dict(PCR_READY, PcrState="Paused"))
    )[0]


def test_runprogram_follows_official_limits():
    ok = json.dumps([{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 30, "Cycles": 1}])
    assert safety.check("pcr", "RunProgram", {"Steps": ok}, island())[0]

    pcr_loop = [
        {"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 180, "Cycles": 1},
        {"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 15, "Cycles": 1},
        {"ZoneATarget": 58, "ZoneCTarget": 58, "HoldSeconds": 30, "Cycles": 1},
        {"ZoneATarget": 72, "ZoneCTarget": 72, "HoldSeconds": 45, "NextStep": 2, "Cycles": 35},
    ]
    assert safety.check("pcr", "RunProgram", {"Steps": json.dumps(pcr_loop)}, island())[0]

    nested_loop = [
        {"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 15, "Cycles": 1},
        {"ZoneATarget": 60, "ZoneCTarget": 60, "HoldSeconds": 10, "Cycles": 1},
        {"ZoneATarget": 72, "ZoneCTarget": 72, "HoldSeconds": 10, "NextStep": 2, "Cycles": 3},
        {"ZoneATarget": 72, "ZoneCTarget": 72, "HoldSeconds": 10, "NextStep": 1, "Cycles": 5},
    ]
    too_many = [{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 10}] * 31
    bad = {
        "too hot": [{"ZoneATarget": 137, "ZoneCTarget": 137, "HoldSeconds": 30}],
        "below 4 C": [{"ZoneATarget": -2, "ZoneCTarget": -2, "HoldSeconds": 30}],
        "gradient": [{"ZoneATarget": 50, "ZoneCTarget": 90, "HoldSeconds": 30}],
        "forward jump": [
            {"ZoneATarget": 50, "ZoneCTarget": 50, "HoldSeconds": 30, "NextStep": 2, "Cycles": 3}
        ],
        "ramp leaves range": [
            {
                "ZoneATarget": 95,
                "ZoneCTarget": 95,
                "HoldSeconds": 30,
                "RampPerCycleCelsius": 1,
                "Cycles": 10,
            }
        ],
        "nested loops": nested_loop,
        "too many steps": too_many,
        "empty": [],
    }
    for why, steps in bad.items():
        assert not safety.check(
            "pcr", "RunProgram", {"Steps": json.dumps(steps)}, island()
        )[0], why
    assert not safety.check("pcr", "RunProgram", {"Steps": "{bad-json"}, island())[0]


def test_touch_screen_lock_and_fault_block_actions():
    locked = dict(PCR_READY, LidMotorState="Lifted", InterfaceLock=1)
    assert not safety.check("pcr", "OpenDoor", {}, island(pcr=locked))[0]

    faulted = dict(PCR_READY, LidMotorState="Lifted", FaultText="A区温度故障")
    assert not safety.check("pcr", "OpenDoor", {}, island(pcr=faulted))[0]
    assert safety.check("pcr", "StopRun", {}, island(pcr=faulted))[0]
    assert safety.check(
        "pcr", "SetHeater", {"Zone": "Lid", "Enabled": False}, island(pcr=faulted)
    )[0]


def test_open_door_needs_lifted_lid_and_idle():
    assert not safety.check("pcr", "OpenDoor", {}, island())[0]
    assert not safety.check(
        "pcr", "OpenDoor", {}, island(pcr=dict(PCR_READY, LidMotorState="Lifted", PcrState="Running"))
    )[0]
    assert safety.check(
        "pcr", "OpenDoor", {}, island(pcr=dict(PCR_READY, LidMotorState="Lifted"))
    )[0]


def test_close_door_must_skip_lid_wait():
    lifted = dict(PCR_READY, DoorState="Open", LidMotorState="Lifted")
    assert not safety.check("pcr", "CloseDoor", {"TimeoutMs": 60000}, island(pcr=lifted))[0]
    assert not safety.check("pcr", "CloseDoor", {"WaitLid": True}, island(pcr=lifted))[0]
    assert safety.check("pcr", "CloseDoor", {"WaitLid": False}, island(pcr=lifted))[0]


def test_lid_action_must_be_explicit_and_safe():
    assert not safety.check("pcr", "SetLidMotor", {}, island())[0]
    assert not safety.check("pcr", "SetLidMotor", {"Action": "Seal"}, island())[0]
    assert safety.check("pcr", "SetLidMotor", {"Action": "Lift"}, island())[0]
    assert not safety.check(
        "pcr", "SetLidMotor", {"Action": "Lift"}, island(pcr=dict(PCR_READY, PcrState="Running"))
    )[0]
    assert not safety.check(
        "pcr", "SetLidMotor", {"Action": "Press"}, island(pcr=dict(PCR_READY, DoorState="Open"))
    )[0]
    assert safety.check(
        "pcr", "SetLidMotor", {"Action": "Press"}, island(pcr=dict(PCR_READY, LidMotorState="Lifted"))
    )[0]


def test_set_heater_zone_must_be_lid():
    for bad_zone in ("A", "B", "C", "", None):
        ok, _ = safety.check(
            "pcr",
            "SetHeater",
            {"Zone": bad_zone, "Enabled": True, "TargetCelsius": 105},
            island(),
        )
        assert not ok, f"Zone={bad_zone!r} should be rejected"
    assert safety.check(
        "pcr", "SetHeater", {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105}, island()
    )[0]


def test_set_heater_on_requires_closed_door_and_pressed_lid():
    open_door = dict(PCR_READY, DoorState="Open", LidMotorState="Lifted")
    assert not safety.check(
        "pcr",
        "SetHeater",
        {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105},
        island(pcr=open_door),
    )[0]
    lifted = dict(PCR_READY, DoorState="Closed", LidMotorState="Lifted")
    assert not safety.check(
        "pcr",
        "SetHeater",
        {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105},
        island(pcr=lifted),
    )[0]
    # Turning off lid heater is allowed even with door open / lid lifted.
    assert safety.check(
        "pcr", "SetHeater", {"Zone": "Lid", "Enabled": False}, island(pcr=open_door)
    )[0]


def test_set_heater_target_temperature_range():
    for bad_temp in (29.9, 110.1, -10, 200, True, None):
        ok, _ = safety.check(
            "pcr",
            "SetHeater",
            {"Zone": "Lid", "Enabled": True, "TargetCelsius": bad_temp},
            island(),
        )
        assert not ok, f"TargetCelsius={bad_temp!r} should be rejected"
    for valid_temp in (30, 105, 110.0):
        assert safety.check(
            "pcr",
            "SetHeater",
            {"Zone": "Lid", "Enabled": True, "TargetCelsius": valid_temp},
            island(),
        )[0]
    assert not safety.check("pcr", "SetHeater", {"Zone": "Lid", "Enabled": 1}, island())[0]


def test_set_plate_capacity_range():
    for bad_ul in (0, -1, 256, 50.5, True, None):
        assert not safety.check(
            "pcr", "SetPlateCapacity", {"Microliters": bad_ul}, island()
        )[0]
    for ok_ul in (1, 50, 255):
        assert safety.check("pcr", "SetPlateCapacity", {"Microliters": ok_ul}, island())[0]


def test_set_tube_mode_must_be_block():
    for bad_mode in ("Tube", "tube", "", None):
        assert not safety.check("pcr", "SetTubeMode", {"Mode": bad_mode}, island())[0]
    assert safety.check("pcr", "SetTubeMode", {"Mode": "Block"}, island())[0]


def test_ready_for_arm():
    assert rules.ready_for_arm({"pcr": PCR_READY}) is not None
    assert rules.ready_for_arm({"pcr": dict(PCR_READY, DoorState="Open")}) is not None
    assert (
        rules.ready_for_arm(
            {"pcr": dict(PCR_READY, DoorState="Open", LidMotorState="Lifted", PcrState="Running")}
        )
        is not None
    )
    assert (
        rules.ready_for_arm(
            {"pcr": dict(PCR_READY, DoorState="Open", LidMotorState="Lifted", PcrState="Idle")}
        )
        is None
    )
