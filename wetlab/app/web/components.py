"""Domain pieces drawn as HTML: mode banner, header bar, freeze notice, bench, devices, steps."""

from typing import Any

from . import labels
from .ui import esc, pill


def mode_banner(mode: str) -> str:
    """Prominent top-of-page indicator showing whether connected to virtual or real island."""
    cls = "real" if mode == "real" else "virtual"
    title = labels.MODE_BANNERS.get(mode, f"当前连接：{mode}")
    hint = labels.MODE_HINTS.get(mode, "")
    return (
        f'<div class="wl"><div class="wl-mode {cls}">'
        f"<b>{esc(title)}</b><span>{esc(hint)}</span></div></div>"
    )


def status_bar(island: dict[str, Any], pending: list[dict[str, str]]) -> str:
    pills = [pill(island["link"]["text"], island["link"]["tone"])]
    frz_pill = pill("已冻结", "red", live=True) if island["frozen"] else pill("运行正常", "green")
    pills.append(frz_pill)
    if island["active"]:
        a = island["active"]
        text, tone = labels.status(labels.PROTOCOL_STATUS, a["protocol"]["status"])
        summary = f"{text} {a['protocol']['id']} · {a['done']}/{a['total']} 步"
        pills.append(pill(summary, tone, live=a["protocol"]["status"] == "running"))
    last_cmd = island.get("last_human_command")
    if last_cmd is not None and last_cmd["ok"]:
        pills.append(pill(f"最近处理：{last_cmd['label']} 已完成", "gray"))
    waits = "".join(f"<div>正在交给实验岛服务处理：{esc(p['label'])}</div>" for p in pending)
    pending_box = f"<div class=wl-pending>{waits}</div>" if waits else ""
    return (
        '<div class="wl"><div class="wl-bar"><div class="brand">WetLab<small>自主实验平台</small></div>'
        f'<div class="pills">{"".join(pills)}</div></div>{pending_box}</div>'
    )


def freeze_notice(ctx: dict[str, Any], *, detailed: bool) -> str:
    fr = ctx["freeze"]
    rows = [("原因", fr["reason"] or "未记录"), ("时间", labels.full_time_text(fr["at"]))]
    if detailed and ctx["step"] is not None:
        step = ctx["step"]
        rows.append(("中断的步骤", f"方案 {step['protocol_id']} 第 {step['n']} 步：{ctx['step_text']}"))
    if detailed and ctx["operation"] is not None:
        op = ctx["operation"]
        dev_name = labels.device_label(op["device"])
        rows.append(("设备指令", f"{dev_name} {op['action']}（{op['id']}）"))
    body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    guide = (
        "请到现场检查后，在下方给出结论。"
        if detailed
        else "请到现场检查后，在“实验岛”页给出结论。"
    )
    return (
        '<div class="wl wl-alert"><div class="t">实验岛已冻结，设备不会再动作</div>'
        f'<div class="b">{guide}</div><dl>{body}</dl></div>'
    )


def todo(item: dict[str, Any]) -> str:
    head = f'{pill(item["tag"], "amber")}{esc(item["title"])}'
    sub = f'<div class="s">{esc(item["sub"])}</div>'
    return f'<div class="wl wl-todo"><div class="t">{head}</div>{sub}</div>'


def bench(tiles: list[dict[str, Any]]) -> str:
    out = []
    for tile in tiles:
        slots = []
        for s in tile["slots"]:
            name = f'<div class="n">{esc(s["label"])}</div>' if len(tile["slots"]) > 1 else ""
            if s["labware"]:
                lw = s["labware"]
                slot_html = (
                    f'<div class="wl-slot full">{name}'
                    f'<div class="id">{esc(lw["id"])}</div>'
                    f'<div class="ty">{esc(lw["type"])}</div></div>'
                )
                slots.append(slot_html)
            else:
                slots.append(f'<div class="wl-slot empty">{name}空位</div>')
        tile_html = (
            f'<div class="wl-tile"><div class="h"><b>{esc(tile["label"])}</b></div>'
            f'{"".join(slots)}</div>'
        )
        out.append(tile_html)
    return f'<div class="wl"><div class="wl-tiles">{"".join(out)}</div></div>'


def device_cards(devices: list[dict[str, Any]]) -> str:
    cards = []
    for d in devices:
        rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in d["rows"])
        err = f'<div class="err">设备报错：{esc(d["error"])}</div>' if d["error"] else ""
        head = f'<div class="h"><b>{esc(d["label"])}</b>{pill(d["health"], d["tone"])}</div>'
        foot = f'<div class="f">状态更新于 {esc(d["updated"])}</div>'
        cards.append(f'<div class="wl-dev">{head}{err}<dl>{rows}</dl>{foot}</div>')
    return f'<div class="wl"><div class="wl-devs">{"".join(cards)}</div></div>'


def mission_head(mission: dict[str, Any]) -> str:
    text, tone = labels.status(labels.MISSION_STATUS, mission["status"])
    mode = labels.MODE_LABELS.get(mission["mode"], mission["mode"])
    mode_tone = "blue" if mission["mode"] == "real" else "gray"
    created = labels.clock_text(mission["created_at"])
    return (
        f'<div class="wl wl-mhead"><div class="t">{esc(mission["title"])}</div><div class="m">'
        f"{pill(text, tone)}{pill(mode, mode_tone)}"
        f"<span>{esc(mission['id'])} · 创建于 {esc(created)}</span></div></div>"
    )


def stepper(stages: list[dict[str, str]]) -> str:
    cells = "".join(
        f'<div class="{s["state"]}"><b>{esc(s["name"])}</b>{esc(s["note"])}</div>'
        for s in stages
    )
    return f'<div class="wl"><div class="wl-stepper">{cells}</div></div>'


def _stage_chip(stage: dict[str, Any]) -> str:
    temp = stage["temperature_c"]
    temp_text = f"{int(temp)}" if float(temp).is_integer() else f"{temp}"
    hold = labels.duration_text(stage["hold_seconds"])
    return f'<div class="st"><b>{esc(temp_text)}℃</b><span>{esc(hold)}</span></div>'


def pcr_program(args: dict[str, Any]) -> str:
    """Temperature stages as chips; a cycle block is boxed with its repeat count."""
    parts = []
    for stage in args.get("stages", []):
        if "repeat" in stage:
            inner = '<span class="ar">→</span>'.join(_stage_chip(s) for s in stage["steps"])
            parts.append(f'<div class="cy">{inner}<em>× {esc(stage["repeat"])}</em></div>')
        else:
            parts.append(_stage_chip(stage))
    no_lid = "lid_c" in args and args["lid_c"] is None
    lid = "热盖不加热" if no_lid else f"热盖 {args.get('lid_c', 105)}℃"
    meta = f'<div class="meta">体系 {esc(args.get("volume_ul", "?"))} µL · {esc(lid)}</div>'
    return f'<div class="wl-pcr">{"<span class=ar>→</span>".join(parts)}{meta}</div>'


def steps(rows: list[dict[str, Any]], *, current: int | None, show_status: bool) -> str:
    items = []
    for r in rows:
        is_bad = r["status"] in ("failed", "unknown")
        cls = "done" if r["status"] == "done" else ("bad" if is_bad else "")
        if r["n"] == current and not cls:
            cls = "now"
        mark = "✓" if r["status"] == "done" else ("✕" if cls == "bad" else str(r["n"]))
        sub = []
        if show_status and r["status"] not in ("pending", "done"):
            sub.append(esc(r["status_text"]))
        if r["note"]:
            sub.append(f'<span class="err">{esc(r["note"])}</span>')
        program = ""
        title = r["text"]
        if r["content"].get("skill") == "pcr_start_program":
            program = pcr_program(r["content"].get("args") or {})
            title = "启动 PCR 程序"
        small = f"<small>{' · '.join(sub)}</small>" if sub else ""
        items.append(
            f'<li class="{cls}"><div class="k">{mark}</div>'
            f'<div class="d">{esc(title)}{small}{program}</div>'
            f'<div class="r">{esc(r["right"])}</div></li>'
        )
    return f'<div class="wl"><ol class="wl-steps">{"".join(items)}</ol></div>'


def run_now(rows: list[dict[str, Any]], *, status: str, remaining: float, frozen: bool) -> str:
    """Progress meter and the step in progress for the execution view."""
    done = sum(1 for r in rows if r["status"] == "done")
    total = len(rows)
    cur = next((r for r in rows if r["status"] != "done"), None)
    pct = 100 * done / total if total else 0
    if status in ("running", "paused") and cur is not None:
        label = "冻结在" if frozen else ("暂停在" if status == "paused" else "正在进行")
        now_text = f'{label}第 {cur["n"]} 步：{cur["text"]}'
        detail = cur["right"]
    else:
        text, _ = labels.status(labels.PROTOCOL_STATUS, status)
        now_text = f"方案{text}"
        detail = ""
    is_active = status in ("running", "paused")
    left = f"预计还需约 {labels.duration_text(remaining)}" if is_active else ""
    detail_row = f"<div class=row><span>{esc(detail)}</span></div>" if detail else ""
    return (
        '<div class="wl wl-card wl-now">'
        f'<div class="row"><span class="wl-big">{done} / {total} 步</span>'
        f"<span>{esc(left)}</span></div>"
        f'<div class="wl-meter"><div style="width:{pct:.1f}%"></div></div>'
        f'<div class="cur">{esc(now_text)}</div>{detail_row}</div>'
    )
