"""Rotation skill for the LK-TECH steering turntable (docs/spec/04 section 5.8)."""

from typing import Any, Literal

from ..base import ACT, skill


@skill(kind=ACT, label="转向台转到 {angle}°")
def rotate_turntable(
    lab,
    angle: Literal[0, 90, 180, 270],
) -> dict[str, Any]:
    """Rotate the steering turntable to an absolute angle (0, 90, 180, or 270 degrees)."""
    lab.do(
        "turntable",
        "SingleLoop",
        {"Angle": angle},
        expect={"Angles": str(angle)},
        within_s=15,
    )
    return {"angle": angle}
