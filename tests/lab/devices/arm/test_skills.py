"""Tests for arm transfer skill, Index rules, capture alignment, and non-labware blocks (#37)."""

import json
from pathlib import Path
from typing import Any

import pytest

from wetlab.lab import registry
from wetlab.lab.devices import POSITIONS
from wetlab.lab.errors import Blocked
from wetlab.lab.lab import Lab
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.store import open_db, records

REPO_ROOT = Path(__file__).resolve().parents[4]
ARM_CAPTURE = REPO_ROOT / "captures" / "20261118_arm_141121.jsonl"


def _load_capture_movmulti_payloads() -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in ARM_CAPTURE.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        body = json.loads(row["payload"])
        if body.get("GatewayCode") == "GW_001" and body.get("Type") == "MovMulti":
            payloads.append(body["Body"]["MovMulti"])
    return payloads


def test_transfer_succeeds_when_ready_and_blocks_dangerous_orders() -> None:
    conn = open_db(":memory:")
    backend = SimBackend(Scenario(labware={"LW-001": "rack:1-3"}))
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "96_plate", "rack:1-3")

    # 1. Dangerous order: transfer into PCR without opening door -> blocked, 0 accidents.
    blocked_pcr = registry.call(lab, "transfer", labware_id="LW-001", to_position="pcr")
    assert blocked_pcr["status"] == "blocked"
    assert blocked_pcr["error"]["code"] == "INTERLOCK_BLOCKED"
    assert backend.world.accidents == []
    assert lab.materials.where("LW-001") == "rack:1-3"

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


def test_transfer_material_index_and_vendor_capture_alignment() -> None:
    vendor_moves = _load_capture_movmulti_payloads()
    assert len(vendor_moves) >= 2
    vendor_to_pcr = vendor_moves[0]
    vendor_from_pcr = next(
        m for m in vendor_moves if m["Tasks"][0]["PositionId"] == int(POSITIONS["pcr"].point)
    )

    conn = open_db(":memory:")
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "rack:1-3"},
            devices={"pcr": {"DoorState": "Open", "LidMotorState": "Lifted", "PcrState": "Idle"}},
        )
    )
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "96_plate", "rack:1-3")

    recorded_expects: list[dict[str, Any] | None] = []
    orig_do = lab.do

    def spy_do(
        device: str,
        action: str,
        params: dict[str, Any] | None = None,
        expect: dict[str, Any] | None = None,
        within_s: float = 15,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        if device == "arm" and action == "MovMulti":
            recorded_expects.append(expect)
        return orig_do(
            device,
            action,
            params,
            expect=expect,
            within_s=within_s,
            timeout_s=timeout_s,
        )

    lab.do = spy_do  # type: ignore[method-assign]

    # 1. rack:1-3 -> pcr: exact match with vendor capture MovMulti (Index == "1-3").
    r1 = registry.call(lab, "transfer", labware_id="LW-001", to_position="pcr")
    assert r1["ok"] is True
    op1 = records.last_operation(conn, "arm", "MovMulti")
    assert op1 is not None
    assert op1["params"] == vendor_to_pcr
    assert op1["params"]["Material"]["Index"] == "1-3"
    assert all(type(t["PositionId"]) is int for t in op1["params"]["Tasks"])
    assert recorded_expects[-1] == {"RobotMode": "使能空闲", "SafetyStatus": "NORMAL"}

    # 2. pcr -> rack:1-3: exact match with vendor return capture MovMulti (Index == "1-3").
    r2 = registry.call(lab, "transfer", labware_id="LW-001", to_position="rack:1-3")
    assert r2["ok"] is True
    op2 = records.last_operation(conn, "arm", "MovMulti")
    assert op2 is not None
    assert op2["params"] == vendor_from_pcr
    assert op2["params"]["Material"]["Index"] == "1-3"

    # 3. rack:1-3 -> rack:1-2: source rack index ("1-3") used; then rack:1-2 -> pcr ("1-2").
    r3 = registry.call(lab, "transfer", labware_id="LW-001", to_position="rack:1-2")
    assert r3["ok"] is True
    op3 = records.last_operation(conn, "arm", "MovMulti")
    assert op3 is not None
    assert op3["params"]["Material"]["Index"] == "1-3"

    r4 = registry.call(lab, "transfer", labware_id="LW-001", to_position="pcr")
    assert r4["ok"] is True
    op4 = records.last_operation(conn, "arm", "MovMulti")
    assert op4 is not None
    assert op4["params"]["Material"]["Index"] == "1-2"
    expected_r12_to_pcr = {
        "Tasks": [
            {"PauseTime": 0, "PositionId": int(POSITIONS["rack:1-2"].point), "ClawSwitch": 0},
            {"PauseTime": 0, "PositionId": int(POSITIONS["pcr"].point), "ClawSwitch": 1},
        ],
        "Material": {**vendor_to_pcr["Material"], "Index": "1-2"},
    }
    assert op4["params"] == expected_r12_to_pcr

    # 4. pcr -> shaker: neither side is rack -> Index == "1-1".
    r5 = registry.call(lab, "transfer", labware_id="LW-001", to_position="shaker")
    assert r5["ok"] is True
    op5 = records.last_operation(conn, "arm", "MovMulti")
    assert op5 is not None
    assert op5["params"]["Material"]["Index"] == "1-1"
    expected_pcr_to_shaker = {
        "Tasks": [
            {"PauseTime": 0, "PositionId": int(POSITIONS["pcr"].point), "ClawSwitch": 0},
            {"PauseTime": 0, "PositionId": int(POSITIONS["shaker"].point), "ClawSwitch": 1},
        ],
        "Material": {**vendor_to_pcr["Material"], "Index": "1-1"},
    }
    assert op5["params"] == expected_pcr_to_shaker


def test_transfer_and_materials_reject_turntable_and_arm_home() -> None:
    conn = open_db(":memory:")
    backend = SimBackend(Scenario(labware={"LW-001": "rack:1-3"}))
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "96_plate", "rack:1-3")

    for forbidden in ("turntable", "arm:home"):
        with pytest.raises(Blocked):
            lab.materials.place("LW-002", "96_plate", forbidden)

        res = registry.call(lab, "transfer", labware_id="LW-001", to_position=forbidden)
        assert res["ok"] is False
        assert res["status"] == "blocked"
        assert res["operation_ids"] == []
        assert lab.materials.where("LW-001") == "rack:1-3"
        assert backend.world.plates["LW-001"] == "rack:1-3"

    assert records.list_operations(conn) == []
