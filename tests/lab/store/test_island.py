"""Tests for wetlab.lab.store.island (device_state, freeze, and meta helpers)."""

from pathlib import Path
import sqlite3

import pytest

from wetlab.lab.store import island, open_db, records


def test_freeze_seeded_single_row_and_transitions() -> None:
    conn = open_db(":memory:")
    try:
        assert island.get_freeze(conn) == {
            "frozen": False,
            "reason": None,
            "operation_id": None,
            "at": None,
        }

        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("INSERT INTO freeze (frozen) VALUES (0)")
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute("DELETE FROM freeze")

        with conn:
            island.set_frozen(conn, reason="GATEWAY_OFFLINE", at=12.5)
        assert island.get_freeze(conn) == {
            "frozen": True,
            "reason": "GATEWAY_OFFLINE",
            "operation_id": None,
            "at": 12.5,
        }

        with conn:
            island.clear_frozen(conn)
        assert island.get_freeze(conn)["frozen"] is False

        with conn:
            island.set_frozen(
                conn,
                reason="VERIFY_FAILED",
                at=30.0,
                operation_id="WL-30000-1",
            )
            records.insert_operation(
                conn,
                id="WL-30000-1",
                seq=1,
                device="pcr",
                action="OpenDoor",
                params={},
                status="unknown",
                error_code="VERIFY_FAILED",
                started_at=15.0,
                elapsed_s=15.0,
            )
        assert island.get_freeze(conn)["operation_id"] == "WL-30000-1"

        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                island.set_frozen(
                    conn,
                    reason="TIMEOUT_UNKNOWN",
                    at=40.0,
                    operation_id="WL-NONEXISTENT",
                )
    finally:
        conn.close()


def test_device_state_and_meta_helpers_non_committing(tmp_path: Path) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    writer = open_db(db_file, mode="virtual")
    reader = open_db(db_file, mode="virtual")
    try:
        assert island.get_island_mode(reader) == "virtual"
        assert island.get_db_id(writer) == island.get_db_id(reader)
        assert island.get_closed_skills(reader) == set()
        assert island.get_meta(reader, "custom_key") is None

        island.upsert_device_state(
            writer,
            device="pcr",
            props={"DoorState": "Closed", "PcrState": "Idle"},
            updated_at=5.0,
        )
        island.set_closed_skills(writer, ["transfer", "pcr_start_program", "transfer"])
        island.set_meta(writer, "custom_key", "v1")
        assert writer.in_transaction is True
        assert island.get_all_device_states(reader) == {}
        assert island.get_closed_skills(reader) == set()

        writer.commit()
        assert island.get_device_state(reader, "pcr") == {
            "device": "pcr",
            "props": {"DoorState": "Closed", "PcrState": "Idle"},
            "error": None,
            "updated_at": 5.0,
        }
        assert island.get_closed_skills(reader) == {"pcr_start_program", "transfer"}
        assert island.get_meta(reader, "custom_key") == "v1"

        with writer:
            island.set_closed_skills(writer, [])
            island.set_meta(writer, "custom_key", "v2")
            island.upsert_device_state(
                writer,
                device="_gateway",
                props={"online": True, "busy_by_others": False},
                updated_at=8.0,
            )

        assert island.get_closed_skills(reader) == set()
        assert island.get_meta(reader, "custom_key") == "v2"
        assert island.get_device_state(reader, "nonexistent") is None
    finally:
        writer.close()
        reader.close()
