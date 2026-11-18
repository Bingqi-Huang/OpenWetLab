"""Tests for Scenario.from_snapshot / from_snapshot and calibrated SOP rehearsal (#33)."""

import json
from pathlib import Path

import pytest

from wetlab.lab.materials import Materials
from wetlab.lab.protocol import rehearse
from wetlab.lab.sim import DEFAULT_EPOCH, Scenario, SimBackend
from wetlab.lab.sim.scenario import from_snapshot
from wetlab.lab.snapshot import read_snapshot
from wetlab.lab.store import island, open_db, records

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def test_from_empty_snapshot_uses_default_epoch_and_clean_defaults() -> None:
    conn = open_db(":memory:")
    try:
        scen = Scenario.from_snapshot(read_snapshot(conn))
        assert scen.start_time == DEFAULT_EPOCH
        assert scen.labware == {}
        assert scen.devices == {}
    finally:
        conn.close()


def test_from_snapshot_restores_clock_labware_and_resumes_running_pcr() -> None:
    conn = open_db(":memory:")
    try:
        with conn:
            records.upsert_labware(conn, id="LW-001", type="pcr_plate_96", position="pcr")
            island.upsert_device_state(
                conn,
                device="pcr",
                props={
                    "DoorState": "Closed",
                    "LidMotorState": "Pressed",
                    "PcrState": "Running",
                    "FaultText": "无故障",
                    "PcrRemainingSeconds": 50,
                    "_age_s": 1.0,
                },
                updated_at=2000.0,
            )
            # Latest operation finished at 2010.0 (10s after PCR state was saved)
            records.insert_operation(
                conn,
                id="WL-2005000-1",
                seq=1,
                device="shaker",
                action="GetStatus",
                params={},
                status="succeeded",
                started_at=2005.0,
                elapsed_s=5.0,
            )

        snap = read_snapshot(conn)
        scen = from_snapshot(snap)
        assert scen.start_time == 2010.0
        assert scen.labware == {"LW-001": "pcr"}
        assert scen.devices["pcr"]["PcrRemainingSeconds"] == 40

        sim = SimBackend(scen)
        assert sim.now() == 2010.0
        assert sim.state()["PCR002"]["PcrState"] == "Running"
        sim.sleep(39.0)
        assert sim.state()["PCR002"]["PcrState"] == "Running"
        sim.sleep(2.0)
        assert sim.state()["PCR002"]["PcrState"] == "Idle"
    finally:
        conn.close()


def test_from_snapshot_accepts_dict_labware_entries() -> None:
    snap = {
        "devices": {},
        "gateway": {"online": True, "busy_by_others": False},
        "labware": {
            "LW-003": {
                "position": "rack:1-2",
                "type": "96孔PCR板",
                "contents": [{"name": "X", "wells": None, "volume_ul": 50.0, "note": None}],
            }
        },
        "freeze": {"frozen": False, "reason": None, "operation_id": None, "at": None},
        "last_timestamp": 2500.0,
    }
    scen = from_snapshot(snap)
    assert scen.start_time == 2500.0
    assert scen.labware == {"LW-003": "rack:1-2"}


def test_task_thermal_workflow_calibrated_rehearsal_totals_and_wait_margins() -> None:
    conn = open_db(":memory:")
    try:
        Materials(conn, now=lambda: 100.0).place("LW-001", "pcr_plate_96", "pcr")
        proto = json.loads((SOPS_DIR / "task_thermal_workflow.json").read_text("utf-8"))
        res = rehearse(conn, proto)
        assert res["ok"] is True
        assert res["accidents"] == []
        assert res["total_s"] == pytest.approx(4438.0)

        wait_steps = [s for s in res["steps"] if "wait_until" in s["step"]]
        assert len(wait_steps) == 4
        for step_out in wait_steps:
            max_s = step_out["step"]["wait_until"]["max_seconds"]
            assert step_out["elapsed_s"] < max_s
            assert (max_s - step_out["elapsed_s"]) >= 500.0
    finally:
        conn.close()
