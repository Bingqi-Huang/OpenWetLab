"""Scenario format for initializing deterministic virtual island runs."""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..devices import BY_NAME, CODE_TO_NAME, LABWARE_POSITIONS
from .faults import Fault

DEFAULT_EPOCH = 1_800_000_000.0


class Scenario(BaseModel):
    """Initial clock, labware placement, device property overrides, and faults."""

    start_time: float = DEFAULT_EPOCH
    labware: dict[str, str] = Field(default_factory=dict)
    devices: dict[str, dict] = Field(default_factory=dict)
    faults: list[Fault] = Field(default_factory=list)
    gateway_devices: list[dict[str, Any]] | None = None

    @field_validator("labware")
    @classmethod
    def _validate_labware(cls, v: dict[str, str]) -> dict[str, str]:
        occupied: set[str] = set()
        for lw_id, pos in v.items():
            if pos != "arm" and pos not in LABWARE_POSITIONS:
                raise ValueError(f"unknown position {pos!r} for labware {lw_id!r}")
            if pos in occupied:
                raise ValueError(f"multiple labware items placed at {pos!r}")
            occupied.add(pos)
        return dict(v)

    @field_validator("devices")
    @classmethod
    def _normalize_devices(cls, v: dict[str, dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for dev, props in v.items():
            short = CODE_TO_NAME.get(dev, dev)
            if short not in BY_NAME:
                raise ValueError(f"unknown device: {dev!r}")
            out[short] = {**out.get(short, {}), **props}
        return out

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "Scenario":
        """Construct a Scenario from a `snapshot.read_snapshot(conn)` dictionary."""
        return from_snapshot(snapshot)


def from_snapshot(snapshot: dict[str, Any]) -> Scenario:
    """Construct a Scenario from a `snapshot.read_snapshot(conn)` dictionary."""
    last_ts = float(snapshot.get("last_timestamp") or 0.0)
    start_time = last_ts if last_ts > 0.0 else DEFAULT_EPOCH
    raw_labware = snapshot.get("labware") or {}
    labware: dict[str, str] = {}
    for lw_id, entry in raw_labware.items():
        pos = entry["position"] if isinstance(entry, dict) else entry
        if pos is not None:
            labware[lw_id] = str(pos)

    devices: dict[str, dict[str, Any]] = {}
    raw_devices = snapshot.get("devices") or {}
    for dev_name, entry in raw_devices.items():
        props = {
            k: v
            for k, v in (entry.get("props") or {}).items()
            if not k.startswith("_")
        }
        if not props:
            continue
        if dev_name == "pcr" and props.get("PcrState") == "Running":
            updated_at = entry.get("updated_at")
            elapsed = (
                max(0.0, start_time - float(updated_at))
                if updated_at is not None
                else 0.0
            )
            rem = int(round(float(props.get("PcrRemainingSeconds") or 0) - elapsed))
            if rem > 0:
                props["PcrRemainingSeconds"] = rem
            else:
                props["PcrState"] = "Idle"
                props["PcrRemainingSeconds"] = 0
        devices[dev_name] = props

    return Scenario(start_time=start_time, labware=labware, devices=devices)
