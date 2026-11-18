"""Specification for the NAYO MCHS thermo-shaker module (T002001 on GW_001)."""

from ..base import ACT, Action, Position, Prop, READ

NAME = "shaker"
LABEL = "恒温振荡模组"
CODE = "T002001"

ACTIONS = {
    "GetStatus": Action(READ),
    "GetErrors": Action(READ),
    "GetSerialNumber": Action(READ),
    "SetTargetSpeed": Action(ACT),
    "SetDirection": Action(ACT),
    "StartShake": Action(ACT),
    "StartShakeTimed": Action(ACT, until_done=True),
    "StopShake": Action(ACT, seconds=10, stop=True),
    "Home": Action(ACT),
    "SetClamp": Action(ACT, seconds=2),
    "SetTargetTemperature": Action(ACT),
    "SetHoldTime": Action(ACT),
    "SetTemperatureControl": Action(ACT),
    "EmergencyStop": Action(ACT, stop=True),
    "ClearError": Action(ACT),
}

STATE_PROPS = ["ElmState", "ShakeState", "ActualSpeed", "ErrorCode"]
KEY_PROPS = STATE_PROPS + [
    "TargetSpeed",
    "BlockTemperature",
    "TargetTemperature",
    "TempControllerStateText",
]
PROPS = {
    "ShakeState": Prop("振荡", values={0: "振荡中", 1: "已停止"}),
    "ActualSpeed": Prop("实际转速", unit="rpm"),
    "TargetSpeed": Prop("设定转速", unit="rpm"),
    "ElmState": Prop("夹紧", values={1: "夹紧", 2: "松开"}),
    "BlockTemperature": Prop("热块温度", unit="℃"),
    "TargetTemperature": Prop("目标温度", unit="℃"),
    "TempControllerStateText": Prop("温控"),
    "ErrorCode": Prop("错误码", values={0: "无"}),
}

POSITIONS = {
    "shaker": Position(label="恒温振荡模组", point="2120763995201499138", posture=0),
}


def result_ok(action: str, data: dict) -> tuple[bool, str]:
    """Judge shaker RPC result: actions check `deviceData.ok`; read queries return status fields."""
    dd = data.get("deviceData")
    if isinstance(dd, str):
        return False, dd or f"shaker {action} failed"
    act = ACTIONS.get(action)
    if act is not None and act.kind == READ:
        if not isinstance(dd, dict):
            return False, str(data.get("description") or f"shaker {action} returned no deviceData")
        if dd.get("ok") is False or data.get("success") is False:
            err = dd.get("error") or dd.get("message") or data.get("description")
            return False, str(err or f"shaker {action} failed")
        return True, ""
    if isinstance(dd, dict) and dd.get("ok") is True:
        return True, ""
    err = (
        (dd.get("error") or dd.get("message") or dd.get("finishReason"))
        if isinstance(dd, dict)
        else None
    )
    return False, str(err or data.get("description") or f"shaker {action} failed")

