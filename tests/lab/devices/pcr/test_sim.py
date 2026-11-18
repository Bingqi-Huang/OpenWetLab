"""Tests for PcrSim behaviors, ramp duration, and physical accident detection."""

import json
import math
import time

import pytest

from wetlab.lab.devices.pcr.sim import PcrSim, program_seconds
from wetlab.lab.errors import DeviceFailed
from wetlab.lab.sim import Scenario, SimBackend


def test_open_and_close_door_timing_and_moving_state():
    backend = SimBackend()
    pcr_sim = backend.world.device("pcr")
    backend.call("PCR002", "SetLidMotor", {"Action": "Lift"}, 20)
    backend.sleep(5.0)

    t0 = backend.now()
    proc = backend.world.env.process(pcr_sim.OpenDoor({}))
    backend.sleep(2.0)
    assert backend.state()["PCR002"]["DoorState"] == "Moving"
    backend.world.env.run(until=proc)
    assert backend.state()["PCR002"]["DoorState"] == "Open"
    assert pytest.approx(backend.now() - t0, abs=0.01) == 4.1

    t1 = backend.now()
    proc_close = backend.world.env.process(pcr_sim.CloseDoor({"WaitLid": False}))
    backend.sleep(2.0)
    assert backend.state()["PCR002"]["DoorState"] == "Moving"
    backend.world.env.run(until=proc_close)
    assert backend.state()["PCR002"]["DoorState"] == "Closed"
    assert pytest.approx(backend.now() - t1, abs=0.01) == 4.1


def test_close_door_auto_presses_lid_only_when_plate_inside():
    with_plate = SimBackend(
        Scenario(
            labware={"LW-001": "pcr"},
            devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted"}},
        )
    )
    with_plate.call("PCR002", "CloseDoor", {"WaitLid": False}, 20)
    assert with_plate.state()["PCR002"]["DoorState"] == "Closed"
    assert with_plate.state()["PCR002"]["LidMotorState"] == "Moving"
    with_plate.sleep(6.0)
    assert with_plate.state()["PCR002"]["LidMotorState"] == "Pressed"

    empty = SimBackend(Scenario(devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted"}}))
    empty.call("PCR002", "CloseDoor", {"WaitLid": False}, 20)
    empty.sleep(10.0)
    assert empty.state()["PCR002"]["LidMotorState"] == "Lifted"


def test_close_door_without_wait_lid_false_times_out_when_empty():
    empty = SimBackend(Scenario(devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted"}}))
    t0 = empty.now()
    with pytest.raises(DeviceFailed):
        empty.call("PCR002", "CloseDoor", {}, 30)
    assert pytest.approx(empty.now() - t0, abs=0.01) == 24.1
    assert empty.state()["PCR002"]["WorkingStatus"] == 2


def test_set_lid_motor_defaults_to_seal():
    backend = SimBackend(
        Scenario(labware={"LW-001": "pcr"}, devices={"pcr": {"LidMotorState": "Lifted"}})
    )
    t0 = backend.now()
    backend.call("PCR002", "SetLidMotor", {}, 20)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 0.2
    assert backend.state()["PCR002"]["LidMotorState"] == "Sealing"
    backend.sleep(5.0)
    assert backend.state()["PCR002"]["LidMotorState"] == "Lifted"
    assert backend.world.history["LW-001"][-1]["what"] == "sealed"


def test_set_lid_motor_returns_immediately_and_moves_in_background():
    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    assert backend.state()["PCR002"]["LidMotorState"] == "Pressed"

    t0 = backend.now()
    backend.call("PCR002", "SetLidMotor", {"Action": "Lift"}, 20)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 0.2
    assert backend.state()["PCR002"]["LidMotorState"] == "Moving"

    backend.sleep(3.0)
    assert backend.state()["PCR002"]["LidMotorState"] == "Moving"

    backend.sleep(2.0)
    assert backend.state()["PCR002"]["LidMotorState"] == "Lifted"

    t1 = backend.now()
    backend.call("PCR002", "SetLidMotor", {"Action": "Press"}, 20)
    assert pytest.approx(backend.now() - t1, abs=0.01) == 0.2
    assert backend.state()["PCR002"]["LidMotorState"] == "Moving"
    backend.sleep(5.0)
    assert backend.state()["PCR002"]["LidMotorState"] == "Pressed"


def test_run_program_returns_immediately_counts_down_and_finishes_idle():
    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    steps = [{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 30, "Cycles": 1}]
    expected_total = program_seconds(steps)
    assert expected_total > 50  # includes 20s prep + ramp from 25 C to 37 C + 30s hold

    t0 = backend.now()
    backend.call("PCR002", "RunProgram", {"Steps": json.dumps(steps)}, 15)
    assert pytest.approx(backend.now() - t0, abs=0.01) == 0.25
    st = backend.state()["PCR002"]
    assert st["PcrState"] == "Running"
    rem1 = st["PcrRemainingSeconds"]
    assert 0 < rem1 <= expected_total

    backend.sleep(10.0)
    rem2 = backend.state()["PCR002"]["PcrRemainingSeconds"]
    assert rem2 < rem1

    backend.sleep(expected_total)
    st_done = backend.state()["PCR002"]
    assert st_done["PcrState"] == "Idle"
    assert st_done["PcrRemainingSeconds"] == 0
    assert backend.world.history["LW-001"][-1]["outcome"] == "completed"


def test_program_seconds_calibrated_prep_heating_cooling_and_hold():
    steps = [
        {"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 10, "Cycles": 1},
        {"ZoneATarget": 4, "ZoneCTarget": 4, "HoldSeconds": 30, "Cycles": 1},
    ]
    expected = round(20.0 + (95.0 - 25.0) / 2.0 + 10.0 + math.log(95.0 / 4.0) / 0.029 + 30.0)
    assert program_seconds(steps) == expected == 204

    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    t0 = backend.now()
    backend.call("PCR002", "RunProgram", {"Steps": json.dumps(steps)}, 15)
    backend.sleep(expected - 1.0)
    assert backend.state()["PCR002"]["PcrState"] == "Running"
    backend.sleep(2.0)
    assert backend.state()["PCR002"]["PcrState"] == "Idle"
    assert backend.now() - t0 >= expected


def test_program_lid_auto_control_when_enabled_vs_disabled():
    # 1. Lid enabled before RunProgram -> off for 10s in prep, back on, then off at <= 4 C step
    b_on = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    b_on.call("PCR002", "SetHeater", {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105.0}, 15)
    steps = [
        {"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 10, "Cycles": 1},
        {"ZoneATarget": 4, "ZoneCTarget": 4, "HoldSeconds": 30, "Cycles": 1},
    ]
    b_on.call("PCR002", "RunProgram", {"Steps": json.dumps(steps)}, 15)
    assert b_on.state()["PCR002"]["HeaterLidEnabled"] is True

    b_on.sleep(5.0)  # inside the 10s initial lid-off window
    assert b_on.state()["PCR002"]["HeaterLidEnabled"] is False

    b_on.sleep(10.0)  # after 10s off window, lid turns back on
    assert b_on.state()["PCR002"]["HeaterLidEnabled"] is True

    b_on.sleep(55.0)  # past 20s prep + 45s step 1 -> now inside 4 C step 2
    assert b_on.state()["PCR002"]["PcrState"] == "Running"
    assert b_on.state()["PCR002"]["HeaterLidEnabled"] is False

    while b_on.state()["PCR002"]["PcrState"] == "Running":
        b_on.sleep(5.0)
    assert b_on.state()["PCR002"]["HeaterLidEnabled"] is False

    # 2. Lid disabled before RunProgram -> stays False throughout
    b_off = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    steps_37 = [{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 20, "Cycles": 1}]
    b_off.call("PCR002", "RunProgram", {"Steps": json.dumps(steps_37)}, 15)
    for _ in range(10):
        assert b_off.state()["PCR002"]["HeaterLidEnabled"] is False
        b_off.sleep(5.0)
    assert b_off.state()["PCR002"]["HeaterLidEnabled"] is False


def test_stop_run_interrupts_running_program_to_idle():
    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    steps = [{"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 300, "Cycles": 1}]
    backend.call("PCR002", "RunProgram", {"Steps": json.dumps(steps)}, 15)
    backend.sleep(20.0)
    assert backend.state()["PCR002"]["PcrState"] == "Running"

    backend.call("PCR002", "StopRun", {}, 15)
    st = backend.state()["PCR002"]
    assert st["PcrState"] == "Idle"
    assert st["PcrRemainingSeconds"] == 0
    assert backend.world.history["LW-001"][-1]["outcome"] == "stopped"


def test_list_files_returns_eight_monad_programs():
    backend = SimBackend()
    res = backend.call("PCR002", "ListFiles", {}, 15)
    assert res["success"] is True
    assert res["totalFiles"] == 8
    names = [item["fullName"] for item in res["files"]]
    assert names == [f"Monad/{f}" for f in PcrSim.FILES]


def test_heater_plate_capacity_and_tube_mode_settings():
    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    backend.call("PCR002", "SetTubeMode", {"Mode": "Block"}, 15)
    res_cap = backend.call("PCR002", "SetPlateCapacity", {"Microliters": 50}, 15)
    assert res_cap["microliters"] == 50
    backend.call(
        "PCR002",
        "SetHeater",
        {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105.0},
        15,
    )
    backend.sleep(60.0)
    st = backend.state()["PCR002"]
    assert st["TubeMode"] == "Block"
    assert st["PlateCapacity"] == 0
    assert st["HeaterLidEnabled"] is True
    assert st["HeaterLidTarget"] == 105.0
    assert st["LidTemperature"] == 105.0

    backend.call("PCR002", "SetHeater", {"Zone": "Lid", "Enabled": False}, 15)
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is False


def test_two_hour_pcr_program_completes_under_one_second_with_ramp_time():
    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    steps = [
        {"ZoneATarget": 95, "ZoneCTarget": 95, "HoldSeconds": 3600, "Cycles": 1},
        {"ZoneATarget": 60, "ZoneCTarget": 60, "HoldSeconds": 3600, "Cycles": 1},
    ]
    total = program_seconds(steps)
    assert total > 7200

    wall_start = time.perf_counter()
    sim_start = backend.now()
    backend.call("PCR002", "RunProgram", {"Steps": json.dumps(steps)}, 15)
    while backend.state()["PCR002"]["PcrState"] == "Running":
        backend.sleep(60.0)
    wall_elapsed = time.perf_counter() - wall_start

    assert wall_elapsed < 1.0
    assert backend.now() - sim_start > 7200.0
    assert backend.world.accidents == []


def test_accident_heating_started_without_closed_door_or_pressed_lid():
    open_door = SimBackend(
        Scenario(devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted"}})
    )
    steps = json.dumps([{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 10}])
    open_door.call("PCR002", "RunProgram", {"Steps": steps}, 15)
    assert len(open_door.world.accidents) == 1

    unpressed = SimBackend(
        Scenario(devices={"pcr": {"DoorState": "Closed", "LidMotorState": "Lifted"}})
    )
    lid_params = {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105}
    unpressed.call("PCR002", "SetHeater", lid_params, 15)
    assert len(unpressed.world.accidents) == 1


def test_accidents_door_and_lid_misuse():
    # 1. Opening door when lid is Pressed.
    b1 = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    b1.call("PCR002", "OpenDoor", {}, 15)
    assert len(b1.world.accidents) == 1

    # 2. Lifting lid while a program is running.
    b2 = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    steps = json.dumps([{"ZoneATarget": 37, "ZoneCTarget": 37, "HoldSeconds": 60}])
    b2.call("PCR002", "RunProgram", {"Steps": steps}, 15)
    b2.call("PCR002", "SetLidMotor", {"Action": "Lift"}, 15)
    assert len(b2.world.accidents) == 1

    # 3. Pressing lid while door is Open.
    b3 = SimBackend(Scenario(devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted"}}))
    b3.call("PCR002", "SetLidMotor", {"Action": "Press"}, 15)
    assert len(b3.world.accidents) == 1
