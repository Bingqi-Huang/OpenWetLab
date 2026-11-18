"""Tests for steering turntable specification and interlock rules."""

from wetlab.lab import safety
from wetlab.lab.devices.turntable import rules, spec

GATEWAY_OK = {"online": True, "busy_by_others": False}
ARM_IDLE = {
    "RobotMode": "使能空闲",
    "SafetyStatus": "NORMAL",
    "GripperCommStatus": "CONNECTED",
    "WorkingStatus": 0,
    "_age_s": 2,
}
TURNTABLE_IDLE = {
    "States": "Enabled",
    "Angles": "0",
    "WorkingStatus": 0,
    "_age_s": 2,
}


def island(**overrides) -> dict:
    state = {
        "_gateway": dict(GATEWAY_OK),
        "arm": dict(ARM_IDLE),
        "turntable": dict(TURNTABLE_IDLE),
    }
    state.update(overrides)
    return state


def test_turntable_spec():
    assert spec.NAME == "turntable"
    assert spec.CODE == "STEERING_PLATFORM001"
    assert spec.STATE_PROPS == ["States", "Angles"]
    assert spec.KEY_PROPS == ["States", "Angles"]


def test_turntable_not_while_arm_moves():
    moving = dict(ARM_IDLE, RobotMode="运行中")
    assert not safety.check("turntable", "SingleLoop", {"Angle": 90}, island(arm=moving))[0]
    assert safety.check("turntable", "SingleLoop", {"Angle": 90}, island())[0]


def test_turntable_enabled_and_allowed_angles():
    disabled = dict(TURNTABLE_IDLE, States="Disabled")
    assert not safety.check("turntable", "SingleLoop", {"Angle": 90}, island(turntable=disabled))[0]

    for bad_angle in (45, -90, 360, False, True, "90", None):
        assert not safety.check(
            "turntable", "SingleLoop", {"Angle": bad_angle}, island()
        )[0]
    for ok_angle in (0, 90, 180, 270):
        assert safety.check("turntable", "SingleLoop", {"Angle": ok_angle}, island())[0]


def test_ready_for_arm():
    assert rules.ready_for_arm({"turntable": TURNTABLE_IDLE}) is None
    assert rules.ready_for_arm({"turntable": dict(TURNTABLE_IDLE, States="Disabled")}) is not None
