"""Tests for arm transfer skill and dangerous cross-device ordering blocks."""

from wetlab.lab import registry
from wetlab.lab.lab import Lab
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.store import open_db


def test_transfer_succeeds_when_ready_and_blocks_dangerous_orders():
    conn = open_db(":memory:")
    backend = SimBackend(Scenario(labware={"LW-001": "rack:low"}))
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "96_plate", "rack:low")

    # 1. Dangerous order: transfer into PCR without opening door -> blocked, 0 accidents.
    blocked_pcr = registry.call(lab, "transfer", labware_id="LW-001", to_position="pcr")
    assert blocked_pcr["status"] == "blocked"
    assert blocked_pcr["error"]["code"] == "INTERLOCK_BLOCKED"
    assert backend.world.accidents == []
    assert lab.materials.where("LW-001") == "rack:low"

    # 2. Open PCR door, then transfer succeeds and updates both truth and SQLite ledger.
    assert registry.call(lab, "pcr_open")["ok"] is True
    moved = registry.call(lab, "transfer", labware_id="LW-001", to_position="pcr")
    assert moved["ok"] is True
    assert moved["state_after"]["labware"] == {"LW-001": "pcr"}
    assert backend.world.plates["LW-001"] == "pcr"
    assert backend.world.accidents == []

    # 3. Dangerous order: transfer into shaker while shaker is spinning -> blocked, 0 accidents.
    assert registry.call(lab, "shake_start", rpm=500)["ok"] is True
    blocked_shaker = registry.call(lab, "transfer", labware_id="LW-001", to_position="shaker")
    assert blocked_shaker["status"] == "blocked"
    assert backend.world.accidents == []

    # 4. Occupied target position in ledger -> blocked before any MovMulti is sent.
    assert registry.call(lab, "shake_stop")["ok"] is True
    lab.materials.place("LW-002", "96_plate", "shaker")
    occupied = registry.call(lab, "transfer", labware_id="LW-001", to_position="shaker")
    assert occupied["status"] == "blocked"
    assert occupied["operation_ids"] == []
    assert backend.world.accidents == []
