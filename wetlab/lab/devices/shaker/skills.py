"""Skills for the NAYO MCHS thermo-shaker module (docs/spec/04 section 5.8)."""

from typing import Annotated, Any

from pydantic import Field

from ...errors import SkillFailed
from ..base import ACT, skill


@skill(kind=ACT, label="振荡 {seconds} 秒，{rpm} rpm")
def shake(
    lab,
    seconds: Annotated[int, Field(ge=1, le=60, strict=True)],
    rpm: Annotated[int, Field(ge=200, le=3000, strict=True)],
) -> dict[str, Any]:
    """Clamp the plate, shake for `seconds` at `rpm`, then release the clamp."""
    lab.do("shaker", "SetClamp", {"clamp": True}, expect={"ElmState": 1}, within_s=5)
    lab.do("shaker", "SetTargetSpeed", {"rpm": rpm})
    lab.do(
        "shaker",
        "StartShakeTimed",
        {"seconds": seconds},
        expect={"ActualSpeed": 0, "ShakeState": 1, "ElmState": 2},
        timeout_s=seconds + 30,
    )
    lab.materials.record_at("shaker", "shaken", seconds=seconds, rpm=rpm)
    return {"seconds": seconds, "rpm": rpm}


@skill(kind=ACT, label="开始连续振荡，{rpm} rpm")
def shake_start(
    lab,
    rpm: Annotated[int, Field(ge=200, le=3000, strict=True)],
) -> dict[str, Any]:
    """Clamp the plate and start continuous shaking at `rpm`."""
    lab.do("shaker", "SetClamp", {"clamp": True}, expect={"ElmState": 1}, within_s=5)
    lab.do("shaker", "SetTargetSpeed", {"rpm": rpm})
    lab.do("shaker", "StartShake", {}, expect={"ShakeState": 0}, within_s=15)
    lab.materials.record_at("shaker", "shake_started", rpm=rpm)
    return {"started": True, "rpm": rpm}


@skill(kind=ACT, label="停止振荡")
def shake_stop(lab) -> dict[str, Any]:
    """Stop continuous shaking, release the clamp, and record actual shaking duration."""
    shaker_before = lab.state().get("shaker") or {}
    already_stopped = (
        shaker_before.get("ShakeState") != 0
        and (shaker_before.get("ActualSpeed") or 0) == 0
    )
    check_time = lab.backend.now()
    prev_stop = lab.last_operation("shaker", "StopShake")

    lab.do(
        "shaker",
        "StopShake",
        {},
        expect={"ActualSpeed": 0, "ShakeState": 1, "ElmState": 2},
        within_s=15,
    )

    last_start = lab.last_operation("shaker", "StartShake")
    if last_start is None:
        actual_s = 0.0
    elif already_stopped and prev_stop is not None and prev_stop["seq"] > last_start["seq"]:
        actual_s = max(0.0, round(float(prev_stop["started_at"]) - float(last_start["started_at"]), 1))
    else:
        actual_s = max(0.0, round(check_time - float(last_start["started_at"]), 1))

    lab.materials.record_at(
        "shaker",
        "shake_stopped",
        seconds=actual_s,
        ended_early=already_stopped,
    )
    if already_stopped:
        raise SkillFailed(
            "SHAKE_ENDED_EARLY",
            f"shaker was already stopped before shake_stop (actual {actual_s}s)",
        )
    return {"stopped": True, "seconds": actual_s}
