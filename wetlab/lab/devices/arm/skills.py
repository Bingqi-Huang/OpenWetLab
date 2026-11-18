"""Transfer skill for the Huayan 6-axis robot arm (docs/spec/04 section 5.8 & 5.16)."""

from typing import Annotated, Any

from pydantic import Field

from ...errors import Blocked
from ..base import ACT, Position, skill


def _material_index(src: Position, dst: Position) -> str:
    if src.device == "rack":
        return src.index
    if dst.device == "rack":
        return dst.index
    return "1-1"


@skill(kind=ACT, label="将 {labware_id} 搬运到 {to_position}")
def transfer(
    lab,
    labware_id: Annotated[str, Field(min_length=1, strict=True)],
    to_position: Annotated[str, Field(min_length=1, strict=True)],
) -> dict[str, Any]:
    """Transfer `labware_id` from its current island position to an empty `to_position`."""
    from_pos = lab.materials.where(labware_id)
    if from_pos is None:
        raise Blocked(f"labware {labware_id} is not on the island")
    if to_position not in lab.positions:
        raise Blocked(f"unknown position: {to_position}")
    src = lab.positions[from_pos]
    dst = lab.positions[to_position]
    if not dst.holds_labware:
        raise Blocked(f"position {to_position} does not hold labware")
    if to_position == from_pos:
        raise Blocked(f"labware {labware_id} is already at {to_position}")
    occupant = lab.materials.at(to_position)
    if occupant is not None:
        raise Blocked(f"position {to_position} is already occupied by {occupant}")

    payload = {
        "Tasks": [
            {"PauseTime": 0, "PositionId": int(src.point), "ClawSwitch": 0},
            {"PauseTime": 0, "PositionId": int(dst.point), "ClawSwitch": 1},
        ],
        "Material": {
            "Index": _material_index(src, dst),
            "Angle": 0.0,
            "Speed": "低速",
            "GrabHeight": 0.0,
            "Thickness": 0.0,
        },
    }
    lab.do(
        "arm",
        "MovMulti",
        payload,
        expect={"RobotMode": "使能空闲", "SafetyStatus": "NORMAL"},
        within_s=15,
        timeout_s=90,
    )
    lab.materials.move(labware_id, to_position, operation_id=lab.last_operation_id)
    return {"labware_id": labware_id, "from": from_pos, "to": to_position}
