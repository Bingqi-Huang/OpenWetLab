"""Tests for wetlab.lab.store.records (operations, labware, and labware_events)."""

from pathlib import Path
import sqlite3

import pytest

from wetlab.lab.store import open_db, records


def test_records_functions_never_auto_commit(tmp_path: Path) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    writer = open_db(db_file)
    reader = open_db(db_file)
    try:
        seq = records.next_operation_seq(writer)
        records.insert_operation(
            writer, id=f"WL-1000-{seq}", seq=seq, device="shaker", action="SetClamp",
            params={"clamp": True}, status="succeeded", started_at=1.0,
        )
        records.upsert_labware(writer, id="LW-001", type="pcr_plate_96", position="shaker")
        records.insert_labware_event(writer, labware_id="LW-001", at=1.0, what="placed", by="human")
        assert writer.in_transaction is True
        assert records.list_operations(reader) == []
        assert records.list_labware(reader) == []

        writer.rollback()
        assert records.list_operations(writer) == []
        assert records.list_labware(writer) == []
    finally:
        writer.close()
        reader.close()


def test_operation_seq_uniqueness_across_fixed_clock_and_reopen(tmp_path: Path) -> None:
    db_file = tmp_path / "wetlab.sqlite"

    conn1 = open_db(db_file)
    with conn1:
        seq1 = records.next_operation_seq(conn1)
        records.insert_operation(
            conn1, id=f"WL-0-{seq1}", seq=seq1, device="pcr", action="GetPcrStatus",
            params={}, status="succeeded", started_at=0.0,
        )
        seq2 = records.next_operation_seq(conn1)
        records.insert_operation(
            conn1, id=f"WL-0-{seq2}", seq=seq2, device="pcr", action="OpenDoor",
            params={}, status="succeeded", started_at=0.0,
        )
    conn1.close()

    conn2 = open_db(db_file)
    try:
        with conn2:
            seq3 = records.next_operation_seq(conn2)
            records.insert_operation(
                conn2, id=f"WL-0-{seq3}", seq=seq3, device="pcr", action="CloseDoor",
                params={"WaitLid": False}, status="succeeded", started_at=0.0,
            )
        ops = records.list_operations(conn2)
        assert [op["id"] for op in ops] == ["WL-0-1", "WL-0-2", "WL-0-3"]
        assert [op["seq"] for op in ops] == [1, 2, 3]
    finally:
        conn2.close()


def test_insert_update_get_list_and_last_operation() -> None:
    conn = open_db(":memory:")
    try:
        with conn:
            conn.execute("INSERT INTO missions (id, title, input_text, mode, status, created_at) VALUES ('M-1', 't', 'i', 'virtual', 'open', 0.0)")
            conn.execute("INSERT INTO experiments (id, mission_id, n, task_json, status) VALUES ('E-1', 'M-1', 1, '{}', 'open')")
            conn.execute("INSERT INTO protocols (id, experiment_id, version, content_json, fingerprint, status, submitted_at) VALUES ('P-1', 'E-1', 1, '{}', 'fp', 'running', 0.0)")
            conn.execute("INSERT INTO steps (protocol_id, n, kind, content_json, status) VALUES ('P-1', 1, 'skill', '{}', 'sent')")

            records.insert_operation(
                conn, id="WL-1000-1", seq=1, protocol_id="P-1", step_n=1, device="pcr",
                action="RunProgram", params={"Steps": '[{"HoldSeconds": 30}]'},
                status="succeeded", result={"success": True}, started_at=10.0, elapsed_s=0.5,
            )
            records.insert_operation(
                conn, id="WL-1000-2", seq=2, protocol_id="P-1", step_n=1, device="pcr",
                action="RunProgram", params={"Steps": '[{"HoldSeconds": 60}]'},
                status="succeeded", result={"success": True}, started_at=10.0, elapsed_s=0.4,
            )
            records.insert_operation(
                conn, id="WL-2000-3", seq=3, device="pcr", action="RunProgram",
                params={"Steps": "[]"}, status="blocked", error_code="INTERLOCK_BLOCKED",
                started_at=20.0, elapsed_s=0.0,
            )
            records.insert_operation(
                conn, id="WL-3000-4", seq=4, device="shaker", action="StartShake",
                params={"rpm": 800}, status="sent", started_at=30.0,
            )
            records.update_operation(
                conn, "WL-3000-4", status="failed", elapsed_s=1.2,
                result={"ok": False}, error_code="DEVICE_FAILED",
            )

        last_pcr = records.last_operation(conn, "pcr", "RunProgram")
        assert last_pcr is not None
        assert last_pcr["id"] == "WL-1000-2"
        assert last_pcr["params"] == {"Steps": '[{"HoldSeconds": 60}]'}
        assert last_pcr["started_at"] == 10.0

        assert records.last_operation(conn, "shaker", "StartShake") is None
        updated = records.get_operation(conn, "WL-3000-4")
        assert updated is not None
        assert (updated["status"], updated["error_code"], updated["elapsed_s"]) == ("failed", "DEVICE_FAILED", 1.2)
        assert updated["result"] == {"ok": False}

        assert [op["id"] for op in records.list_operations(conn, protocol_id="P-1", step_n=1)] == ["WL-1000-1", "WL-1000-2"]
        assert [op["id"] for op in records.list_operations(conn, device="shaker")] == ["WL-3000-4"]

        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                records.insert_operation(
                    conn, id="WL-4000-5", seq=5, protocol_id=None, step_n=1,
                    device="pcr", action="OpenDoor", params={}, status="succeeded", started_at=40.0,
                )
    finally:
        conn.close()


def test_labware_positions_and_events_history() -> None:
    conn = open_db(":memory:")
    try:
        with conn:
            records.upsert_labware(conn, id="LW-001", type="pcr_plate_96", position="rack:low")
            records.insert_labware_event(
                conn, labware_id="LW-001", at=1.0, what="placed", by="human", detail={"position": "rack:low"}
            )
            records.upsert_labware(conn, id="LW-002", type="pcr_plate_96", position="shaker")

        assert records.get_labware(conn, "LW-001") == {"id": "LW-001", "type": "pcr_plate_96", "position": "rack:low"}
        assert records.get_labware_at(conn, "rack:low") == {"id": "LW-001", "type": "pcr_plate_96", "position": "rack:low"}
        assert records.get_labware_at(conn, "pcr") is None

        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                records.upsert_labware(conn, id="LW-003", type="pcr_plate_96", position="shaker")

        with conn:
            records.insert_operation(
                conn, id="WL-5000-1", seq=1, device="arm", action="MovMulti",
                params={"to": "pcr"}, status="succeeded", started_at=5.0, elapsed_s=41.0,
            )
            records.update_labware_position(conn, "LW-001", "pcr")
            records.insert_labware_event(
                conn, labware_id="LW-001", at=46.0, what="moved", by="device",
                detail={"from": "rack:low", "to": "pcr"}, operation_id="WL-5000-1",
            )
            records.update_labware_position(conn, "LW-001", None)
            records.insert_labware_event(conn, labware_id="LW-001", at=100.0, what="removed", by="human")
            records.update_labware_position(conn, "LW-002", None)

        assert records.list_labware(conn, active_only=True) == []
        assert len(records.list_labware(conn, active_only=False)) == 2

        events = records.list_labware_events(conn, "LW-001")
        assert [e["what"] for e in events] == ["placed", "moved", "removed"]
        assert events[1]["operation_id"] == "WL-5000-1"
        assert events[1]["detail"] == {"from": "rack:low", "to": "pcr"}
        assert events[0]["by"] == "human"
    finally:
        conn.close()
