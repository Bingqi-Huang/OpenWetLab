"""Tests for robot arm specification, local interlock rules, and touched-position extraction."""

from wetlab.lab.devices.arm import rules, spec

ARM_IDLE = {
    "RobotMode": "使能空闲",
    "SafetyStatus": "NORMAL",
    "GripperCommStatus": "CONNECTED",
    "WorkingStatus": 0,
    "_age_s": 2,
}


def test_arm_spec():
    assert spec.NAME == "arm"
    assert spec.CODE == "TYPE_ROBOT_ARM001"
    assert spec.POSITIONS == {}
    assert spec.HOME.point == "2120809646631186433"
    assert spec.HOME.label == "机械臂原位"
    assert spec.ACTIONS["MovMulti"].result_topic == "motion/response"
    assert spec.ACTIONS["MovMulti"].seconds == 60


def test_arm_must_be_idle_normal_and_gripper_connected():
    valid = {
        "Tasks": [{"PositionId": 2120809646631186433, "ClawSwitch": 1}],
        "Material": {"Speed": "低速"},
    }
    assert rules.check("MovMulti", valid, {"arm": ARM_IDLE}) is None
    assert (
        rules.check("MovMulti", valid, {"arm": dict(ARM_IDLE, RobotMode="运行中")})
        is not None
    )
    assert (
        rules.check("MovMulti", valid, {"arm": dict(ARM_IDLE, SafetyStatus="ESTOPPED")})
        is not None
    )
    assert (
        rules.check("MovMulti", valid, {"arm": dict(ARM_IDLE, GripperCommStatus="DISCONNECTED")})
        is not None
    )


def test_movmulti_tasks_and_speed_validation():
    assert rules.check("MovMulti", {"Tasks": [], "Material": {"Speed": "低速"}}, {"arm": ARM_IDLE}) is not None
    assert rules.check("MovMulti", {"Tasks": [{}], "Material": {"Speed": "低速"}}, {"arm": ARM_IDLE}) is not None
    assert (
        rules.check(
            "MovMulti",
            {
                "Tasks": [{"PositionId": 2120809646631186433, "ClawSwitch": 1}],
                "Material": {"Speed": "高速"},
            },
            {"arm": ARM_IDLE},
        )
        is not None
    )
    assert rules.check("Stop", {}, {"arm": ARM_IDLE}) is None


def test_positions_touched():
    params = {
        "Tasks": [
            {"PositionId": 2118246861233086466, "ClawSwitch": 0},
            {"PositionId": 2118260185060499457, "ClawSwitch": 1},
        ]
    }
    assert rules.positions_touched("MovMulti", params) == [
        "2118246861233086466",
        "2118260185060499457",
    ]
    assert rules.positions_touched("Stop", params) == []
