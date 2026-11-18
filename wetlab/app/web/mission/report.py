"""报告: the generated `report.md`, every protocol decision, and each plate's history."""

from typing import Any

import streamlit as st

from wetlab.app.web import labels, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.mission_data import mission_labware
from wetlab.lab.store import records


def _decisions(detail: dict[str, Any]) -> None:
    rows = []
    for exp in detail["experiments"]:
        exp_text, _ = labels.status(labels.EXPERIMENT_STATUS, exp["status"])
        for p in (p for p in detail["protocols"] if p["experiment_id"] == exp["id"]):
            text, _ = labels.status(labels.PROTOCOL_STATUS, p["status"])
            rows.append(
                [
                    f"{exp['id']}（{exp_text}）",
                    p["id"],
                    text,
                    p["decided_by"] or "—",
                    labels.clock_text(p["decided_at"]),
                    p["reject_comment"] or "",
                ]
            )
    if rows:
        ui.table(["实验", "方案", "状态", "批准 / 退回人", "时间", "退回意见"], rows, wrap=5)
    else:
        st.caption("还没有实验记录。")


def _plates(detail: dict[str, Any]) -> None:
    ids = mission_labware(detail["protocols"])
    if not ids:
        st.caption("本任务的方案没有用到孔板。")
        return
    with open_web_db() as conn:
        histories = {i: records.list_labware_events(conn, i) for i in ids}
    for lw_id, events in histories.items():
        st.markdown(f"**{lw_id}**")
        table_rows = [
            [
                labels.full_time_text(ev["at"]),
                labels.labware_event_text(ev["what"], ev["detail"] or {}),
                "人工" if ev["by"] == "human" else "设备",
            ]
            for ev in events
        ]
        ui.table(["时间", "经历", "来源"], table_rows, wrap=1)


def render(detail: dict[str, Any]) -> None:
    mission = detail["mission"]
    if detail["report"] is None:
        ui.empty(
            "报告还没有生成",
            "任务结束后自动生成：每块板的经历、每一步的结果、批准记录，以及实验助手的结论。",
        )
    else:
        top, dl = st.columns([5, 1], vertical_alignment="center")
        top.caption("报告分两部分：程序记录的事实，以及实验助手写的说明和结论。")
        dl.download_button(
            "下载报告",
            data=detail["report"],
            file_name=f"{mission['id']}-report.md",
            mime="text/markdown",
            icon=":material/download:",
            key="report_download",
            width="stretch",
        )
        with st.container(border=True):
            st.markdown(detail["report"])
    ui.section("方案与批准记录")
    _decisions(detail)
    ui.section("孔板经历", "按时间顺序，含人工登记和设备动作")
    _plates(detail)
