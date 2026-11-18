"""Tests for wetlab.lab.materials (labware ledger and history, 04 section 5.7)."""

import pytest

from wetlab.lab.errors import Blocked, SkillFailed
from wetlab.lab.materials import Materials
from wetlab.lab.store import open_db, records


def test_place_move_record_correct_remove_and_history() -> None:
    conn = open_db(":memory:")
    clock = [100.0]
    mat = Materials(conn, now=lambda: clock[0])
    try:
        assert mat.where("LW-001") is None
        assert mat.at("rack:1-3") is None

        # Place LW-001 on rack:1-3 and LW-002 on shaker
        mat.place("LW-001", "pcr_plate_96", "rack:1-3")
        clock[0] = 105.0
        mat.place("LW-002", "pcr_plate_96", "shaker")

        assert mat.where("LW-001") == "rack:1-3"
        assert mat.at("rack:1-3") == "LW-001"
        assert mat.at("pcr") is None

        # Occupying an already-occupied position or re-placing an active plate -> Blocked
        with pytest.raises(Blocked, match="occupied"):
            mat.place("LW-003", "pcr_plate_96", "shaker")
        with pytest.raises(Blocked, match="already on the island"):
            mat.place("LW-001", "pcr_plate_96", "pcr")
        with pytest.raises(Blocked, match="unknown position"):
            mat.place("LW-003", "pcr_plate_96", "washer")

        # Moving to an occupied position -> Blocked; moving to empty position -> updates & logs
        with pytest.raises(Blocked, match="occupied"):
            mat.move("LW-001", "shaker")

        with conn:
            records.insert_operation(
                conn, id="WL-110000-1", seq=1, device="arm", action="MovMulti",
                params={}, status="succeeded", started_at=110.0, elapsed_s=41.0,
            )
        clock[0] = 151.0
        mat.move("LW-001", "pcr", operation_id="WL-110000-1")
        assert mat.where("LW-001") == "pcr"
        assert mat.at("rack:1-3") is None
        assert mat.at("pcr") == "LW-001"

        # record_at records on occupied position and is a no-op on empty position
        clock[0] = 160.0
        mat.record_at("pcr", "pcr_program_completed", cycles=23)
        mat.record_at("rack:1-3", "ignored_empty")

        # correct: conflict with another plate -> Blocked; same position or new position -> recorded by human
        with pytest.raises(Blocked, match="occupied"):
            mat.correct("LW-001", "shaker")
        clock[0] = 170.0
        mat.correct("LW-001", "pcr")
        clock[0] = 175.0
        mat.correct("LW-001", "rack:1-2")
        assert mat.where("LW-001") == "rack:1-2"

        # remove: clears position and logs human removal; second remove -> Blocked; re-place works
        clock[0] = 180.0
        mat.remove("LW-001")
        assert mat.where("LW-001") is None
        assert mat.at("rack:1-2") is None
        with pytest.raises(Blocked, match="not on the island"):
            mat.remove("LW-001")
        with pytest.raises(Blocked, match="not on the island"):
            mat.move("LW-001", "pcr")

        clock[0] = 190.0
        mat.place("LW-001", "pcr_plate_96", "pcr")
        assert mat.where("LW-001") == "pcr"

        hist = mat.history("LW-001")
        assert [(e["what"], e["by"], e["at"]) for e in hist] == [
            ("placed", "human", 100.0),
            ("moved", "device", 151.0),
            ("pcr_program_completed", "device", 160.0),
            ("corrected", "human", 170.0),
            ("corrected", "human", 175.0),
            ("removed", "human", 180.0),
            ("placed", "human", 190.0),
        ]
        assert hist[1]["operation_id"] == "WL-110000-1"
        assert hist[2]["detail"] == {"cycles": 23}
    finally:
        conn.close()


def test_materials_participates_in_outer_transaction_rollback_and_skill_failed_error() -> None:
    err = SkillFailed("PROGRAM_ENDED_EARLY", "stopped at 12s")
    assert err.code == "PROGRAM_ENDED_EARLY"
    assert str(err) == "stopped at 12s"

    conn = open_db(":memory:")
    mat = Materials(conn, now=lambda: 1.0)
    try:
        with pytest.raises(RuntimeError, match="abort command"):
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                mat.place("LW-001", "pcr_plate_96", "pcr")
                raise RuntimeError("abort command")
        assert mat.where("LW-001") is None
        assert mat.history("LW-001") == []
    finally:
        conn.close()
