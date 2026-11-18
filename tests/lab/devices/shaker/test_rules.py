"""Tests for thermo-shaker device specification and interlock rules."""

from wetlab.lab import safety
from wetlab.lab.devices.shaker import rules, spec

GATEWAY_OK = {"online": True, "busy_by_others": False}
SHAKER_IDLE = {
    "ElmState": 2,
    "ShakeState": 1,
    "ActualSpeed": 0,
    "ErrorCode": 0,
    "WorkingStatus": 0,
    "_age_s": 2,
}


def island(**overrides) -> dict:
    state = {"_gateway": dict(GATEWAY_OK), "shaker": dict(SHAKER_IDLE)}
    state.update(overrides)
    return state


def test_shaker_spec_props():
    assert spec.NAME == "shaker"
    assert spec.CODE == "T002001"
    assert spec.STATE_PROPS == ["ElmState", "ShakeState", "ActualSpeed", "ErrorCode"]
    for prop in ("BlockTemperature", "TargetTemperature", "TempControllerStateText", "TargetSpeed"):
        assert prop in spec.KEY_PROPS


def test_must_clamp_before_shaking():
    assert not safety.check("shaker", "StartShakeTimed", {"seconds": 5}, island())[0]
    assert not safety.check("shaker", "StartShake", {}, island())[0]

    clamped = dict(SHAKER_IDLE, ElmState=1)
    assert safety.check("shaker", "StartShakeTimed", {"seconds": 5}, island(shaker=clamped))[0]
    assert safety.check("shaker", "StartShake", {}, island(shaker=clamped))[0]

    spinning = dict(clamped, ActualSpeed=300, ShakeState=0)
    assert not safety.check(
        "shaker", "StartShakeTimed", {"seconds": 5}, island(shaker=spinning)
    )[0]
    assert not safety.check(
        "shaker", "StartShakeTimed", {"seconds": 0}, island(shaker=clamped)
    )[0]
    assert not safety.check(
        "shaker", "StartShakeTimed", {"seconds": True}, island(shaker=clamped)
    )[0]


def test_do_not_release_clamp_while_shaking():
    spinning = dict(SHAKER_IDLE, ElmState=1, ActualSpeed=300, ShakeState=0)
    assert not safety.check("shaker", "SetClamp", {"clamp": False}, island(shaker=spinning))[0]
    assert not safety.check("shaker", "SetClamp", {}, island())[0]
    assert safety.check("shaker", "SetClamp", {"clamp": True}, island())[0]


def test_speed_and_temperature_ranges():
    assert not safety.check("shaker", "SetTargetSpeed", {"rpm": 5000}, island())[0]
    assert not safety.check("shaker", "SetTargetSpeed", {"rpm": 199}, island())[0]
    assert not safety.check("shaker", "SetTargetSpeed", {"rpm": True}, island())[0]
    assert safety.check("shaker", "SetTargetSpeed", {"rpm": 300}, island())[0]

    assert not safety.check("shaker", "SetTargetTemperature", {"celsius": 150}, island())[0]
    assert not safety.check("shaker", "SetTargetTemperature", {"celsius": -20}, island())[0]
    assert not safety.check("shaker", "SetTargetTemperature", {"celsius": False}, island())[0]
    assert safety.check("shaker", "SetTargetTemperature", {"celsius": 37.0}, island())[0]


def test_error_code_blocks_normal_actions():
    errored = dict(SHAKER_IDLE, ErrorCode=4)
    assert not safety.check("shaker", "SetClamp", {"clamp": True}, island(shaker=errored))[0]
    assert safety.check("shaker", "ClearError", {}, island(shaker=errored))[0]
    assert safety.check("shaker", "StopShake", {}, island(shaker=errored))[0]


def test_ready_for_arm():
    assert rules.ready_for_arm({"shaker": SHAKER_IDLE}) is None
    assert rules.ready_for_arm({"shaker": dict(SHAKER_IDLE, ElmState=1)}) is not None
    assert rules.ready_for_arm({"shaker": dict(SHAKER_IDLE, ShakeState=0)}) is not None
    assert rules.ready_for_arm({"shaker": dict(SHAKER_IDLE, ActualSpeed=100)}) is not None
