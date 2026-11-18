"""Plain-Chinese wording for statuses, times, device readings, and plate history."""

from datetime import datetime
from typing import Any

from wetlab.lab.devices import DEVICE_LABELS, POSITIONS, PROPS
from wetlab.lab.protocol import format_duration

# 状态码 → (中文, 颜色)。颜色名对应 style.css 里 .wl-pill 的配色。
MISSION_STATUS = {
    "running": ("进行中", "primary"),
    "completed": ("已完成", "green"),
    "failed": ("未完成", "red"),
    "stopped": ("已终止", "gray"),
}
EXPERIMENT_STATUS = {
    "running": ("进行中", "primary"),
    "completed": ("已完成", "green"),
    "failed": ("失败", "red"),
    "stopped": ("已终止", "gray"),
}
PROTOCOL_STATUS = {
    "submitted": ("待审阅", "amber"),
    "approved": ("已批准", "blue"),
    "running": ("执行中", "primary"),
    "paused": ("已暂停", "amber"),
    "done": ("已完成", "green"),
    "failed": ("执行失败", "red"),
    "rejected": ("已退回", "gray"),
    "stopped": ("已停止", "red"),
    "aborted": ("已终止", "gray"),
}
STEP_STATUS = {
    "pending": ("待执行", "gray"),
    "sent": ("执行中", "primary"),
    "waiting": ("等待中", "primary"),
    "done": ("完成", "green"),
    "failed": ("失败", "red"),
    "unknown": ("待现场确认", "red"),
}
MODE_LABELS = {"virtual": "虚拟演练", "real": "真实实验岛"}
MODE_BANNERS = {
    "virtual": "当前连接：虚拟实验岛（virtual）",
    "real": "当前连接：真实实验岛（real）",
}
MODE_HINTS = {
    "virtual": "仿真演练环境 · 不会向现场真实设备发送任何指令",
    "real": "现场真机环境 · 已批准方案将驱动真实设备动作",
}
COMMAND_LABELS = {
    "new_mission": "新建任务",
    "place_labware": "放上孔板",
    "set_labware_contents": "修改孔板内容",
    "remove_labware": "取走孔板",
    "correct_labware": "更正孔板位置",
    "submit_protocol": "提交方案",
    "approve": "批准方案",
    "reject": "退回方案",
    "pause": "暂停执行",
    "resume": "继续执行",
    "stop_all": "全部停止",
    "unfreeze": "解除冻结",
    "abort": "终止实验",
    "message": "发送留言",
}


def status(table: dict[str, tuple[str, str]], code: str) -> tuple[str, str]:
    """Look up (Chinese text, tone) for a status code; unknown codes are gray."""
    return table.get(code, (code, "gray"))


def position_label(position: str | None) -> str:
    """`pcr` → `PCR仪`; `None` means the plate has been taken off the island."""
    if position is None:
        return "已取走"
    pos = POSITIONS.get(position)
    return pos.label if pos is not None else position


def device_label(device: str) -> str:
    return DEVICE_LABELS.get(device, device)


def stop_failed_text(failed_devices: list[str]) -> str:
    """Prominent warning banner text when one or more devices failed to stop."""
    formatted: list[str] = []
    for dev in failed_devices:
        label = DEVICE_LABELS.get(dev)
        formatted.append(f"{label}（{dev}）" if label else str(dev))
    names = "、".join(formatted) if formatted else "未知设备"
    return f"设备 {names} 没有停下来！请立即到现场处理（运动中请直接按物理急停按钮）。"


def clock_text(ts: float | None) -> str:
    """`11-18 10:04`; the year is left out because missions run for days, not years."""
    if ts is None or ts <= 0:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def full_time_text(ts: float | None) -> str:
    if ts is None or ts <= 0:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def ago_text(seconds: float | None) -> str:
    """How long ago, coarse enough to read at a glance."""
    if seconds is None:
        return "从未"
    if seconds < 2:
        return "刚刚"
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


def duration_text(seconds: float) -> str:
    """Rounded duration for countdowns and estimates (`12 分 5 秒`)."""
    return format_duration(float(round(max(0.0, seconds))))


def prop_text(device: str, prop: str, value: Any) -> tuple[str, str]:
    """(Chinese property name, value with meaning or unit) for a key device reading."""
    shown = PROPS[device][prop]
    if value in shown.values:
        return shown.label, shown.values[value]
    if shown.unit == "s":
        return shown.label, duration_text(float(value))
    if isinstance(value, float):
        value = f"{value:.1f}"
    return shown.label, f"{value} {shown.unit}".strip()


def _hold(seconds: Any) -> str:
    return duration_text(float(seconds))


def content_item_text(item: dict[str, Any]) -> str:
    """Format one `labware_contents` row (`样品名（孔位 · 体积 · 备注）`)."""
    parts: list[str] = []
    wells = item.get("wells")
    if wells:
        parts.append(", ".join(str(w) for w in wells))
    vol = item.get("volume_ul")
    if vol is not None:
        parts.append(f"{float(vol):g} µL")
    note = item.get("note")
    if note:
        parts.append(str(note))
    extra = f"（{' · '.join(parts)}）" if parts else ""
    return f"{item['name']}{extra}"


def contents_text(contents: list[dict[str, Any]]) -> str:
    """Join all items in a plate's `contents` list into a readable Chinese summary."""
    return "；".join(content_item_text(it) for it in contents)


def labware_event_text(what: str, detail: dict[str, Any]) -> str:
    """One sentence for a plate's history entry (`labware_events.what` + `detail`)."""
    d = detail
    if what == "placed":
        pos_str = position_label(d.get("position"))
        items = d.get("contents") or []
        summary = contents_text(items)
        return f"人工放到 {pos_str}（{summary}）" if summary else f"人工放到 {pos_str}"
    if what == "contents_updated":
        items = d.get("contents") or []
        summary = contents_text(items)
        return f"更新孔板内容：{summary}" if summary else "清空孔板内容"
    if what == "removed":
        return f"人工从 {position_label(d.get('from'))} 取走"
    if what == "corrected":
        return f"人工更正位置：{position_label(d.get('from'))} → {position_label(d.get('to'))}"
    if what == "moved":
        return f"机械臂搬运：{position_label(d.get('from'))} → {position_label(d.get('to'))}"
    if what == "pcr_program_started":
        lid = "热盖不加热" if d.get("lid_c") is None else f"热盖 {float(d['lid_c']):g}℃"
        vol = d.get("volume_ul")
        est = _hold(d.get("estimated_seconds", 0))
        return f"PCR 程序开始：{d.get('summary', '')}（{vol} µL，{lid}，预计 {est}）"
    if what == "pcr_program_completed":
        return f"PCR 程序完成（实际用时 {_hold(d.get('elapsed_s', 0))}）"
    if what == "shaken":
        return f"振荡 {d.get('seconds')} 秒，{d.get('rpm')} rpm"
    if what == "shake_started":
        return f"开始振荡，{d.get('rpm')} rpm"
    if what == "shake_stopped":
        early = "，提前停止" if d.get("ended_early") else ""
        return f"振荡结束，实际 {_hold(d.get('seconds', 0))}{early}"
    extra = "，".join(f"{k}={v}" for k, v in d.items())
    return f"{what}（{extra}）" if extra else what
