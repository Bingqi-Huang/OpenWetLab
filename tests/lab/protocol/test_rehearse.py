"""Tests for wetlab.lab.protocol.rehearse (Step 5 acceptance items and #2 closed_skills)."""

import json
from pathlib import Path
import sqlite3

import pytest

from wetlab.lab.materials import Materials
from wetlab.lab.protocol import rehearse, render
from wetlab.lab.store import island, open_db, records

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def _dump_all_tables(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = [r["name"] for r in rows]
    return {
        t: [dict(row) for row in conn.execute(f"SELECT * FROM {t}").fetchall()]
        for t in tables
    }


def test_both_sops_rehearse_cleanly_with_matching_totals_zero_accidents_and_unchanged_db(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    conn = open_db(db_file)
    Materials(conn, now=lambda: 100.0).place("LW-001", "pcr_plate_96", "pcr")
    try:
        before = _dump_all_tables(conn)

        for filename in ("pcr_37c_30s.json", "task_thermal_workflow.json"):
            proto = json.loads((SOPS_DIR / filename).read_text("utf-8"))
            res = rehearse(conn, proto)
            assert res["ok"] is True, f"{filename} failed: {res['failure']}"
            assert res["failure"] is None
            assert res["accidents"] == []
            assert len(res["steps"]) == len(proto["steps"])
            step_sum = round(sum(s["elapsed_s"] for s in res["steps"]), 3)
            assert res["total_s"] == pytest.approx(step_sum)
            assert "预演通过" in render(proto, res)

        assert _dump_all_tables(conn) == before
    finally:
        conn.close()


def test_rehearse_rejects_closed_skills_before_executing_any_step() -> None:
    """#2: Closed skills in `meta` fail rehearsal upfront with zero operations."""
    conn = open_db(":memory:")
    Materials(conn, now=lambda: 1.0).place("LW-001", "pcr_plate_96", "pcr")
    try:
        with conn:
            island.set_closed_skills(conn, {"pcr_start_program"})
        sop1 = json.loads((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))
        res = rehearse(conn, sop1)
        assert res["ok"] is False
        assert res["steps"] == []
        assert res["failure"] is not None
        assert res["failure"]["code"] == "VALIDATION_FAILED"
        assert "pcr_start_program" in res["failure"]["reason"]
        assert "还没有开放" in res["failure"]["reason"]
        assert records.list_operations(conn) == []
    finally:
        conn.close()


def test_sop1_with_plate_on_shaker_and_wrong_order_pcr_open() -> None:
    conn = open_db(":memory:")
    Materials(conn, now=lambda: 1.0).place("LW-001", "pcr_plate_96", "shaker")
    try:
        sop1 = json.loads((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))
        sop1["labware"] = {"LW-001": "shaker"}
        res = rehearse(conn, sop1)
        assert res["ok"] is False
        assert res["failure"]["step_n"] == 2
        assert "Pressed" in res["failure"]["reason"]

        Materials(conn, now=lambda: 2.0).correct("LW-001", "pcr")
        bad_order = {
            "name": "运行中开门错误顺序",
            "labware": {"LW-001": "pcr"},
            "steps": [
                {"skill": "pcr_close", "args": {}},
                {
                    "skill": "pcr_start_program",
                    "args": {
                        "stages": [{"temperature_c": 37, "hold_seconds": 60}],
                        "volume_ul": 50,
                        "lid_c": None,
                    },
                },
                {"skill": "pcr_open", "args": {}},
            ],
        }
        res_bad = rehearse(conn, bad_order)
        assert res_bad["ok"] is False
        assert res_bad["failure"]["step_n"] == 3
        assert res_bad["failure"]["code"] == "INTERLOCK_BLOCKED"
    finally:
        conn.close()


def test_rehearsal_while_pcr_running_resumes_by_remaining_seconds() -> None:
    conn = open_db(":memory:")
    Materials(conn, now=lambda: 500.0).place("LW-001", "pcr_plate_96", "pcr")
    try:
        with conn:
            island.upsert_device_state(
                conn,
                device="pcr",
                props={
                    "DoorState": "Closed",
                    "LidMotorState": "Pressed",
                    "PcrState": "Running",
                    "FaultText": "无故障",
                    "PcrRemainingSeconds": 35,
                    "HeaterLidEnabled": False,
                    "LidTemperature": 27.0,
                },
                updated_at=500.0,
            )

        resume_proto = {
            "name": "等运行中的程序跑完后开门",
            "labware": {"LW-001": "pcr"},
            "steps": [
                {
                    "wait_until": {
                        "device": "pcr",
                        "state": {"PcrState": "Idle"},
                        "max_seconds": 120,
                    }
                },
                {"skill": "pcr_open", "args": {}},
            ],
        }
        res = rehearse(conn, resume_proto)
        assert res["ok"] is True
        assert res["steps"][0]["elapsed_s"] == pytest.approx(35.0)
        assert res["steps"][1]["state_after"]["pcr"]["DoorState"] == "Open"
    finally:
        conn.close()
