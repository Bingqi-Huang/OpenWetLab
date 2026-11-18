"""Skills and stage compiler for the PCR thermal cycler (docs/spec/04 section 5.8)."""

from collections.abc import Sequence
import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ...errors import Blocked, DeviceFailed, SkillFailed
from ..base import ACT, READ, skill
from . import rules

MAX_PCR_STEPS = 30
MAX_UNHEATED_LID_STAGE_C = 45.0


class PcrStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature_c: Annotated[float, Field(ge=4, le=99, strict=True)]
    hold_seconds: Annotated[int, Field(ge=1, le=3600, strict=True)]


class PcrRepeatStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repeat: Annotated[int, Field(ge=2, le=99, strict=True)]
    steps: Annotated[list[PcrStepSpec], Field(min_length=1)]


PcrStage = PcrStepSpec | PcrRepeatStage
_STAGES_ADAPTER = TypeAdapter(Annotated[list[PcrStage], Field(min_length=1)])


def _hw_step(temp_c: float, hold_s: int, next_step: int = 0, cycles: int = 1) -> dict[str, Any]:
    t = int(temp_c) if temp_c.is_integer() else temp_c
    return {
        "ZoneATarget": t,
        "ZoneCTarget": t,
        "HoldSeconds": hold_s,
        "NextStep": next_step,
        "Cycles": cycles,
    }


def compile_pcr_stages(stages: Sequence[PcrStage | dict[str, Any]]) -> list[dict[str, Any]]:
    """Compile human/agent stage list into 1-indexed 0x03C0 hardware step dicts."""
    parsed = _STAGES_ADAPTER.validate_python(stages)
    compiled: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, PcrStepSpec):
            compiled.append(_hw_step(item.temperature_c, item.hold_seconds))
        else:
            start_step = len(compiled) + 1
            for idx, sub in enumerate(item.steps):
                is_last = idx == len(item.steps) - 1
                next_step = start_step if (is_last and len(item.steps) > 1) else 0
                cycles = item.repeat if is_last else 1
                compiled.append(_hw_step(sub.temperature_c, sub.hold_seconds, next_step, cycles))
    if len(compiled) > MAX_PCR_STEPS:
        raise ValueError(f"expanded PCR program has {len(compiled)} steps (max {MAX_PCR_STEPS})")
    return compiled


def total_hold_seconds(steps: list[dict[str, Any]]) -> int:
    """Compute total hold seconds across all loop passes of compiled 0x03C0 steps."""
    total = 0
    i = 0
    loop_iter: dict[int, int] = {}
    enclosing: dict[int, int] = {}
    for end_idx, st in enumerate(steps):
        nxt = int(st.get("NextStep", 0))
        cycles = int(st.get("Cycles", 1))
        if nxt and cycles > 1:
            for idx in range(nxt - 1, end_idx + 1):
                enclosing[idx] = end_idx

    while i < len(steps):
        st = steps[i]
        end_idx = enclosing.get(i)
        k = loop_iter.get(end_idx, 0) if end_idx is not None else 0
        hold = int(st["HoldSeconds"]) + int(st.get("RampPerCycleSeconds", 0)) * k
        nxt = int(st.get("NextStep", 0))
        cycles = int(st.get("Cycles", 1))
        if nxt and cycles > 1:
            total += hold
            if k + 1 < cycles:
                loop_iter[i] = k + 1
                i = nxt - 1
                continue
            loop_iter.pop(i, None)
        elif cycles > 1:
            for rep in range(cycles):
                total += int(st["HoldSeconds"]) + int(st.get("RampPerCycleSeconds", 0)) * rep
        else:
            total += hold
        i += 1
    return total


def _fmt_temp(c: float) -> str:
    return f"{int(c)}℃" if float(c).is_integer() else f"{c:g}℃"


def _fmt_duration(sec: int) -> str:
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60} 分钟"
    if sec > 60:
        return f"{sec // 60} 分 {sec % 60} 秒"
    return f"{sec} 秒"


def format_pcr_stages(stages: Sequence[PcrStage | dict[str, Any]]) -> str:
    """Render stages as single-line Chinese summary (docs/spec/04 section 5.8)."""
    parsed = _STAGES_ADAPTER.validate_python(stages)
    parts: list[str] = []
    for item in parsed:
        if isinstance(item, PcrStepSpec):
            parts.append(f"{_fmt_temp(item.temperature_c)} {_fmt_duration(item.hold_seconds)}")
        else:
            inner = " → ".join(
                f"{_fmt_temp(s.temperature_c)} {_fmt_duration(s.hold_seconds)}" for s in item.steps
            )
            parts.append(f"（{inner}）× {item.repeat}")
    return " → ".join(parts).replace(" → （", " →（")


def _turn_lid_off(lab: Any) -> None:
    params = {"Zone": "Lid", "Enabled": False}
    lab.do("pcr", "SetHeater", params, expect={"HeaterLidEnabled": False}, within_s=10)


@skill(kind=READ, label="读取 PCR 状态")
def pcr_status(lab) -> dict[str, Any]:
    """Read the current status and progress of the PCR thermal cycler."""
    return lab.do("pcr", "GetPcrStatus")


@skill(kind=ACT, label="打开 PCR 仓门")
def pcr_open(lab) -> dict[str, Any]:
    """Lift the heated lid if not already lifted, then open the PCR door."""
    pcr_state = lab.state().get("pcr") or {}
    if pcr_state.get("LidMotorState") != "Lifted":
        expect_lifted = {"LidMotorState": "Lifted"}
        lab.do("pcr", "SetLidMotor", {"Action": "Lift"}, expect=expect_lifted, within_s=15)
    return lab.do("pcr", "OpenDoor", {}, expect={"DoorState": "Open"}, within_s=10)


@skill(kind=ACT, label="关闭 PCR 仓门")
def pcr_close(lab) -> dict[str, Any]:
    """Close the PCR door, waiting up to 15s for lid press if a plate is inside."""
    has_plate = lab.materials.at("pcr") is not None
    expect: dict[str, Any] = {"DoorState": "Closed"}
    if has_plate:
        expect["LidMotorState"] = "Pressed"
    wait_s = 15 if has_plate else 10
    return lab.do("pcr", "CloseDoor", {"WaitLid": False}, expect=expect, within_s=wait_s)


@skill(kind=READ, label="列出 PCR 程序")
def pcr_list_programs(lab) -> dict[str, Any]:
    """List program files stored inside the PCR thermal cycler."""
    return lab.do("pcr", "ListFiles")


def _validate_pcr_start_args(model: Any) -> Any:
    expanded = sum(len(s.steps) if isinstance(s, PcrRepeatStage) else 1 for s in model.stages)
    if expanded > MAX_PCR_STEPS:
        raise ValueError(f"expanded PCR program has {expanded} steps (max {MAX_PCR_STEPS})")
    if model.lid_c is None:
        temps = [
            sub.temperature_c
            for s in model.stages
            for sub in (s.steps if isinstance(s, PcrRepeatStage) else [s])
        ]
        if any(t > MAX_UNHEATED_LID_STAGE_C for t in temps):
            raise ValueError(
                f"lid_c cannot be null when any stage exceeds {MAX_UNHEATED_LID_STAGE_C} C"
            )
    return model


@skill(kind=ACT, label="启动 PCR 程序")
def pcr_start_program(
    lab,
    stages: Annotated[list[PcrStage], Field(min_length=1)],
    volume_ul: Annotated[int, Field(ge=1, le=255, strict=True)],
    lid_c: Annotated[float, Field(ge=30, le=110, strict=True)] | None = 105,
) -> dict[str, Any]:
    """Configure block mode, plate volume, and lid heater, then start a custom PCR program."""
    steps = compile_pcr_stages(stages)
    hold_sum = total_hold_seconds(steps)
    summary = format_pcr_stages(stages)

    lab.do("pcr", "SetTubeMode", {"Mode": "Block"})
    lab.do("pcr", "SetPlateCapacity", {"Microliters": volume_ul})
    lid_enabled = False
    if lid_c is not None:
        lid_params = {"Zone": "Lid", "Enabled": True, "TargetCelsius": lid_c}
        lab.do("pcr", "SetHeater", lid_params, expect={"HeaterLidEnabled": True}, within_s=10)
        lid_enabled = True
    elif (lab.state().get("pcr") or {}).get("HeaterLidEnabled"):
        _turn_lid_off(lab)

    try:
        run_params = {"Steps": json.dumps(steps)}
        res = lab.do("pcr", "RunProgram", run_params, expect={"PcrState": "Running"}, within_s=10)
    except (Blocked, DeviceFailed):
        if lid_enabled:
            _turn_lid_off(lab)
        raise

    est_s = int((lab.state().get("pcr") or {}).get("PcrTotalSeconds") or hold_sum)
    lab.materials.record_at(
        "pcr",
        "pcr_program_started",
        summary=summary,
        volume_ul=volume_ul,
        lid_c=lid_c,
        hold_seconds=hold_sum,
        estimated_seconds=est_s,
    )
    return {
        "started": True,
        "summary": summary,
        "steps": len(steps),
        "hold_seconds": hold_sum,
        "estimated_seconds": est_s,
        "device_result": res,
    }


pcr_start_program.__skill_validator__ = _validate_pcr_start_args  # type: ignore[attr-defined]


@skill(kind=ACT, label="确认 PCR 程序完成")
def pcr_confirm_finished(lab) -> dict[str, Any]:
    """Verify the PCR program is not running, turn off the lid heater, and confirm completion."""
    pcr_state = lab.state().get("pcr") or {}
    cur_state = pcr_state.get("PcrState")
    if cur_state in ("Running", "Paused"):
        raise SkillFailed("PROGRAM_STILL_RUNNING", f"PCR program is still {cur_state}")

    _turn_lid_off(lab)
    pcr_state = lab.state().get("pcr") or {}
    fault_err = rules.fault({"pcr": pcr_state})
    if fault_err:
        raise SkillFailed("PCR_FAULT", fault_err)
    after_state = pcr_state.get("PcrState")
    if after_state != "Idle":
        raise SkillFailed("PROGRAM_ENDED_EARLY", f"PCR is not Idle (state={after_state})")

    last_run = lab.last_operation("pcr", "RunProgram")
    if last_run is None:
        raise SkillFailed("PROGRAM_ENDED_EARLY", "no previous RunProgram record found")
    last_stop = lab.last_operation("pcr", "StopRun")
    if last_stop is not None and last_stop["seq"] > last_run["seq"]:
        raise SkillFailed("PROGRAM_ENDED_EARLY", "PCR program was stopped before completion")

    raw_steps = last_run["params"]["Steps"]
    steps = json.loads(raw_steps) if isinstance(raw_steps, str) else raw_steps
    hold_sum = total_hold_seconds(steps)
    elapsed = lab.backend.now() - float(last_run["started_at"])
    if elapsed < hold_sum:
        msg = f"PCR program ended after {elapsed:.1f}s, expected at least {hold_sum}s of holds"
        raise SkillFailed("PROGRAM_ENDED_EARLY", msg)

    lab.materials.record_at(
        "pcr", "pcr_program_completed", hold_seconds=hold_sum, elapsed_s=round(elapsed, 1)
    )
    return {"completed": True, "hold_seconds": hold_sum, "elapsed_s": round(elapsed, 1)}


@skill(kind=ACT, label="停止 PCR 程序")
def pcr_stop(lab) -> dict[str, Any]:
    """Stop any non-Idle PCR program and always turn off the heated lid."""
    pcr_state = lab.state().get("pcr") or {}
    try:
        if pcr_state.get("PcrState") != "Idle":
            lab.do("pcr", "StopRun", {}, expect={"PcrState": "Idle"}, within_s=15)
    finally:
        _turn_lid_off(lab)
    return {"stopped": True}
