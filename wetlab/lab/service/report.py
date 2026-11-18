"""Mission report Markdown renderer (`report.md`, 04 section 6.5)."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3

from ..protocol.render import format_contents_text
from .report_scope import MissionReportData, collect_mission_data


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _render_part_one(data: MissionReportData) -> list[str]:
    mission = data.mission
    lines = [
        f"# 任务实验报告：{mission['title']}（{mission['id']}）",
        "",
        "## 第一部分：事实记录（由数据库自动生成）",
        "",
        "### 1. 任务概览",
        f"- **任务编号**：`{mission['id']}`",
        f"- **标题**：{mission['title']}",
        f"- **模式**：`{mission['mode']}`",
        f"- **当前状态**：`{mission['status']}`",
        f"- **创建时间**：{_fmt_ts(mission['created_at'])}",
        "",
        "### 2. 方案与人工批准记录",
    ]
    if not data.protocols:
        lines.append("- （无已提交方案）")
    for proto in data.protocols:
        dec_at = _fmt_ts(proto["decided_at"])
        decided = (
            f"批准/决定人：{proto['decided_by']}（{dec_at}）"
            if proto["decided_by"]
            else "尚未人工审批"
        )
        rej = proto["reject_comment"]
        comment = f"；退回/拒绝原因：{rej}" if rej else ""
        fp_short = proto["fingerprint"][:16]
        lines.append(
            f"- `{proto['id']}`（实验 `{proto['experiment_id']}` v{proto['version']}）："
            f"状态 `{proto['status']}`，指纹 `{fp_short}…`，{decided}{comment}"
        )
    lines.append("")

    lines.append("### 3. 方案步骤执行与耗时")
    any_steps = False
    for proto in data.protocols:
        pid = proto["id"]
        for s in data.steps_by_proto.get(pid, []):
            any_steps = True
            res = s["result"] or {}
            label = s["content"].get("skill") or s["kind"]
            fallback_s = (
                round(s["finished_at"] - s["started_at"], 3)
                if s["started_at"] is not None and s["finished_at"] is not None
                else 0.0
            )
            elapsed = res.get("elapsed_s", fallback_s)
            step_ops = res.get("operation_ids") or [
                op["id"]
                for op in data.operations
                if op["protocol_id"] == pid and op["step_n"] == s["n"]
            ]
            op_str = ", ".join(f"`{oid}`" for oid in step_ops) if step_ops else "无底层指令"
            lines.append(
                f"- `{pid}` 第 {s['n']} 步 `{label}`：状态 `{s['status']}`，"
                f"耗时 `{elapsed}s`，关联操作 [{op_str}]"
            )
    if not any_steps:
        lines.append("- （无执行步骤）")
    lines.append("")

    lines.append("### 4. 底层硬件操作明细（`operations` 表逐项核对）")
    if not data.operations:
        lines.append("- （无底层硬件操作记录）")
    else:
        lines.append("| 序号 | 操作编号 | 方案 / 步号 | 设备 | 动作 | 状态 | 耗时(s) | 错误码 | 参数 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for op in data.operations:
            sn = op["step_n"] if op["step_n"] is not None else "—"
            step_ref = f"{op['protocol_id'] or '—'}#{sn}"
            params_str = json.dumps(op["params"], ensure_ascii=False)
            err_code = op["error_code"] or "—"
            lines.append(
                f"| {op['seq']} | `{op['id']}` | `{step_ref}` | `{op['device']}` | "
                f"`{op['action']}` | `{op['status']}` | {op['elapsed_s']:.3f} | "
                f"`{err_code}` | `{params_str}` |"
            )
    lines.append("")

    lines.append("### 5. 器皿位置与完整经历")
    if not data.labware_ids:
        lines.append("- （本任务未声明器皿）")
    for lw_id in data.labware_ids:
        lw = data.labware_rows.get(lw_id)
        lw_type = lw["type"] if lw else "未知类型"
        pos_str = f"`{lw['position']}`" if (lw and lw["position"]) else "已移出实验岛"
        items = data.labware_contents.get(lw_id, [])
        contents_txt = format_contents_text(items) if items else "未登记"
        lines.append(f"- **`{lw_id}`**（类型 `{lw_type}`，当前位置：{pos_str}）：")
        lines.append(f"  - 板内内容：{contents_txt}")
        evs = data.labware_events.get(lw_id, [])
        if not evs:
            lines.append("  - （本任务期间无经历记录）")
        for ev in evs:
            detail = ev["detail"] or {}
            detail_json = json.dumps(detail, ensure_ascii=False)
            detail_str = f" {detail_json}" if detail else ""
            op_ref = f"（操作 `{ev['operation_id']}`）" if ev["operation_id"] else ""
            ts_str = _fmt_ts(ev["at"])
            if ev["what"] == "contents_updated":
                upd_items = detail.get("contents") or []
                upd_txt = format_contents_text(upd_items) if upd_items else "清空内容"
                lines.append(
                    f"  - [{ts_str}] 由 `{ev['by']}` 修改板内内容"
                    f"（`contents_updated`：{upd_txt}）{detail_str}"
                )
            else:
                lines.append(
                    f"  - [{ts_str}] 由 `{ev['by']}` 记录 `{ev['what']}`{detail_str}{op_ref}"
                )
    lines.append("")

    lines.append("### 6. 偏差、暂停与人工干预记录")
    deviations: list[str] = []
    for proto in data.protocols:
        pid = proto["id"]
        if proto["status"] in ("rejected", "failed", "stopped", "aborted"):
            suffix = f"（{proto['reject_comment']}）" if proto["reject_comment"] else ""
            deviations.append(f"方案 `{pid}` 终态 `{proto['status']}`{suffix}")
        for s in data.steps_by_proto.get(pid, []):
            if s["status"] in ("failed", "unknown"):
                err = (s["result"] or {}).get("error")
                deviations.append(f"方案 `{pid}` 第 {s['n']} 步 `{s['status']}`：{err}")

    for cmd in data.intervention_commands:
        out_txt = cmd["outcome"] or "待处理"
        deviations.append(
            f"[{_fmt_ts(cmd['at'])}] 人工命令 `{cmd['kind']}`"
            f"（发起人 `{cmd['who']}`，结果 `{out_txt}`）"
        )
    for ev in data.alert_events:
        ev_json = json.dumps(ev["payload"], ensure_ascii=False)
        deviations.append(f"[{_fmt_ts(ev['at'])}] 系统事件 `{ev['kind']}`：{ev_json}")

    if not deviations:
        lines.append("- 无偏差、暂停或人工干预。")
    for item in deviations:
        lines.append(f"- {item}")
    lines.append("")
    return lines


def _render_part_two(data: MissionReportData) -> list[str]:
    lines = [
        "## 第二部分：说明与结论（操作员与协调者撰写）",
        "",
        "### 1. 各实验操作员说明（Operator `notes`）",
    ]
    if not data.experiments:
        lines.append("- （无实验记录）")
    for exp in data.experiments:
        notes_txt = exp["notes"] or "（暂无说明）"
        lines.append(f"- **`{exp['id']}`**（状态 `{exp['status']}`）：{notes_txt}")
    lines.append("")
    lines.extend(
        [
            "### 2. 任务结论（Coordinator `conclusion`）",
            data.mission["conclusion"] or "（暂无结论）",
            "",
        ]
    )
    return lines


def generate_report(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    finished_at: float | None = None,
) -> str:
    """Render two-part `report.md` strictly scoped to `mission_id`."""
    data = collect_mission_data(conn, mission_id, finished_at=finished_at)
    lines = _render_part_one(data)
    lines.extend(_render_part_two(data))
    return "\n".join(lines)


def write_report(
    conn: sqlite3.Connection,
    mission_id: str,
    *,
    runs_dir: str | Path = Path("runs"),
    finished_at: float | None = None,
) -> Path:
    """Generate and save `<runs_dir>/<mission_id>/report.md`."""
    content = generate_report(conn, mission_id, finished_at=finished_at)
    out_path = Path(runs_dir) / mission_id / "report.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    return out_path
