"""Tests for wetlab.lab.snapshot (read-only island snapshot and key_props filtering)."""

from wetlab.lab.snapshot import extract_key_state, read_snapshot
from wetlab.lab.store import island, open_db, records


def test_empty_and_populated_snapshot() -> None:
    conn = open_db(":memory:")
    try:
        empty = read_snapshot(conn)
        assert set(empty["devices"].keys()) == {"pcr", "shaker", "arm", "turntable", "rack"}
        assert empty["gateway"] == {
            "online": False,
            "busy_by_others": False,
            "updated_at": None,
            "age_s": None,
        }
        assert empty["labware"] == {}
        assert empty["freeze"]["frozen"] is False
        assert empty["last_timestamp"] == 0.0

        with conn:
            island.upsert_device_state(
                conn,
                device="pcr",
                props={
                    "DoorState": "Closed",
                    "LidMotorState": "Pressed",
                    "PcrState": "Running",
                    "FaultText": "无故障",
                    "PcrRemainingSeconds": 120,
                    "LidTemperature": 105.0,
                    "HeaterLidEnabled": True,
                    "InternalDebugCounter": 999,
                },
                updated_at=50.0,
            )
            island.upsert_device_state(
                conn,
                device="shaker",
                props={"ElmState": 2, "ShakeState": 1, "ActualSpeed": 0, "ErrorCode": 0},
                error="sensor warning",
                updated_at=52.0,
            )
            island.upsert_device_state(
                conn,
                device="_gateway",
                props={"online": True, "busy_by_others": False},
                updated_at=53.0,
            )
            records.upsert_labware(conn, id="LW-001", type="pcr_plate_96", position="pcr")
            records.upsert_labware(conn, id="LW-002", type="pcr_plate_96", position=None)
            records.insert_operation(
                conn,
                id="WL-60000-1",
                seq=1,
                device="pcr",
                action="RunProgram",
                params={},
                status="succeeded",
                started_at=60.0,
                elapsed_s=4.5,
            )

        snap = read_snapshot(conn, now=55.0)
        assert snap["gateway"] == {
            "online": True,
            "busy_by_others": False,
            "updated_at": 53.0,
            "age_s": 2.0,
        }
        assert snap["devices"]["pcr"]["props"]["InternalDebugCounter"] == 999
        assert "InternalDebugCounter" not in snap["devices"]["pcr"]["key_props"]
        assert snap["devices"]["pcr"]["key_props"]["PcrRemainingSeconds"] == 120
        assert snap["devices"]["pcr"]["age_s"] == 5.0
        assert snap["devices"]["shaker"]["error"] == "sensor warning"
        assert snap["labware"] == {"LW-001": "pcr"}
        assert snap["labware_details"] == {
            "LW-001": {"type": "pcr_plate_96", "position": "pcr"}
        }
        assert snap["last_timestamp"] == 64.5

        key_state = extract_key_state(
            {"pcr": snap["devices"]["pcr"]["props"]}, snap["labware"]
        )
        assert "InternalDebugCounter" not in key_state["pcr"]
        assert key_state["pcr"]["HeaterLidEnabled"] is True
        assert key_state["labware"] == {"LW-001": "pcr"}
    finally:
        conn.close()
