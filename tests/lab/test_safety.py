"""Tests for island-wide safety checks (04 section 5.6), is_stop, device_fault, and #14/#27."""

import ast
import pathlib

from wetlab.lab import safety
from wetlab.lab.devices import ISLAND, KEY_PROPS, NAME_TO_CODE, POSITIONS, rack
from wetlab.lab.devices.base import ACT, Action, Position, READ, skill

GATEWAY_OK = {"online": True, "busy_by_others": False}
PCR_READY = {
    "DoorState": "Closed",
    "LidMotorState": "Pressed",
    "PcrState": "Idle",
    "FaultText": "无故障",
    "WorkingStatus": 0,
    "_age_s": 2,
}
SHAKER_IDLE = {
    "ElmState": 2,
    "ShakeState": 1,
    "ActualSpeed": 0,
    "ErrorCode": 0,
    "WorkingStatus": 0,
    "_age_s": 2,
}
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
RACK_IDLE = {
    "rackState": 0,
    "errorCode": 0,
    "WorkingStatus": 0,
    "_age_s": 2,
}


def island(**overrides) -> dict:
    state = {
        "_gateway": dict(GATEWAY_OK),
        "pcr": dict(PCR_READY),
        "shaker": dict(SHAKER_IDLE),
        "arm": dict(ARM_IDLE),
        "turntable": dict(TURNTABLE_IDLE),
        "rack": dict(RACK_IDLE),
    }
    state.update(overrides)
    return state


def test_island_registry_and_skill_decorator() -> None:
    assert [d.spec.NAME for d in ISLAND] == ["pcr", "shaker", "arm", "turntable", "rack"]
    assert NAME_TO_CODE["rack"] == "T006001"
    assert rack.spec.STATE_PROPS == ["rackState", "errorCode"]
    assert KEY_PROPS["rack"] == ["rackState", "errorCode"]
    assert set(POSITIONS) == {
        "rack:1-2",
        "rack:1-3",
        "pcr",
        "shaker",
        "turntable",
        "arm:home",
    }

    @skill(kind=ACT, label="测试 {x}")
    def sample_skill(lab, x: int):
        """Sample English description."""
        return x + 1

    assert sample_skill.__skill__.name == "sample_skill"
    assert sample_skill(None, 2) == 3

    assert Action(ACT).moves is False
    pos = Position("料架 1-2", "rack", "2120761428308422658", 0, "1-2")
    assert pos.holds_labware is True
    assert pos.device == "rack"
    assert pos.index == "1-2"
    moving_pairs = {
        (dev.spec.NAME, act_name)
        for dev in ISLAND
        for act_name, act in dev.spec.ACTIONS.items()
        if act.moves
    }
    assert moving_pairs == {
        ("pcr", "OpenDoor"),
        ("pcr", "CloseDoor"),
        ("pcr", "SetLidMotor"),
        ("shaker", "SetClamp"),
        ("shaker", "StartShake"),
        ("shaker", "StartShakeTimed"),
        ("shaker", "StopShake"),
        ("shaker", "Home"),
        ("turntable", "SingleLoop"),
        ("arm", "MovMulti"),
    }


def test_allow_list_and_read_only_actions() -> None:
    assert safety.kind("pcr", "GetDoor") == READ
    assert safety.kind("pcr", "OpenDoor") == ACT
    assert safety.kind("pcr", "GetDevices") == READ
    assert safety.kind("pcr", "RawCommand") is None
    assert safety.kind("T007001", "TurnOn") is None

    assert not safety.check("pcr", "RawCommand", {}, island())[0]
    assert not safety.check("arm", "MovL", {}, island())[0]
    assert not safety.check("T007001", "GetStatus", {}, island())[0]
    assert safety.check("pcr", "GetDoor", {}, {})[0]
    assert safety.check("pcr", "GetDevices", {}, {})[0]


def test_is_stop_and_stop_exemptions_on_busy_working_status_2_and_fault() -> None:
    assert safety.is_stop("pcr", "StopRun") is True
    assert safety.is_stop("pcr", "PauseRun") is True
    assert safety.is_stop("pcr", "SetHeater", {"Zone": "Lid", "Enabled": False}) is True
    assert safety.is_stop("pcr", "SetHeater", {"Zone": "Lid", "Enabled": True}) is False
    assert safety.is_stop("shaker", "StopShake") is True
    assert safety.is_stop("arm", "Stop") is True
    assert safety.is_stop("pcr", "OpenDoor") is False

    ext_busy = island(_gateway={"online": True, "busy_by_others": True})
    assert not safety.check("shaker", "SetClamp", {"clamp": True}, ext_busy)[0]
    assert safety.check("shaker", "StopShake", {}, ext_busy)[0]
    assert safety.check("pcr", "SetHeater", {"Zone": "Lid", "Enabled": False}, ext_busy)[0]

    shaker_busy = island(shaker=dict(SHAKER_IDLE, WorkingStatus=1))
    assert not safety.check(
        "pcr", "OpenDoor", {}, dict(shaker_busy, pcr=dict(PCR_READY, LidMotorState="Lifted"))
    )[0]
    assert safety.check("pcr", "SetHeater", {"Zone": "Lid", "Enabled": False}, shaker_busy)[0]

    # WorkingStatus=2 and PcrState=Fault still allow StopRun and turning off lid heater (#1, #11)
    ws2_fault = island(
        pcr=dict(PCR_READY, LidMotorState="Lifted", WorkingStatus=2, PcrState="Fault")
    )
    assert not safety.check("pcr", "OpenDoor", {}, ws2_fault)[0]
    assert safety.check("pcr", "StopRun", {}, ws2_fault)[0]
    assert safety.check("pcr", "SetHeater", {"Zone": "Lid", "Enabled": False}, ws2_fault)[0]


def test_device_fault_and_no_device_name_literals_in_safety_or_protocol() -> None:
    st = island()
    for name in ("pcr", "shaker", "arm", "turntable", "rack"):
        assert safety.device_fault(name, st) is None

    assert safety.device_fault("pcr", island(pcr=dict(PCR_READY, _error="COM9"))) is not None
    assert safety.device_fault("pcr", island(pcr=dict(PCR_READY, WorkingStatus=2))) is not None
    assert safety.device_fault("pcr", island(pcr=dict(PCR_READY, PcrState="Fault"))) is not None
    assert safety.device_fault("pcr", island(pcr=dict(PCR_READY, FaultText="温控异常"))) is not None
    assert safety.device_fault("shaker", island(shaker=dict(SHAKER_IDLE, ErrorCode=4))) is not None
    assert (
        safety.device_fault("arm", island(arm=dict(ARM_IDLE, SafetyStatus="ESTOPPED")))
        is not None
    )

    # #14: safety.py and protocol/*.py must have zero "pcr" / "shaker" string literals
    lab_dir = pathlib.Path(__file__).resolve().parents[2] / "wetlab" / "lab"
    checked_files = [lab_dir / "safety.py", *sorted((lab_dir / "protocol").glob("*.py"))]
    for py_path in checked_files:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        assert "pcr" not in literals, f"'pcr' literal found in {py_path.name}"
        assert "shaker" not in literals, f"'shaker' literal found in {py_path.name}"


def test_single_missing_state_prop_blocks_with_missing_property_reason() -> None:
    """#27: Fresh state missing ONLY ONE `STATE_PROPS` must be blocked due to missing prop."""
    action_for_device = {
        "pcr": ("OpenDoor", {}, dict(PCR_READY, LidMotorState="Lifted")),
        "shaker": ("SetClamp", {"clamp": True}, dict(SHAKER_IDLE)),
        "arm": (
            "MovMulti",
            {
                "Tasks": [{"PositionId": 2120809646631186433, "ClawSwitch": 1}],
                "Material": {"Speed": "低速"},
            },
            dict(ARM_IDLE),
        ),
        "turntable": ("SingleLoop", {"Angle": 90}, dict(TURNTABLE_IDLE)),
    }
    for dev_name, (act, params, valid_props) in action_for_device.items():
        assert safety.check(dev_name, act, params, island(**{dev_name: dict(valid_props)}))[0]
        dev_mod = next(d for d in ISLAND if d.spec.NAME == dev_name)
        for prop in dev_mod.spec.STATE_PROPS:
            one_missing = dict(valid_props)
            del one_missing[prop]
            ok, reason = safety.check(dev_name, act, params, island(**{dev_name: one_missing}))
            assert not ok, f"{dev_name} missing {prop} must be blocked"
            assert "missing state properties" in reason
            assert prop in reason


def test_gateway_offline_error_stale_and_cross_device_handoff() -> None:
    offline = island(_gateway={"online": False, "busy_by_others": False})
    assert safety.check("shaker", "SetClamp", {"clamp": True}, offline) == (
        False,
        "GATEWAY_OFFLINE",
    )
    assert safety.check("shaker", "GetStatus", {}, offline)[0]

    with_err = island(pcr=dict(PCR_READY, LidMotorState="Lifted", _error="COM9 busy"))
    assert not safety.check("pcr", "OpenDoor", {}, with_err)[0]
    assert not safety.check("pcr", "StopRun", {}, with_err)[0]

    stale = island(pcr=dict(PCR_READY, LidMotorState="Lifted", _age_s=30))
    assert not safety.check("pcr", "OpenDoor", {}, stale)[0]

    to_pcr = {
        "Tasks": [
            {"PositionId": 2118246861233086466, "ClawSwitch": 0},
            {"PositionId": 2118260185060499457, "ClawSwitch": 1},
        ],
        "Material": {"Index": "1-3", "Speed": "低速"},
    }
    assert not safety.check("arm", "MovMulti", to_pcr, island(pcr=PCR_READY))[0]
    pcr_open = dict(PCR_READY, DoorState="Open", LidMotorState="Lifted", PcrState="Idle")
    assert safety.check("arm", "MovMulti", to_pcr, island(pcr=pcr_open))[0]
    assert not safety.check(
        "arm", "MovMulti", to_pcr, island(pcr=dict(pcr_open, FaultText="故障"))
    )[0]
    assert not safety.check("arm", "MovMulti", to_pcr, island(pcr=dict(pcr_open, _age_s=25)))[0]

    washer = {
        "Tasks": [{"PositionId": 2122225115687583746, "ClawSwitch": 1}],
        "Material": {"Speed": "低速"},
    }
    assert not safety.check("arm", "MovMulti", washer, island())[0]
