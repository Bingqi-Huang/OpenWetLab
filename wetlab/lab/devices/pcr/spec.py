"""Specification for the Arhat-auto 96 thermal cycler (PCR002 on GW_001)."""

from ..base import ACT, Action, Position, Prop, READ

NAME = "pcr"
LABEL = "PCR仪"
CODE = "PCR002"

ACTIONS = {
    "GetDeviceInfo": Action(READ),
    "GetVersion": Action(READ),
    "GetSerialNumber": Action(READ),
    "GetPartNumber": Action(READ),
    "GetTemperatures": Action(READ),
    "GetHeaters": Action(READ),
    "GetDoor": Action(READ),
    "GetFaults": Action(READ),
    "GetPcrStatus": Action(READ),
    "GetRunControlState": Action(READ),
    "GetFinalHold": Action(READ),
    "ListFiles": Action(READ),
    "ReadAttribute": Action(READ),
    "ReadAttributes": Action(READ),
    "OpenDoor": Action(ACT, seconds=5, moves=True),
    "CloseDoor": Action(ACT, seconds=5, moves=True),
    "SetLidMotor": Action(ACT, seconds=8, moves=True),
    "RunFile": Action(ACT, until_done=True),
    "RunProgram": Action(ACT),
    "StartRun": Action(ACT),
    "PauseRun": Action(ACT, stop=True),
    "StopRun": Action(ACT, stop=True),
    "SetHeater": Action(ACT),
    "SetPlateCapacity": Action(ACT),
    "SetTubeMode": Action(ACT),
}

STATE_PROPS = ["DoorState", "LidMotorState", "PcrState", "FaultText"]
KEY_PROPS = STATE_PROPS + ["PcrRemainingSeconds", "LidTemperature", "HeaterLidEnabled"]
PROPS = {
    "PcrState": Prop("运行状态", values={"Idle": "空闲", "Running": "运行中", "Paused": "已暂停", "SelfTest": "自检", "Fault": "故障"}),
    "PcrRemainingSeconds": Prop("程序剩余", unit="s"),
    "DoorState": Prop("仓门", values={"Closed": "关闭", "Open": "打开", "Moving": "运动中", "SelfTest": "自检", "MotorFault": "电机故障"}),
    "LidMotorState": Prop("热盖", values={"Lifted": "抬起", "Pressed": "下压", "Sealing": "密封中", "Moving": "运动中"}),
    "HeaterLidEnabled": Prop("热盖加热", values={True: "开", False: "关"}),
    "LidTemperature": Prop("热盖温度", unit="℃"),
    "FaultText": Prop("故障"),
}

POSITIONS = {
    "pcr": Position(
        label="PCR仪",
        device="pcr",
        point="2118260185060499457",
        posture=90,
        index="1-1",
    ),
}


def result_ok(action: str, data: dict) -> tuple[bool, str]:
    """Judge PCR RPC result from `deviceData.success` (outer `Data.success` is always true)."""
    dd = data.get("deviceData")
    if isinstance(dd, dict):
        if dd.get("success") is True:
            return True, ""
        err = dd.get("error") or dd.get("message") or data.get("description")
        return False, str(err or f"PCR {action} failed")
    if isinstance(dd, str) and dd:
        return False, dd
    return False, str(data.get("description") or f"PCR {action} returned no deviceData")

