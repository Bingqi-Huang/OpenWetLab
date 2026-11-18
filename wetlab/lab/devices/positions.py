"""On-site island position loader and vendor taught-point verifier (04 section 5.16)."""

import hashlib
import json
import os
from pathlib import Path
from types import ModuleType

from . import arm, pcr, rack, shaker, turntable
from .base import Position

REPO_ROOT = Path(__file__).resolve().parents[3]
_DEVICES_BY_NAME: dict[str, ModuleType] = {
    dev.spec.NAME: dev for dev in (pcr, shaker, arm, turntable, rack)
}


def default_island_dir() -> Path:
    """Return `island/<WETLAB_ISLAND or GW_001>` under the repository root."""
    island_name = os.environ.get("WETLAB_ISLAND") or "GW_001"
    return REPO_ROOT / "island" / island_name


def positions_hash(island_dir: Path | None = None) -> str:
    """Compute the SHA-256 hex digest of the current `positions.json` file on disk."""
    target_dir = Path(island_dir) if island_dir is not None else default_island_dir()
    positions_path = target_dir / "positions.json"
    return hashlib.sha256(positions_path.read_bytes()).hexdigest()


def _load_vendor_points(target_dir: Path, source: object) -> dict[str, int]:
    if not isinstance(source, dict):
        raise ValueError("positions.json missing valid top-level 'source' object")
    rel_file = source.get("file")
    expected_sha = source.get("sha256")
    if not isinstance(rel_file, str) or not rel_file:
        raise ValueError("positions.json 'source.file' must be a non-empty string")
    if not isinstance(expected_sha, str) or not expected_sha:
        raise ValueError("positions.json 'source.sha256' must be a non-empty string")

    vendor_path = target_dir / rel_file
    if not vendor_path.is_file():
        raise FileNotFoundError(f"vendor points file not found: {vendor_path}")
    actual_sha = hashlib.sha256(vendor_path.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise ValueError(
            f"vendor points sha256 mismatch for {vendor_path.name}: "
            f"expected {expected_sha}, got {actual_sha}"
        )

    vendor_raw = json.loads(vendor_path.read_text(encoding="utf-8-sig"))
    if not isinstance(vendor_raw, list):
        raise ValueError(f"vendor points file {vendor_path.name} must contain a JSON list")

    points: dict[str, int] = {}
    for entry in vendor_raw:
        if not isinstance(entry, dict) or "id" not in entry or "posture" not in entry:
            raise ValueError(f"invalid point entry in {vendor_path.name}: {entry!r}")
        points[str(entry["id"])] = int(entry["posture"])
    return points


def load_positions(island_dir: Path | None = None) -> dict[str, Position]:
    """Load `positions.json` and verify every taught point and posture against the vendor file."""
    target_dir = Path(island_dir) if island_dir is not None else default_island_dir()
    positions_path = target_dir / "positions.json"
    if not positions_path.is_file():
        raise FileNotFoundError(f"positions.json not found: {positions_path}")

    raw = json.loads(positions_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("positions.json root must be a JSON object")

    vendor_points = _load_vendor_points(target_dir, raw.get("source"))
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
        if point not in vendor_points:
            raise ValueError(f"position {pos_name!r} point {point!r} missing from vendor file")
        if point in seen_points:
            raise ValueError(f"duplicate taught point {point!r} in positions.json")
        raw_posture = item.get("posture")
        if isinstance(raw_posture, bool) or not isinstance(raw_posture, int):
            raise ValueError(f"position {pos_name!r} has invalid posture {raw_posture!r}")
        if vendor_points[point] != raw_posture:
            raise ValueError(
                f"position {pos_name!r} posture {raw_posture} != vendor posture "
                f"{vendor_points[point]} for point {point!r}"
            )
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
