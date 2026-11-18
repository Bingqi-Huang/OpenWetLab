"""Tests for registry.call envelopes, closed_skills, isolated skill imports, and catalog()."""

import subprocess
import sys

from wetlab.lab import registry
from wetlab.lab.devices import KEY_PROPS
from wetlab.lab.lab import Lab
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import open_db, records

EXPECTED_12_SKILLS = {
    "pcr_status",
    "pcr_open",
    "pcr_close",
    "pcr_list_programs",
    "pcr_start_program",
    "pcr_confirm_finished",
    "pcr_stop",
    "shake",
    "shake_start",
    "shake_stop",
    "transfer",
    "rotate_turntable",
}
ENVELOPE_KEYS = {
    "ok",
    "status",
    "result",
    "error",
    "operation_ids",
    "state_after",
    "elapsed_s",
}


def _check_envelope(resp: dict, expected_status: str) -> None:
    assert set(resp.keys()) == ENVELOPE_KEYS
    assert resp["status"] == expected_status
    assert resp["ok"] is (expected_status == "succeeded")
    if resp["ok"]:
        assert resp["error"] is None
    else:
        assert isinstance(resp["error"], dict)
        assert set(resp["error"].keys()) == {"code", "message"}
    assert isinstance(resp["operation_ids"], list)
    assert isinstance(resp["elapsed_s"], float)
    sa = resp["state_after"]
    assert "labware" in sa
    for dev_name, keys in KEY_PROPS.items():
        if dev_name in sa:
            assert set(sa[dev_name].keys()) <= set(keys)


def test_isolated_import_of_each_device_skills_does_not_import_lab_or_registry():
    for dev in ("pcr", "shaker", "arm", "turntable"):
        code = (
            f"import sys, wetlab.lab.devices.{dev}.skills; "
            "assert 'wetlab.lab.lab' not in sys.modules; "
            "assert 'wetlab.lab.registry' not in sys.modules"
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr


def test_catalog_lists_all_12_skills_with_ranges_and_stable_version_hash():
    cat1 = registry.catalog()
    cat2 = registry.catalog()
    assert cat1["version"] == cat2["version"]
    assert len(cat1["version"]) == 16

    by_name = {item["name"]: item for item in cat1["skills"]}
    assert set(by_name.keys()) == EXPECTED_12_SKILLS

    shake_props = by_name["shake"]["parameters"]["properties"]
    assert shake_props["seconds"]["minimum"] == 1
    assert shake_props["seconds"]["maximum"] == 60
    assert shake_props["rpm"]["minimum"] == 200
    assert shake_props["rpm"]["maximum"] == 3000

    tt_props = by_name["rotate_turntable"]["parameters"]["properties"]
    assert set(tt_props["angle"]["enum"]) == {0, 90, 180, 270}

    # Subprocess check: deterministic across separate interpreter processes.
    proc = subprocess.run(
        [sys.executable, "-c", "from wetlab.lab import registry; print(registry.catalog()['version'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == cat1["version"]


def test_closed_skills_returns_not_available_without_sending_commands():
    conn = open_db(":memory:")
    backend = SimBackend()
    backend.closed_skills.add("pcr_stop")
    lab = Lab(backend, conn)

    resp = registry.call(lab, "pcr_stop")
    _check_envelope(resp, "not_available")
    assert resp["error"]["code"] == "NOT_AVAILABLE"
    assert resp["operation_ids"] == []
    assert records.list_operations(conn) == []


def test_all_six_return_statuses_conform_to_01_section_5_1():
    # 1. succeeded
    conn1 = open_db(":memory:")
    lab1 = Lab(SimBackend(), conn1)
    r_ok = registry.call(lab1, "pcr_status")
    _check_envelope(r_ok, "succeeded")
    assert len(r_ok["operation_ids"]) == 1

    # 2. blocked (INVALID_ARGUMENT and INTERLOCK_BLOCKED)
    r_bad_arg = registry.call(lab1, "shake", seconds=0, rpm=300)
    _check_envelope(r_bad_arg, "blocked")
    assert r_bad_arg["error"]["code"] == "INVALID_ARGUMENT"

    # 3. failed (DEVICE_FAILED on step 2 of pcr_open, capturing step 1 + step 2 operation_ids)
    conn2 = open_db(":memory:")
    b_fail = SimBackend(
        Scenario(
            labware={"LW-001": "pcr"},
            faults=[Fault(device="pcr", action="OpenDoor", nth=1, kind="fail", message="门电机异常")],
        )
    )
    lab2 = Lab(b_fail, conn2)
    r_fail = registry.call(lab2, "pcr_open")
    _check_envelope(r_fail, "failed")
    assert r_fail["error"]["code"] == "DEVICE_FAILED"
    assert len(r_fail["operation_ids"]) == 2

    # 4. unknown (TIMEOUT_UNKNOWN -> freezes lab) & 5. frozen on subsequent act
    conn3 = open_db(":memory:")
    b_timeout = SimBackend(
        Scenario(faults=[Fault(device="shaker", action="SetClamp", nth=1, kind="no_result")])
    )
    lab3 = Lab(b_timeout, conn3)
    r_unk = registry.call(lab3, "shake", seconds=5, rpm=300)
    _check_envelope(r_unk, "unknown")
    assert r_unk["error"]["code"] == "TIMEOUT_UNKNOWN"
    assert len(r_unk["operation_ids"]) == 1

    r_frz = registry.call(lab3, "shake", seconds=5, rpm=300)
    _check_envelope(r_frz, "frozen")
    assert r_frz["error"]["code"] == "FROZEN"
    assert r_frz["operation_ids"] == []

    # Read skill still works while frozen!
    r_read_frz = registry.call(lab3, "pcr_status")
    _check_envelope(r_read_frz, "succeeded")

    # 6. not_available
    b_timeout.closed_skills.add("rotate_turntable")
    r_na = registry.call(lab3, "rotate_turntable", angle=90)
    _check_envelope(r_na, "not_available")
