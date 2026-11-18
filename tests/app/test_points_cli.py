"""Tests for `wetlab points import <file>` validation, diffing, and archiving (#36)."""

import hashlib
import io
import json
from pathlib import Path
import shutil
import socket

import pytest

from wetlab.app.cli import main
from wetlab.lab.devices.positions import load_positions

REPO_ROOT = Path(__file__).resolve().parents[2]
GW001_DIR = REPO_ROOT / "island" / "GW_001"
VENDOR_20261118 = GW001_DIR / "vendor" / "Position_20261118.json"


def _build_20261117_island(tmp_path: Path) -> Path:
    """Reconstruct 2026-11-17 vendor points in `tmp_path` from real 2026-11-18 data (04 §5.16)."""
    island_dir = tmp_path / "GW_001"
    shutil.copytree(GW001_DIR, island_dir)
    (island_dir / "vendor" / "Position_20261118.json").unlink()

    pts = json.loads(VENDOR_20261118.read_text(encoding="utf-8-sig"))
    old_pts = []
    for entry in pts:
        pt_id = str(entry["id"])
        if pt_id == "2122873256718376961":
            continue
        wps = json.loads(entry["positionData"])
        if pt_id == "2118246861233086466":
            wps[-1]["Pos"][2] = "367.016"
        elif pt_id == "2118251893923930114":
            wps[-1]["Pos"][0] = "-194.332"
            wps[-1]["Pos"][1] = "343.208"
            wps[-1]["Pos"][2] = "195.624"
        elif pt_id == "2118260185060499457":
            wps[-1]["Pos"][1] = "-224.346"
        copied = dict(entry)
        copied["positionData"] = json.dumps(wps, ensure_ascii=False, separators=(",", ":"))
        old_pts.append(copied)

    old_bytes = json.dumps(old_pts, ensure_ascii=False, indent=2).encode("utf-8")
    old_vendor = island_dir / "vendor" / "Position_20261117.json"
    old_vendor.write_bytes(old_bytes)

    pos_path = island_dir / "positions.json"
    cfg = json.loads(pos_path.read_text(encoding="utf-8"))
    cfg["source"] = {
        "file": "vendor/Position_20261117.json",
        "sha256": hashlib.sha256(old_bytes).hexdigest(),
    }
    pos_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert len(load_positions(island_dir)) == 6
    return island_dir


def test_points_import_20261117_to_20261118_exact_four_diffs_and_updates_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    island_dir = _build_20261117_island(tmp_path)
    pos_path = island_dir / "positions.json"
    pos_before = pos_path.read_bytes()

    def _forbid_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("wetlab points import must never perform network operations")

    monkeypatch.setattr(socket, "create_connection", _forbid_network)

    # 1. Answering 'n' prints the 4 diffs but leaves all files untouched.
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    rc_cancel = main(
        [
            "points",
            "import",
            str(VENDOR_20261118),
            "--date",
            "20261118",
            "--island-dir",
            str(island_dir),
        ]
    )
    assert rc_cancel != 0
    assert not (island_dir / "vendor" / "Position_20261118.json").exists()
    assert pos_path.read_bytes() == pos_before
    capsys.readouterr()

    # 2. Answering 'y' prints the exact 4 diffs and updates vendor/ + positions.json source.
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    rc_ok = main(
        [
            "points",
            "import",
            str(VENDOR_20261118),
            "--date",
            "20261118",
            "--island-dir",
            str(island_dir),
        ]
    )
    assert rc_ok == 0
    out = capsys.readouterr().out
    diff_lines = [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith(("[新增]", "[删除]", "[变更]"))
    ]
    assert len(diff_lines) == 4

    added_lines = [ln for ln in diff_lines if ln.startswith("[新增]")]
    removed_lines = [ln for ln in diff_lines if ln.startswith("[删除]")]
    changed_lines = [ln for ln in diff_lines if ln.startswith("[变更]")]
    assert len(added_lines) == 1
    assert removed_lines == []
    assert len(changed_lines) == 3

    assert "2122873256718376961" in added_lines[0]
    assert "MGIP8移液工作站" in added_lines[0]

    by_point = {ln.split()[1]: ln for ln in changed_lines}
    assert set(by_point) == {
        "2118246861233086466",
        "2118251893923930114",
        "2118260185060499457",
    }
    assert "Z 367.016 -> 365.016" in by_point["2118246861233086466"]
    tt_line = by_point["2118251893923930114"]
    assert "X -194.332 -> -190.828" in tt_line
    assert "Y 343.208 -> 346.215" in tt_line
    assert "Z 195.624 -> 194.754" in tt_line
    assert "Y -224.346 -> -226.05" in by_point["2118260185060499457"]

    for unchanged_id in (
        "2120761428308422658",
        "2120763995201499138",
        "2120809646631186433",
        "2122225115687583746",
    ):
        assert unchanged_id not in out

    archived = island_dir / "vendor" / "Position_20261118.json"
    assert archived.is_file()
    assert archived.read_bytes() == VENDOR_20261118.read_bytes()
    assert (island_dir / "vendor" / "Position_20261117.json").is_file()

    updated_cfg = json.loads(pos_path.read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(VENDOR_20261118.read_bytes()).hexdigest()
    assert updated_cfg["source"] == {
        "file": "vendor/Position_20261118.json",
        "sha256": expected_sha,
    }
    assert len(load_positions(island_dir)) == 6


def test_points_import_yes_flag_and_removed_unreferenced_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    island_dir = tmp_path / "GW_001"
    shutil.copytree(GW001_DIR, island_dir)
    monkeypatch.setenv("WETLAB_ISLAND_DIR", str(island_dir))

    pts = json.loads(VENDOR_20261118.read_text(encoding="utf-8-sig"))
    without_washer = [p for p in pts if str(p["id"]) != "2122225115687583746"]
    candidate = tmp_path / "export_no_washer.json"
    candidate.write_text(json.dumps(without_washer, ensure_ascii=False), encoding="utf-8")

    rc = main(["points", "import", str(candidate), "--yes", "--date", "20261119"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[删除] 2122225115687583746" in out
    cfg = json.loads((island_dir / "positions.json").read_text(encoding="utf-8"))
    assert cfg["source"]["file"] == "vendor/Position_20261119.json"
    assert (island_dir / "vendor" / "Position_20261119.json").is_file()


def test_points_import_rejects_malformed_files_and_broken_references(
    tmp_path: Path,
) -> None:
    island_dir = tmp_path / "GW_001"
    shutil.copytree(GW001_DIR, island_dir)
    pos_path = island_dir / "positions.json"
    pos_before = pos_path.read_bytes()
    vendor_before = sorted(p.name for p in (island_dir / "vendor").iterdir())

    valid_pts = json.loads(VENDOR_20261118.read_text(encoding="utf-8-sig"))
    bad_cases: list[tuple[str, str]] = [
        ("not_json.json", "{not valid json"),
        ("not_list.json", json.dumps({"id": 1})),
        ("empty_list.json", "[]"),
        (
            "missing_robotarm.json",
            json.dumps([{k: v for k, v in valid_pts[0].items() if k != "robotarmName"}]),
        ),
        (
            "bad_waypoints.json",
            json.dumps([{**valid_pts[0], "positionData": "[{\"Seq\": 1, \"Type\": \"T\"}]"}]),
        ),
        (
            "bad_coord.json",
            json.dumps(
                [
                    {
                        **valid_pts[0],
                        "positionData": json.dumps(
                            [{"Seq": 1, "Type": "T", "Pos": ["a", "0", "0", "0", "0", "0"]}]
                        ),
                    }
                ]
            ),
        ),
        (
            "missing_pcr_point.json",
            json.dumps([p for p in valid_pts if str(p["id"]) != "2118260185060499457"]),
        ),
        (
            "pcr_posture_changed.json",
            json.dumps(
                [
                    ({**p, "posture": "0"} if str(p["id"]) == "2118260185060499457" else p)
                    for p in valid_pts
                ]
            ),
        ),
    ]

    for name, content in bad_cases:
        bad_file = tmp_path / name
        bad_file.write_text(content, encoding="utf-8")
        rc = main(
            [
                "points",
                "import",
                str(bad_file),
                "--yes",
                "--date",
                "20261120",
                "--island-dir",
                str(island_dir),
            ]
        )
        assert rc != 0, f"{name} should be rejected with non-zero return code"
        assert pos_path.read_bytes() == pos_before
        assert sorted(p.name for p in (island_dir / "vendor").iterdir()) == vendor_before
