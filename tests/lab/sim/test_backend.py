"""Tests for SimBackend protocol compliance, scenario rules, fault injection, and determinism."""

import json

import pytest

from wetlab.lab import safety
from wetlab.lab.devices import CODE_TO_NAME, ISLAND, NAME_TO_CODE
from wetlab.lab.errors import DeviceFailed, NoResult
from wetlab.lab.mqtt.codec import parse_uplink
from wetlab.lab.sim import Fault, Scenario, SimBackend


def _short_state(backend: SimBackend) -> dict:
    raw = backend.state()
    out = {"_gateway": raw["_gateway"]}
    for code, props in raw.items():
        if code != "_gateway":
            out[CODE_TO_NAME[code]] = props
    return out


def test_working_status_becomes_2_on_failure_and_resets_to_0_on_success():
    backend = SimBackend()
    with pytest.raises(DeviceFailed):
        backend.call("PCR002", "UnknownAction", {}, 15)
    assert backend.state()["PCR002"]["WorkingStatus"] == 2

    backend.call("PCR002", "GetPcrStatus", {}, 15)
    assert backend.state()["PCR002"]["WorkingStatus"] == 0


def test_scenario_pcr_plate_lid_pressed_only_when_door_closed():
    closed_with_plate = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    assert closed_with_plate.state()["PCR002"]["DoorState"] == "Closed"
    assert closed_with_plate.state()["PCR002"]["LidMotorState"] == "Pressed"

    open_with_plate = SimBackend(
        Scenario(labware={"LW-001": "pcr"}, devices={"pcr": {"DoorState": "Open"}})
    )
    assert open_with_plate.state()["PCR002"]["DoorState"] == "Open"
    assert open_with_plate.state()["PCR002"]["LidMotorState"] == "Lifted"


def test_fault_injection_fail():
    scen = Scenario(
        faults=[
            Fault(
                device="pcr",
                action="SetLidMotor",
                nth=2,
                kind="fail",
                message="热盖电机异常(0xFF)",
            )
        ]
    )
    backend = SimBackend(scen)
    # 1st call succeeds.
    backend.call("PCR002", "SetLidMotor", {"Action": "Press"}, 20)
    assert backend.state()["PCR002"]["WorkingStatus"] == 0

    # 2nd call triggers injected failure.
    with pytest.raises(DeviceFailed, match="热盖电机异常"):
        backend.call("PCR002", "SetLidMotor", {"Action": "Lift"}, 20)
    assert backend.state()["PCR002"]["WorkingStatus"] == 2

    # 3rd call succeeds and clears WorkingStatus.
    backend.call("PCR002", "SetLidMotor", {"Action": "Lift"}, 20)
    assert backend.state()["PCR002"]["WorkingStatus"] == 0


def test_fault_injection_no_result():
    scen = Scenario(
        faults=[Fault(device="shaker", action="SetClamp", nth=1, kind="no_result")]
    )
    backend = SimBackend(scen)
    t0 = backend.now()
    with pytest.raises(NoResult):
        backend.call("T002001", "SetClamp", {"clamp": True}, 22.0)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 22.0


def test_two_runs_of_same_scenario_are_identical():
    def run_once() -> tuple[float, dict, dict, dict]:
        scen = Scenario(start_time=1_800_000_000.0, labware={"LW-001": "rack:1-3"})
        b = SimBackend(scen)
        b.call("PCR002", "OpenDoor", {}, 20)
        b.call(
            "TYPE_ROBOT_ARM001",
            "MovMulti",
            {
                "Tasks": [
                    {"PositionId": 2118246861233086466, "ClawSwitch": 0},
                    {"PositionId": 2118260185060499457, "ClawSwitch": 1},
                ],
                "Material": {"Index": "1-3", "Speed": "低速"},
            },
            90,
        )
        b.call("PCR002", "CloseDoor", {"WaitLid": False}, 20)
        b.sleep(6.0)
        b.call("PCR002", "SetTubeMode", {"Mode": "Block"}, 15)
        b.call("PCR002", "SetPlateCapacity", {"Microliters": 50}, 15)
        b.call("PCR002", "SetHeater", {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105}, 15)
        steps = [{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 30, "Cycles": 1}]
        b.call("PCR002", "RunProgram", {"Steps": json.dumps(steps)}, 15)
        while b.state()["PCR002"]["PcrState"] == "Running":
            b.sleep(1.0)
        b.call("PCR002", "SetHeater", {"Zone": "Lid", "Enabled": False}, 15)
        assert b.world.accidents == []
        return b.now(), b.state(), dict(b.world.plates), dict(b.world.history)

    assert run_once() == run_once()


def test_safety_gated_sequence_has_zero_accidents():
    backend = SimBackend(Scenario(labware={"LW-001": "shaker"}))

    def safe_do(dev: str, action: str, params: dict) -> dict:
        ok, reason = safety.check(dev, action, params, _short_state(backend))
        assert ok, reason
        return backend.call(NAME_TO_CODE[dev], action, params, 30.0)

    safe_do("shaker", "SetClamp", {"clamp": True})
    safe_do("shaker", "SetTargetSpeed", {"rpm": 500})
    safe_do("shaker", "StartShakeTimed", {"seconds": 5})
    assert backend.world.accidents == []


def test_realtime_flag_controls_wall_clock_sleep(monkeypatch: pytest.MonkeyPatch):
    slept: list[float] = []
    monkeypatch.setattr("wetlab.lab.sim.backend.time.sleep", slept.append)

    fast = SimBackend()
    assert fast.realtime is False
    t0 = fast.now()
    fast.sleep(30.0)
    assert fast.now() - t0 == pytest.approx(30.0)
    assert slept == []

    paced = SimBackend(realtime=True)
    assert paced.realtime is True
    t1 = paced.now()
    paced.sleep(0.0)
    assert paced.now() == pytest.approx(t1)
    assert slept == []

    paced.sleep(1.5)
    assert paced.now() - t1 == pytest.approx(1.5)
    assert slept == [1.5]


def test_get_devices_default_matches_codec_shape_and_covers_island():
    capture_line = None
    with open("captures/20261118_survey_110949.jsonl", encoding="utf-8") as fh:
        for line in fh:
            obj = json.loads(line)
            payload = obj.get("payload", "")
            if "GetDevices" in payload and "rpc-result" in payload:
                capture_line = obj
                break
    assert capture_line is not None
    parsed = parse_uplink(capture_line["topic"], capture_line["payload"])

    backend = SimBackend()
    for dev in ISLAND:
        res = backend.call(dev.spec.CODE, "GetDevices", {}, 15.0)
        assert set(res.keys()) == set(parsed.data.keys()) == {"deviceData"}
        items = res["deviceData"]
        assert isinstance(items, list)
        assert len(items) == len(ISLAND)
        codes = {}
        for entry in items:
            assert isinstance(entry["Id"], int)
            assert isinstance(entry["Name"], str)
            assert entry["IsEnabled"] is True
            cfg_map = {c["Key"]: c["Value"] for c in entry["Configs"]}
            codes[cfg_map["Code"]] = entry
        assert set(codes.keys()) == {d.spec.CODE for d in ISLAND}


def test_get_devices_scenario_override_for_missing_or_disabled():
    custom = [
        {
            "Id": 46,
            "Name": "PCR仪",
            "IsEnabled": False,
            "Configs": [{"Key": "Code", "Value": "PCR002"}],
        }
    ]
    backend = SimBackend(Scenario(gateway_devices=custom))
    res = backend.call("PCR002", "GetDevices", {}, 15.0)
    assert res == {"deviceData": custom}
    res["deviceData"][0]["IsEnabled"] = True
    assert backend.call("T002001", "GetDevices", {}, 15.0)["deviceData"][0]["IsEnabled"] is False

    empty_backend = SimBackend(Scenario(gateway_devices=[]))
    assert empty_backend.call("PCR002", "GetDevices", {}, 15.0) == {"deviceData": []}


def test_fault_injection_failed_after_act_executes_motion_then_raises():
    scen = Scenario(
        faults=[
            Fault(
                device="turntable",
                action="SingleLoop",
                nth=1,
                kind="failed_after_act",
                message="转向命令未收到电机应答",
            )
        ]
    )
    backend = SimBackend(scen)
    assert backend.state()["STEERING_PLATFORM001"]["Angles"] == "0"

    with pytest.raises(DeviceFailed, match="转向命令未收到电机应答"):
        backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 90}, 15.0)

    st = backend.state()["STEERING_PLATFORM001"]
    assert st["Angles"] == "90"
    assert st["WorkingStatus"] == 2

    backend.call("STEERING_PLATFORM001", "SingleLoop", {"Angle": 0}, 15.0)
    st_after = backend.state()["STEERING_PLATFORM001"]
    assert st_after["Angles"] == "0"
    assert st_after["WorkingStatus"] == 0
