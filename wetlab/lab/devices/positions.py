"""On-site island position loader, taught-point verifier, and vendor import (04 §5.16)."""

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import Any

from . import arm, pcr, rack, shaker, turntable
from .base import Position

REPO_ROOT = Path(__file__).resolve().parents[3]
_DEVICES_BY_NAME: dict[str, ModuleType] = {
    dev.spec.NAME: dev for dev in (pcr, shaker, arm, turntable, rack)
}
_REQUIRED_POINT_KEYS = {"id", "posture", "positionData", "deviceName", "robotarmName"}
_AXES_T = ("X", "Y", "Z", "Rx", "Ry", "Rz")
_AXES_J = ("J1", "J2", "J3", "J4", "J5", "J6")


def default_island_dir() -> Path:
    """Return `WETLAB_ISLAND_DIR` if set, else `island/<WETLAB_ISLAND or GW_001>`."""
    override = os.environ.get("WETLAB_ISLAND_DIR")
    if override:
        return Path(override)
    return REPO_ROOT / "island" / (os.environ.get("WETLAB_ISLAND") or "GW_001")


def positions_hash(island_dir: Path | None = None) -> str:
    """Compute the SHA-256 hex digest of the current `positions.json` file on disk."""
    target_dir = Path(island_dir) if island_dir is not None else default_island_dir()
    return hashlib.sha256((target_dir / "positions.json").read_bytes()).hexdigest()


def _fmt_num(val: float) -> str:
    txt = f"{val:.6f}".rstrip("0").rstrip(".")
    return "0" if txt in ("", "-0") else txt


def _non_empty_str(val: object) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _parse_waypoint(wp: object, source_name: str, pt_id: str) -> dict[str, Any]:
    if not isinstance(wp, dict) or not {"Seq", "Type", "Pos"}.issubset(wp):
        raise ValueError(f"{source_name} point {pt_id}: waypoint missing Seq/Type/Pos")
    seq = wp["Seq"]
    if isinstance(seq, bool) or not isinstance(seq, int):
        raise ValueError(f"{source_name} point {pt_id}: invalid waypoint Seq {seq!r}")
    if wp["Type"] not in ("J", "T"):
        raise ValueError(f"{source_name} point {pt_id}: invalid waypoint Type {wp['Type']!r}")
    pos = wp["Pos"]
    if not isinstance(pos, list) or len(pos) != 6:
        raise ValueError(f"{source_name} point {pt_id}: waypoint Pos must have 6 values")
    coords: list[float] = []
    for item in pos:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            raise ValueError(f"{source_name} point {pt_id}: invalid coordinate {item!r}")
        coords.append(float(item))
    return {"seq": seq, "type": wp["Type"], "pos": coords}


def parse_vendor_points(raw_text: str, *, source_name: str = "Position.json") -> dict[str, Any]:
    """Validate vendor taught-point JSON text and return structured entries keyed by point ID."""
    vendor_raw = json.loads(raw_text)
    if not isinstance(vendor_raw, list) or not vendor_raw:
        raise ValueError(f"vendor points file {source_name} must be a non-empty JSON list")

    points: dict[str, dict[str, Any]] = {}
    for entry in vendor_raw:
        if not isinstance(entry, dict) or not _REQUIRED_POINT_KEYS.issubset(entry):
            raise ValueError(f"invalid point entry in {source_name}: {entry!r}")
        raw_id = entry["id"]
        if isinstance(raw_id, bool) or not str(raw_id).strip():
            raise ValueError(f"invalid point id in {source_name}: {raw_id!r}")
        pt_id = str(raw_id).strip()
        if pt_id in points:
            raise ValueError(f"duplicate point id {pt_id} in {source_name}")
        if isinstance(entry["posture"], bool):
            raise ValueError(f"invalid posture in {source_name}: {entry['posture']!r}")
        posture = int(entry["posture"])
        if not (_non_empty_str(entry["deviceName"]) and _non_empty_str(entry["robotarmName"])):
            raise ValueError(f"point {pt_id} in {source_name}: missing deviceName/robotarmName")
        if not _non_empty_str(entry["positionData"]):
            raise ValueError(f"point {pt_id} in {source_name}: positionData must be JSON string")
        wp_list = json.loads(entry["positionData"])
        if not isinstance(wp_list, list) or not wp_list:
            raise ValueError(f"point {pt_id} in {source_name}: positionData must be non-empty")
        parsed_wps = sorted(
            (_parse_waypoint(wp, source_name, pt_id) for wp in wp_list),
            key=lambda w: w["seq"],
        )
        last_wp = parsed_wps[-1]
        points[pt_id] = {
            "id": pt_id,
            "posture": posture,
            "device_name": entry["deviceName"],
            "steps": len(parsed_wps),
            "final_type": last_wp["type"],
            "final_pos": last_wp["pos"],
        }
    return points


def _load_vendor_entries(target_dir: Path, source: object) -> dict[str, dict[str, Any]]:
    if not isinstance(source, dict):
        raise ValueError("positions.json missing valid top-level 'source' object")
    rel_file = source.get("file")
    expected_sha = source.get("sha256")
    if not _non_empty_str(rel_file) or not _non_empty_str(expected_sha):
        raise ValueError("positions.json 'source' requires non-empty 'file' and 'sha256'")
    vendor_path = target_dir / str(rel_file)
    if not vendor_path.is_file():
        raise FileNotFoundError(f"vendor points file not found: {vendor_path}")
    actual_sha = hashlib.sha256(vendor_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError(f"vendor points sha256 mismatch for {vendor_path.name}")
    return parse_vendor_points(
        vendor_path.read_text(encoding="utf-8-sig"), source_name=vendor_path.name
    )


def load_positions(island_dir: Path | None = None) -> dict[str, Position]:
    """Load `positions.json` and verify every taught point and posture against vendor file."""
    target_dir = Path(island_dir) if island_dir is not None else default_island_dir()
    positions_path = target_dir / "positions.json"
    if not positions_path.is_file():
        raise FileNotFoundError(f"positions.json not found: {positions_path}")
    raw = json.loads(positions_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("positions.json root must be a JSON object")

    vendor_entries = _load_vendor_entries(target_dir, raw.get("source"))
    loaded: dict[str, Position] = {}
    seen_points: set[str] = set()
    for pos_name, item in raw.items():
        if pos_name == "source":
            continue
        if not isinstance(item, dict):
            raise ValueError(f"position {pos_name!r} must be a JSON object")
        device = str(item["device"])
        if device not in _DEVICES_BY_NAME:
            raise ValueError(f"position {pos_name!r} references unknown device {device!r}")
        point = str(item["point"])
        if point not in vendor_entries:
            raise ValueError(f"position {pos_name!r} point {point!r} missing from vendor file")
        if point in seen_points:
            raise ValueError(f"duplicate taught point {point!r} in positions.json")
        raw_posture = item.get("posture")
        if isinstance(raw_posture, bool) or not isinstance(raw_posture, int):
            raise ValueError(f"position {pos_name!r} has invalid posture {raw_posture!r}")
        if vendor_entries[point]["posture"] != raw_posture:
            raise ValueError(f"position {pos_name!r} posture mismatch for point {point!r}")
        holds = item.get("holds_labware", True)
        if not isinstance(holds, bool):
            raise ValueError(f"position {pos_name!r} holds_labware must be a boolean")
        seen_points.add(point)
        loaded[pos_name] = Position(
            label=str(item["label"]),
            device=device,
            point=point,
            posture=raw_posture,
            index=str(item["index"]),
            holds_labware=holds,
        )
    return loaded


def compare_vendor_points(target_dir: Path, input_path: Path) -> dict[str, Any]:
    """Compare candidate vendor points against current `positions.json` and old vendor file."""
    new_points = parse_vendor_points(
        input_path.read_text(encoding="utf-8-sig"), source_name=input_path.name
    )
    current_positions = load_positions(target_dir)
    raw_cfg = json.loads((target_dir / "positions.json").read_text(encoding="utf-8"))
    old_points = _load_vendor_entries(target_dir, raw_cfg["source"])
    labels = {pos.point: f"{name} / {pos.label}" for name, pos in current_positions.items()}

    ref_errors: list[str] = []
    for pos_name, pos in current_positions.items():
        if pos.point not in new_points:
            ref_errors.append(f"位置 {pos_name} 引用的点位 {pos.point} 在新文件中不存在")
        elif new_points[pos.point]["posture"] != pos.posture:
            new_p = new_points[pos.point]["posture"]
            ref_errors.append(
                f"位置 {pos_name} 点位 {pos.point} 姿态变化：位置表为 {pos.posture}，新文件为 {new_p}"
            )

    added = [info for pt_id, info in new_points.items() if pt_id not in old_points]
    removed = [
        {"id": pt_id, "tag": labels.get(pt_id) or info["device_name"]}
        for pt_id, info in old_points.items()
        if pt_id not in new_points
    ]
    changed: list[dict[str, Any]] = []
    for pt_id, info in new_points.items():
        if pt_id not in old_points:
            continue
        old_info = old_points[pt_id]
        diffs: list[str] = []
        if old_info["posture"] != info["posture"]:
            diffs.append(f"姿态 {old_info['posture']} -> {info['posture']}")
        axes = _AXES_T if info["final_type"] == "T" else _AXES_J
        if old_info["final_type"] != info["final_type"]:
            diffs.append(f"坐标系 {old_info['final_type']} -> {info['final_type']}")
        else:
            for idx, axis in enumerate(axes):
                old_v = old_info["final_pos"][idx]
                new_v = info["final_pos"][idx]
                if abs(old_v - new_v) > 1e-6:
                    diffs.append(f"{axis} {_fmt_num(old_v)} -> {_fmt_num(new_v)}")
        if diffs:
            tag = labels.get(pt_id) or info["device_name"]
            changed.append({"id": pt_id, "tag": tag, "diffs": diffs})
    return {
        "old_file": raw_cfg["source"]["file"],
        "input_name": input_path.name,
        "added": added,
        "removed": removed,
        "changed": changed,
        "ref_errors": ref_errors,
    }


def run_points_import_cli(
    input_path: Path,
    *,
    island_dir: Path | None = None,
    date_str: str | None = None,
    assume_yes: bool = False,
) -> int:
    """Validate, diff, confirm, and archive a vendor taught-point JSON file locally."""
    target_dir = Path(island_dir) if island_dir is not None else default_island_dir()
    effective_date = date_str or time.strftime("%Y%m%d")
    if not effective_date.isdigit() or len(effective_date) != 8:
        print(f"日期格式错误（应为 YYYYMMDD）：{effective_date}", file=sys.stderr)
        return 1
    try:
        raw_bytes = input_path.read_bytes()
        report = compare_vendor_points(target_dir, input_path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"点位文件校验失败：{exc}", file=sys.stderr)
        return 1

    print(f"对比基准: {report['old_file']} -> {report['input_name']}")
    for item in report["added"]:
        msg = f"{item['device_name']}, 姿态 {item['posture']}, {item['steps']} 步"
        print(f"[新增] {item['id']} ({msg})")
    for item in report["removed"]:
        print(f"[删除] {item['id']} ({item['tag']})")
    for item in report["changed"]:
        print(f"[变更] {item['id']} ({item['tag']}): {', '.join(item['diffs'])}")
    if not (report["added"] or report["removed"] or report["changed"]):
        print("无点位差异。")
    if report["ref_errors"]:
        for err in report["ref_errors"]:
            print(f"[错误] {err}", file=sys.stderr)
        return 1
    if not assume_yes:
        print("确认导入并更新 positions.json？[y/N]: ", end="", flush=True)
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            print("已取消导入，未修改任何文件。")
            return 1

    rel_vendor = f"vendor/Position_{effective_date}.json"
    dest_vendor = target_dir / rel_vendor
    dest_vendor.parent.mkdir(parents=True, exist_ok=True)
    if dest_vendor.resolve() != input_path.resolve():
        dest_vendor.write_bytes(raw_bytes)
    pos_path = target_dir / "positions.json"
    cfg = json.loads(pos_path.read_text(encoding="utf-8"))
    cfg["source"] = {"file": rel_vendor, "sha256": hashlib.sha256(raw_bytes).hexdigest()}
    pos_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    load_positions(target_dir)
    print(f"已归档 {rel_vendor} 并更新 positions.json。")
    return 0


POSITIONS: dict[str, Position] = load_positions()
LABWARE_POSITIONS: dict[str, Position] = {
    pos_name: pos for pos_name, pos in POSITIONS.items() if pos.holds_labware
}
POSITION_DEVICE: dict[str, ModuleType] = {
    pos_name: _DEVICES_BY_NAME[pos.device] for pos_name, pos in POSITIONS.items()
}
POINT_TO_POSITION: dict[str, str] = {
    pos.point: pos_name for pos_name, pos in POSITIONS.items()
}
TAUGHT_POINTS: dict[str, Position] = {
    pos.point: pos for pos in POSITIONS.values()
}
