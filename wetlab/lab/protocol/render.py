"""Human-readable Chinese Markdown rendering of protocols and rehearsal results (04 section 5.9)."""

from typing import Any

from pydantic import TypeAdapter

from ..devices import DEVICE_LABELS, POSITIONS, PROPS
from .model import Protocol, ProtocolStep, SkillStep, WaitStep, WaitUntilStep, coerce_protocol

_STEP = TypeAdapter(ProtocolStep)


def _pos_label(position: str) -> str:
    pos = POSITIONS.get(position)
    return f"{pos.label}（{position}）" if pos is not None else position


def _format_hold(seconds: int | float) -> str:
    whole = int(seconds)
    if seconds == whole and whole >= 60 and whole % 60 == 0:
        return f"{whole // 60} 分钟"
    if seconds == whole:
        return f"{whole} 秒"
    return f"{seconds:.1f} 秒"


def format_duration(seconds: float) -> str:
    """Format elapsed or maximum duration in concise Chinese."""
    rounded = round(seconds, 1)
    whole = int(round(seconds))
    if abs(seconds - whole) < 1e-6:
        if whole >= 60 and whole % 60 == 0:
            return f"{whole // 60} 分钟"
        if whole > 60:
            return f"{whole // 60} 分 {whole % 60} 秒"
        return f"{whole} 秒"
    if rounded >= 60:
        mins = int(rounded // 60)
        rem = round(rounded - mins * 60, 1)
        return f"{mins} 分 {rem:g} 秒"
    return f"{rounded:g} 秒"


def _format_single_stage(stage: dict[str, Any]) -> str:
    temp = stage["temperature_c"]
    temp_str = f"{int(temp)}" if isinstance(temp, (int, float)) and temp == int(temp) else f"{temp}"
    return f"{temp_str}℃ {_format_hold(stage['hold_seconds'])}"


def format_pcr_stages(stages: list[dict[str, Any]]) -> str:
    """Render PCR stages on one line per 04 section 5.8."""
    parts: list[str] = []
    for stage in stages:
        if "repeat" in stage:
            inner = " → ".join(_format_single_stage(st) for st in stage["steps"])
            parts.append(f"（{inner}）× {stage['repeat']}")
        else:
            parts.append(_format_single_stage(stage))
    return " → ".join(parts).replace(" → （", " →（")


def _format_skill_step(step: SkillStep) -> str:
    name, args = step.skill, step.args
    if name == "pcr_start_program":
        stages_text = format_pcr_stages(args.get("stages", []))
        vol = args.get("volume_ul", "?")
        has_lid_key = "lid_c" in args
        lid_val = args.get("lid_c", 105)
        lid_text = "热盖不加热" if (has_lid_key and lid_val is None) else f"热盖 {lid_val:g}℃"
        return f"启动 PCR 程序：{stages_text}（体系 {vol} µL，{lid_text}）"
    if name == "pcr_open":
        return "打开 PCR 仪仓门"
    if name == "pcr_close":
        return "关闭 PCR 仪仓门"
    if name == "pcr_confirm_finished":
        return "确认 PCR 程序正常完成并关闭热盖加热"
    if name == "pcr_stop":
        return "停止 PCR 程序并关闭热盖加热"
    if name == "pcr_status":
        return "查询 PCR 仪状态"
    if name == "pcr_list_programs":
        return "列出 PCR 仪已存程序"
    if name == "shake":
        return f"振荡 {args.get('seconds')} 秒，{args.get('rpm')} rpm"
    if name == "shake_start":
        return f"开始持续振荡（{args.get('rpm')} rpm）"
    if name == "shake_stop":
        return "停止振荡并松开夹爪"
    if name == "transfer":
        return f"机械臂将 {args.get('labware_id')} 搬运至 {_pos_label(str(args.get('to_position', '')))}"
    if name == "rotate_turntable":
        return f"转向台旋转至 {args.get('angle')}°"
    arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"调用技能 {name}({arg_str})"


def _format_condition(device: str, prop: str, value: Any) -> str:
    shown = PROPS.get(device, {}).get(prop)
    if shown is None:
        return f"{prop}={value}"
    return f"{shown.label}变为{shown.values.get(value, value)}{shown.unit}"


def describe_step(step: ProtocolStep | dict[str, Any]) -> str:
    """One-line Chinese description of a protocol step, shared by the review text, web, and CLI."""
    parsed = step if isinstance(step, (SkillStep, WaitStep, WaitUntilStep)) else _STEP.validate_python(step)
    if isinstance(parsed, SkillStep):
        return _format_skill_step(parsed)
    if isinstance(parsed, WaitStep):
        return f"等待 {_format_hold(parsed.wait.seconds)}"
    spec = parsed.wait_until
    dev_label = DEVICE_LABELS.get(spec.device, spec.device)
    cond = "、".join(_format_condition(spec.device, k, v) for k, v in spec.state.items())
    return f"等待 {dev_label} {cond}（最长 {_format_hold(spec.max_seconds)}）"


def labware_paths(protocol: Protocol | dict[str, Any]) -> dict[str, list[str]]:
    """Each plate's positions in order: where it starts, then every `transfer` destination."""
    proto = coerce_protocol(protocol)
    paths: dict[str, list[str]] = {lw_id: [pos] for lw_id, pos in sorted(proto.labware.items())}
    for step in proto.steps:
        if isinstance(step, SkillStep) and step.skill == "transfer":
            paths.setdefault(str(step.args.get("labware_id", "")), []).append(str(step.args.get("to_position", "")))
    return paths


def _labware_trajectories(protocol: Protocol) -> list[str]:
    if not protocol.labware:
        return ["- 无登记器皿"]
    paths = {lw_id: [_pos_label(pos) for pos in stops] for lw_id, stops in labware_paths(protocol).items()}
    lines: list[str] = []
    for lw_id, stops in paths.items():
        if len(stops) == 1:
            lines.append(f"- {lw_id}：始终位于 {stops[0]}")
        else:
            lines.append(f"- {lw_id}：{' → '.join(stops)}")
    return lines


def render(
    protocol: Protocol | dict[str, Any],
    rehearsal: dict[str, Any] | None = None,
) -> str:
    """Render `protocol` and optional `rehearsal` outcome into Chinese Markdown."""
    proto = coerce_protocol(protocol)
    lines: list[str] = [f"# {proto.name}", "", "## 执行前现场应该是这样"]
    if proto.labware:
        for lw_id, pos in sorted(proto.labware.items()):
            lines.append(f"- [ ] 器皿 {lw_id} 位于 {_pos_label(pos)}")
    else:
        lines.append("- [ ] 台面无预置器皿")

    step_results: dict[int, dict[str, Any]] = {}
    if rehearsal is not None:
        for sr in rehearsal.get("steps") or []:
            step_results[int(sr["n"])] = sr

    lines.extend(["", "## 执行步骤"])
    for idx, step in enumerate(proto.steps, start=1):
        desc = describe_step(step)
        sr = step_results.get(idx)
        if sr is not None and "elapsed_s" in sr:
            desc = f"{desc}（预计 {format_duration(float(sr['elapsed_s']))}）"
        lines.append(f"{idx}. {desc}")

    lines.extend(["", "## 器皿去向", *_labware_trajectories(proto), "", "## 预演结论"])
    if rehearsal is None:
        lines.append("- 尚未预演")
    elif rehearsal.get("ok"):
        total_s = float(rehearsal.get("total_s", 0.0))
        lines.append(f"- 结论：预演通过（无事故），预计总耗时 {format_duration(total_s)}")
    else:
        fail = rehearsal.get("failure") or {}
        step_no = fail.get("step_n")
        reason = fail.get("reason", "未知原因")
        if step_no is not None:
            lines.append(f"- 结论：预演未通过（第 {step_no} 步失败：{reason}）")
        else:
            lines.append(f"- 结论：预演未通过（{reason}）")

    return "\n".join(lines) + "\n"
