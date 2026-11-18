"""Tests for island/GW_001/positions.json, devices/positions.py, and LABWARE_POSITIONS (#35)."""

import hashlib
import json
from pathlib import Path
import shutil

from pydantic import ValidationError
import pytest

from wetlab.lab.devices import (
    LABWARE_POSITIONS,
    POINT_TO_POSITION,
    POSITION_DEVICE,
    POSITIONS,
    TAUGHT_POINTS,
)
from wetlab.lab.devices.positions import default_island_dir, load_positions, positions_hash
from wetlab.lab.errors import Blocked
from wetlab.lab.materials import Materials
from wetlab.lab.protocol import validate
from wetlab.lab.sim import Scenario
from wetlab.lab.store import open_db

REPO_ROOT = Path(__file__).resolve().parents[3]
GW001_DIR = REPO_ROOT / "island" / "GW_001"


def _copy_island(tmp_path: Path, name: str = "custom") -> Path:
    dest = tmp_path / name
    shutil.copytree(GW001_DIR, dest)
    return dest


def test_gw001_positions_loaded_and_derived_tables_exclude_non_labware() -> None:
    assert default_island_dir() == GW001_DIR
    assert set(POSITIONS) == {
        "rack:1-2",
        "rack:1-3",
        "pcr",
        "shaker",
        "turntable",
        "arm:home",
    }
    assert set(LABWARE_POSITIONS) == {"rack:1-2", "rack:1-3", "pcr", "shaker"}
    assert "turntable" not in LABWARE_POSITIONS
    assert "arm:home" not in LABWARE_POSITIONS

    r12 = POSITIONS["rack:1-2"]
    assert (r12.label, r12.device, r12.point, r12.posture, r12.index, r12.holds_labware) == (
        "料架 1-2",
        "rack",
        "2120761428308422658",
        0,
        "1-2",
        True,
    )
    r13 = POSITIONS["rack:1-3"]
    assert (r13.label, r13.device, r13.point, r13.posture, r13.index, r13.holds_labware) == (
        "料架 1-3",
        "rack",
        "2118246861233086466",
        0,
        "1-3",
        True,
    )
    pcr_pos = POSITIONS["pcr"]
    assert (
        pcr_pos.label,
        pcr_pos.device,
        pcr_pos.point,
        pcr_pos.posture,
        pcr_pos.index,
        pcr_pos.holds_labware,
    ) == ("PCR仪", "pcr", "2118260185060499457", 90, "1-1", True)

    home = POSITIONS["arm:home"]
    assert (home.label, home.device, home.point, home.posture, home.index, home.holds_labware) == (
        "机械臂原位",
        "arm",
        "2120809646631186433",
        0,
        "1-1",
        False,
    )

    assert POSITION_DEVICE["pcr"].spec.NAME == "pcr"
    assert POSITION_DEVICE["rack:1-3"].spec.NAME == "rack"
    assert POSITION_DEVICE["arm:home"].spec.NAME == "arm"
    assert POINT_TO_POSITION["2120809646631186433"] == "arm:home"
    assert TAUGHT_POINTS["2120809646631186433"] == home


def test_positions_hash_recomputes_from_disk_and_wetlab_island_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = hashlib.sha256((GW001_DIR / "positions.json").read_bytes()).hexdigest()
    assert positions_hash() == expected

    copied = _copy_island(tmp_path)
    h1 = positions_hash(copied)
    assert h1 == expected
    pos_file = copied / "positions.json"
    pos_file.write_text(pos_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    h2 = positions_hash(copied)
    assert h2 != h1

    monkeypatch.setenv("WETLAB_ISLAND", "GW_999")
    assert default_island_dir() == REPO_ROOT / "island" / "GW_999"


def test_load_positions_rejects_missing_vendor_bad_sha_missing_point_and_posture_mismatch(
    tmp_path: Path,
) -> None:
    # 1. Missing vendor file
    missing_vendor = _copy_island(tmp_path, "missing_vendor")
    (missing_vendor / "vendor" / "Position_20261118.json").unlink()
    with pytest.raises((FileNotFoundError, ValueError), match="Position_20261118.json"):
        load_positions(missing_vendor)

    # 2. Vendor SHA-256 mismatch
    bad_sha = _copy_island(tmp_path, "bad_sha")
    v_file = bad_sha / "vendor" / "Position_20261118.json"
    v_file.write_bytes(v_file.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="sha256"):
        load_positions(bad_sha)

    # 3. Point missing from vendor file (with updated source sha256)
    missing_pt = _copy_island(tmp_path, "missing_pt")
    v_pt = missing_pt / "vendor" / "Position_20261118.json"
    pts = json.loads(v_pt.read_text(encoding="utf-8-sig"))
    pts_filtered = [p for p in pts if str(p["id"]) != "2118260185060499457"]
    v_bytes = json.dumps(pts_filtered, ensure_ascii=False).encode("utf-8")
    v_pt.write_bytes(v_bytes)
    cfg = json.loads((missing_pt / "positions.json").read_text(encoding="utf-8"))
    cfg["source"]["sha256"] = hashlib.sha256(v_bytes).hexdigest()
    (missing_pt / "positions.json").write_text(
        json.dumps(cfg, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="2118260185060499457"):
        load_positions(missing_pt)

    # 4. Posture mismatch with vendor file
    bad_posture = _copy_island(tmp_path, "bad_posture")
    cfg_pos = json.loads((bad_posture / "positions.json").read_text(encoding="utf-8"))
    cfg_pos["pcr"]["posture"] = 0
    (bad_posture / "positions.json").write_text(
        json.dumps(cfg_pos, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="posture"):
        load_positions(bad_posture)


def test_non_labware_positions_rejected_by_materials_validate_and_scenario() -> None:
    conn = open_db(":memory:")
    mat = Materials(conn)
    try:
        mat.place("LW-001", "pcr_plate_96", "pcr")
        for non_lw in ("turntable", "arm:home"):
            with pytest.raises(Blocked):
                mat.place("LW-002", "pcr_plate_96", non_lw)
            with pytest.raises(Blocked):
                mat.correct("LW-001", non_lw)
            with pytest.raises(Blocked):
                mat.move("LW-001", non_lw)

            issues = validate(
                {
                    "name": "bad_pos",
                    "labware": {"LW-002": non_lw},
                    "steps": [{"skill": "pcr_open", "args": {}}],
                }
            )
            assert any(non_lw in msg for msg in issues)

            with pytest.raises(ValidationError):
                Scenario(labware={"LW-002": non_lw})
    finally:
        conn.close()
