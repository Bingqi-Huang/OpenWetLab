"""Tests for all 7 PCR skills, stage compilation, and lid heater cleanup (#11)."""

from wetlab.lab import registry
from wetlab.lab.devices.pcr.skills import compile_pcr_stages, format_pcr_stages
from wetlab.lab.lab import Lab
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import island, open_db, records


def _make_lab(
    *,
    plate_in_pcr: bool = True,
    pcr_overrides: dict | None = None,
    faults: list[Fault] | None = None,
) -> tuple[Lab, SimBackend]:
    conn = open_db(":memory:")
    lw = {"LW-001": "pcr"} if plate_in_pcr else {}
    devs = {"pcr": pcr_overrides} if pcr_overrides else {}
    backend = SimBackend(Scenario(labware=lw, devices=devs, faults=faults or []))
    lab = Lab(backend, conn)
    if plate_in_pcr:
        lab.materials.place("LW-001", "96_plate", "pcr")
    return lab, backend


def test_pcr_status_list_programs_open_and_close() -> None:
    lab, backend = _make_lab(plate_in_pcr=True)
    assert registry.call(lab, "pcr_status")["ok"] is True
    assert registry.call(lab, "pcr_list_programs")["result"]["totalFiles"] == 8
    assert registry.call(lab, "pcr_status", unexpected=1)["error"]["code"] == "INVALID_ARGUMENT"

    opened = registry.call(lab, "pcr_open")
    assert opened["ok"] is True and opened["state_after"]["pcr"]["DoorState"] == "Open"
    closed = registry.call(lab, "pcr_close")
    assert closed["ok"] is True and closed["state_after"]["pcr"]["LidMotorState"] == "Pressed"

    registry.call(
        lab,
        "pcr_start_program",
        stages=[{"temperature_c": 37, "hold_seconds": 60}],
        volume_ul=50,
        lid_c=None,
    )
    assert registry.call(lab, "pcr_open")["status"] == "blocked"
    backend.gateway_online = False
    assert registry.call(lab, "pcr_close")["error"]["code"] == "GATEWAY_OFFLINE"
    assert backend.world.accidents == []


def test_compile_task_md_pcr_program_and_reject_nested_blocks() -> None:
    stages = [
        {"temperature_c": 98, "hold_seconds": 60},
        {
            "repeat": 23,
            "steps": [
                {"temperature_c": 98, "hold_seconds": 10},
                {"temperature_c": 60, "hold_seconds": 5},
                {"temperature_c": 72, "hold_seconds": 5},
            ],
        },
        {"temperature_c": 72, "hold_seconds": 60},
    ]
    compiled = compile_pcr_stages(stages)
    assert len(compiled) == 5
    assert compiled[3]["NextStep"] == 2 and compiled[3]["Cycles"] == 23
    assert (
        format_pcr_stages(stages)
        == "98℃ 1 分钟 →（98℃ 10 秒 → 60℃ 5 秒 → 72℃ 5 秒）× 23 → 72℃ 1 分钟"
    )

    lab, _ = _make_lab()
    inner_block = {"repeat": 2, "steps": [{"temperature_c": 60, "hold_seconds": 10}]}
    nested = [{"repeat": 3, "steps": [inner_block]}]
    res = registry.call(lab, "pcr_start_program", stages=nested, volume_ul=50)
    assert res["status"] == "blocked" and res["error"]["code"] == "INVALID_ARGUMENT"
    assert records.list_operations(lab.conn) == []


def test_pcr_start_program_lid_order_and_lid_cleanup_on_runprogram_failure() -> None:
    """#11: If RunProgram fails after lid heater is enabled, lid heater is turned back off!"""
    lab, backend = _make_lab()
    ok_res = registry.call(
        lab,
        "pcr_start_program",
        stages=[{"temperature_c": 90, "hold_seconds": 30}],
        volume_ul=50,
    )
    assert ok_res["ok"] is True
    assert [op["action"] for op in records.list_operations(lab.conn)] == [
        "SetTubeMode",
        "SetPlateCapacity",
        "SetHeater",
        "RunProgram",
    ]
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is True

    # 1. Injected RunProgram DeviceFailed (sets WorkingStatus=2!) -> turns off lid heater!
    lab_fail, backend_fail = _make_lab(
        faults=[Fault(device="pcr", action="RunProgram", nth=1, kind="fail", message="0x03C0 err")]
    )
    fail_res = registry.call(
        lab_fail,
        "pcr_start_program",
        stages=[{"temperature_c": 95, "hold_seconds": 30}],
        volume_ul=50,
        lid_c=105,
    )
    assert fail_res["ok"] is False
    assert fail_res["status"] == "failed"
    assert fail_res["error"]["code"] == "DEVICE_FAILED"
    assert backend_fail.state()["PCR002"]["HeaterLidEnabled"] is False
    assert [op["action"] for op in records.list_operations(lab_fail.conn)] == [
        "SetTubeMode",
        "SetPlateCapacity",
        "SetHeater",
        "RunProgram",
        "SetHeater",
    ]

    # 2. Injected RunProgram no_result -> freezes island and leaves lid heater untouched for human
    lab_nr, backend_nr = _make_lab(
        faults=[Fault(device="pcr", action="RunProgram", nth=1, kind="no_result")]
    )
    nr_res = registry.call(
        lab_nr,
        "pcr_start_program",
        stages=[{"temperature_c": 95, "hold_seconds": 30}],
        volume_ul=50,
        lid_c=105,
    )
    assert nr_res["status"] == "unknown"
    assert island.get_freeze(lab_nr.conn)["frozen"] is True
    assert backend_nr.state()["PCR002"]["HeaterLidEnabled"] is True
    # Human stop_all -> pcr_stop while frozen turns off lid heater!
    stop_frozen = registry.call(lab_nr, "pcr_stop")
    assert stop_frozen["ok"] is True
    assert backend_nr.state()["PCR002"]["HeaterLidEnabled"] is False


def test_pcr_stop_turns_off_lid_even_when_stoprun_fails_or_already_idle() -> None:
    """#11: pcr_stop turns off lid heater even if StopRun fails, and skips StopRun if Idle."""
    # 1. StopRun fails -> lid heater is STILL turned off, and DeviceFailed is propagated!
    lab, backend = _make_lab(
        faults=[Fault(device="pcr", action="StopRun", nth=1, kind="fail", message="stop err")]
    )
    registry.call(
        lab,
        "pcr_start_program",
        stages=[{"temperature_c": 90, "hold_seconds": 300}],
        volume_ul=50,
        lid_c=105,
    )
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is True
    stop_res = registry.call(lab, "pcr_stop")
    assert stop_res["ok"] is False
    assert stop_res["status"] == "failed"
    assert stop_res["error"]["code"] == "DEVICE_FAILED"
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is False

    # 2. Already Idle with lid heater on -> skips StopRun and turns off lid heater
    lab_idle, backend_idle = _make_lab(pcr_overrides={"HeaterLidEnabled": True})
    idle_stop = registry.call(lab_idle, "pcr_stop")
    assert idle_stop["ok"] is True
    assert [op["action"] for op in records.list_operations(lab_idle.conn)] == ["SetHeater"]
    assert backend_idle.state()["PCR002"]["HeaterLidEnabled"] is False


def test_pcr_confirm_finished_still_running_leaves_lid_on_and_handles_completion() -> None:
    """#11: Running/Paused raises PROGRAM_STILL_RUNNING without turning lid off."""
    lab, backend = _make_lab()
    registry.call(
        lab,
        "pcr_start_program",
        stages=[{"temperature_c": 60, "hold_seconds": 40}],
        volume_ul=50,
        lid_c=105,
    )
    assert backend.state()["PCR002"]["PcrState"] == "Running"
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is True

    # While Running -> PROGRAM_STILL_RUNNING, lid heater NOT touched!
    still_run = registry.call(lab, "pcr_confirm_finished")
    assert still_run["ok"] is False
    assert still_run["status"] == "failed"
    assert still_run["error"]["code"] == "PROGRAM_STILL_RUNNING"
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is True

    # While Paused -> PROGRAM_STILL_RUNNING, lid heater NOT touched!
    backend.call("PCR002", "PauseRun", {}, 10)
    still_paused = registry.call(lab, "pcr_confirm_finished")
    assert still_paused["error"]["code"] == "PROGRAM_STILL_RUNNING"
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is True

    # Resume and let program finish -> confirm succeeds and turns off lid heater
    backend.call("PCR002", "StartRun", {}, 10)
    while backend.state()["PCR002"]["PcrState"] == "Running":
        backend.sleep(1.0)
    done = registry.call(lab, "pcr_confirm_finished")
    assert done["ok"] is True
    assert backend.state()["PCR002"]["HeaterLidEnabled"] is False

    # External early stop -> PROGRAM_ENDED_EARLY and lid turned off
    lab2, backend2 = _make_lab()
    registry.call(
        lab2,
        "pcr_start_program",
        stages=[{"temperature_c": 90, "hold_seconds": 300}],
        volume_ul=50,
        lid_c=105,
    )
    backend2.sleep(5.0)
    backend2.call("PCR002", "StopRun", {}, 15)
    early = registry.call(lab2, "pcr_confirm_finished")
    assert early["error"]["code"] == "PROGRAM_ENDED_EARLY"
    assert early["state_after"]["pcr"]["HeaterLidEnabled"] is False
