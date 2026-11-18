"""Specification for the Huayan 6-axis robot arm (TYPE_ROBOT_ARM001 on GW_001)."""

from ..base import ACT, Action, Position, Prop, READ

NAME = "arm"
LABEL = "机械臂"
CODE = "TYPE_ROBOT_ARM001"

ACTIONS = {
    "GetPose": Action(READ),
    "GetAngle": Action(READ),
    "RobotMode": Action(READ),
    "GripperStatus": Action(READ),
    "GripperGetPosition": Action(READ),
    "CheckReachable": Action(READ),
    "MovMulti": Action(ACT, seconds=60, result_topic="motion/response", moves=True),
    "Pause": Action(ACT, stop=True),
    "Continue": Action(ACT),
    "Stop": Action(ACT, stop=True),
    "EmergencyStop": Action(ACT, stop=True),
}

STATE_PROPS = ["RobotMode", "SafetyStatus", "GripperCommStatus"]
KEY_PROPS = list(STATE_PROPS)
PROPS = {
    "RobotMode": Prop("运行状态"),
    "SafetyStatus": Prop("安全状态", values={"NORMAL": "正常"}),
    "GripperCommStatus": Prop("夹爪通讯", values={"CONNECTED": "已连接", "DISCONNECTED": "未连接"}),
}

POSITIONS: dict[str, Position] = {}
HOME = Position(
    label="机械臂原位",
    device="arm",
    point="2120809646631186433",
    posture=0,
    index="1-1",
    holds_labware=False,
)


def result_ok(action: str, data: dict) -> tuple[bool, str]:
    """Judge arm RPC result: `motion/response` checks `code == '0'`; device RPC checks `Data.success`."""
    if "code" in data:
        if str(data["code"]) == "0":
            return True, ""
        return False, str(data.get("msg") or data.get("description") or f"motion code={data['code']}")
    dd = data.get("deviceData")
    if isinstance(dd, str):
        return False, dd or f"arm {action} failed"
    if data.get("success") is not True:
        err = dd.get("error") if isinstance(dd, dict) else None
        return False, str(err or data.get("description") or f"arm {action} failed")
    if isinstance(dd, dict) and dd.get("success") is False:
        err = dd.get("error") or dd.get("message") or data.get("description")
        return False, str(err or f"arm {action} failed")
    return True, ""

