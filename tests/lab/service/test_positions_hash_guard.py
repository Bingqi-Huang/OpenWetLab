"""Tests for recording `positions_hash` on approve and guarding `transfer` steps (#38)."""

import json
from pathlib import Path
import shutil

import pytest

from wetlab.lab.devices import positions_hash
from wetlab.lab.service import Service, create_virtual_service
from wetlab.lab.store import inbox, records, runs

REPO_ROOT = Path(__file__).resolve().parents[3]
GW001_DIR = REPO_ROOT / "island" / "GW_001"


def _enqueue(
    svc: Service,
    kind: str,
    payload: dict,
    *,
    key: str,
    target_id: str | None = None,
) -> None:
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=key,
            at=svc.backend.now(),
            who="researcher",
            kind=kind,
            target_id=target_id,
            payload=payload,
        )


def test_approve_records_positions_hash_and_blocks_transfer_when_positions_json_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    island_copy = tmp_path / "island_gw001"
    shutil.copytree(GW001_DIR, island_copy)
    monkeypatch.setenv("WETLAB_ISLAND_DIR", str(island_copy))
    initial_hash = positions_hash()

    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        startup_op_count = len(records.list_operations(svc.conn))

        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "rack:1-3"},
            key="k-lw",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-20261118-038", "title": "点位哈希核对", "input_text": "搬运板"},
            key="k-m",
        )
        xfer_proto = {
            "name": "料架搬至振荡模组",
            "labware": {"LW-001": "rack:1-3"},
            "steps": [
                {"skill": "transfer", "args": {"labware_id": "LW-001", "to_position": "shaker"}}
            ],
        }
        _enqueue(
            svc,
            "submit_protocol",
            {"mission_id": "M-20261118-038", "protocol": xfer_proto},
            key="k-sub",
        )
        svc.step()

        submitted = runs.get_protocol(svc.conn, "E-001-v1")
        assert submitted is not None
        assert submitted["status"] == "submitted"
        assert submitted["positions_hash"] is None

        # Approve and immediately pause in the same command batch before advancing steps.
        _enqueue(
            svc,
            "approve",
            {
                "protocol_id": "E-001-v1",
                "decided_by": "林研究员",
                "fingerprint": submitted["fingerprint"],
            },
            key="k-app",
        )
        _enqueue(svc, "pause", {"protocol_id": "E-001-v1"}, key="k-pause")
        svc.step()

        approved = runs.get_protocol(svc.conn, "E-001-v1")
        assert approved is not None
        assert approved["positions_hash"] == initial_hash

        # Modify `positions.json` on disk after approval, then resume and advance service.
        pos_file = island_copy / "positions.json"
        cfg = json.loads(pos_file.read_text(encoding="utf-8"))
        cfg["shaker"]["label"] = "恒温振荡模组(重示教)"
        pos_file.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert positions_hash() != initial_hash

        _enqueue(svc, "resume", {"protocol_id": "E-001-v1"}, key="k-resume")
        svc.step()

        # Zero device commands issued; step and protocol marked failed with exact message.
        assert records.list_operations(svc.conn, protocol_id="E-001-v1") == []
        assert len(records.list_operations(svc.conn)) == startup_op_count

        failed_proto = runs.get_protocol(svc.conn, "E-001-v1")
        assert failed_proto is not None
        assert failed_proto["status"] == "failed"

        step1 = runs.get_step(svc.conn, "E-001-v1", 1)
        assert step1 is not None
        assert step1["status"] == "failed"
        step_err = (step1["result"] or {}).get("error") or {}
        assert "示教点位已更新，请重新预演并批准" in step_err.get("message", "")

        fail_events = [
            ev
            for ev in inbox.list_events_after(svc.conn, 0)
            if ev["kind"] == "protocol_failed" and ev["ref_id"] == "E-001-v1"
        ]
        assert len(fail_events) == 1
        ev_err = fail_events[0]["payload"]["error"] or {}
        assert "示教点位已更新，请重新预演并批准" in ev_err.get("message", "")
        lw1 = records.get_labware(svc.conn, "LW-001")
        assert lw1 is not None and lw1["position"] == "rack:1-3"
    finally:
        svc.conn.close()


def test_unchanged_positions_hash_allows_transfer_step_to_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    island_copy = tmp_path / "island_gw001"
    shutil.copytree(GW001_DIR, island_copy)
    monkeypatch.setenv("WETLAB_ISLAND_DIR", str(island_copy))

    svc = create_virtual_service(tmp_path / "wetlab_ok.sqlite", runs_dir=tmp_path / "runs_ok")
    try:
        svc.startup()
        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-002", "type": "96_plate", "position": "rack:1-3"},
            key="lw",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-20261118-039", "title": "点位未变", "input_text": "正常搬运"},
            key="m",
        )
        xfer_proto = {
            "name": "正常搬运",
            "labware": {"LW-002": "rack:1-3"},
            "steps": [
                {"skill": "transfer", "args": {"labware_id": "LW-002", "to_position": "shaker"}}
            ],
        }
        _enqueue(
            svc,
            "submit_protocol",
            {"mission_id": "M-20261118-039", "protocol": xfer_proto},
            key="sub",
        )
        svc.step()
        sub = runs.get_protocol(svc.conn, "E-001-v1")
        assert sub is not None
        _enqueue(
            svc,
            "approve",
            {
                "protocol_id": "E-001-v1",
                "decided_by": "林研究员",
                "fingerprint": sub["fingerprint"],
            },
            key="app",
        )
        svc.step()

        done_proto = runs.get_protocol(svc.conn, "E-001-v1")
        assert done_proto is not None
        assert done_proto["status"] == "done"
        assert done_proto["positions_hash"] == positions_hash()
        lw2 = records.get_labware(svc.conn, "LW-002")
        assert lw2 is not None and lw2["position"] == "shaker"
    finally:
        svc.conn.close()


def test_positions_json_changed_mid_protocol_blocks_upcoming_transfer_step(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    island_copy = tmp_path / "island_gw001"
    shutil.copytree(GW001_DIR, island_copy)
    monkeypatch.setenv("WETLAB_ISLAND_DIR", str(island_copy))

    svc = create_virtual_service(tmp_path / "wetlab_mid.sqlite", runs_dir=tmp_path / "runs_mid")
    try:
        svc.startup()
        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-003", "type": "96_plate", "position": "rack:1-3"},
            key="lw",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-20261118-040", "title": "中途改点位", "input_text": "开门后搬板"},
            key="m",
        )
        proto = {
            "name": "开门后搬进PCR",
            "labware": {"LW-003": "rack:1-3"},
            "steps": [
                {"skill": "pcr_open", "args": {}},
                {"skill": "transfer", "args": {"labware_id": "LW-003", "to_position": "pcr"}},
            ],
        }
        _enqueue(
            svc,
            "submit_protocol",
            {"mission_id": "M-20261118-040", "protocol": proto},
            key="sub",
        )
        svc.step()
        sub = runs.get_protocol(svc.conn, "E-001-v1")
        assert sub is not None
        _enqueue(
            svc,
            "approve",
            {
                "protocol_id": "E-001-v1",
                "decided_by": "林研究员",
                "fingerprint": sub["fingerprint"],
            },
            key="app",
        )
        # First tick runs step 1 (pcr_open -> done).
        svc.step()
        step1 = runs.get_step(svc.conn, "E-001-v1", 1)
        assert step1 is not None and step1["status"] == "done"

        # Modify positions.json before step 2 (transfer) executes.
        pos_file = island_copy / "positions.json"
        pos_file.write_text(pos_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")

        svc.step()
        step2 = runs.get_step(svc.conn, "E-001-v1", 2)
        assert step2 is not None and step2["status"] == "failed"
        err = (step2["result"] or {}).get("error") or {}
        assert "示教点位已更新，请重新预演并批准" in err.get("message", "")
        arm_ops = [
            op
            for op in records.list_operations(svc.conn, protocol_id="E-001-v1")
            if op["device"] == "arm"
        ]
        assert arm_ops == []
        failed_proto = runs.get_protocol(svc.conn, "E-001-v1")
        assert failed_proto is not None and failed_proto["status"] == "failed"
    finally:
        svc.conn.close()
