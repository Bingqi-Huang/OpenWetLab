"""执行: live progress of the approved protocol, pause / resume / stop all / abort."""

from typing import Any

import streamlit as st

from wetlab.app.web import actions, components, labels, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.island_data import read_island
from wetlab.app.web.mission_data import remaining_estimate, step_rows
from wetlab.lab.store import runs

# 04 第 5.15 节：厂商网关逐条处理指令，正在进行的设备动作结束前，暂停和停止到不了设备。
STOP_LIMITS = (
    "暂停和停止要等当前设备动作做完才生效：机械臂搬运约 40～60 秒，开关仓门约 5 秒，"
    "热盖动作约 8 秒，60 秒以内的定时振荡要等振荡结束。运动中需要立刻停下，请按现场的物理急停按钮。"
)


def _send(
    kind: str,
    proto: dict[str, Any],
    label: str,
    popover_key: str | None = None,
) -> None:
    payload = {"protocol_id": proto["id"]}
    if kind == "abort":
        payload["experiment_id"] = proto["experiment_id"]
    target = proto["experiment_id"] if kind == "abort" else proto["id"]
    sent_payload = {} if kind == "stop_all" else payload
    sent_target = None if kind == "stop_all" else target
    actions.send(kind, sent_payload, target_id=sent_target, label=label)
    if popover_key is not None:
        st.session_state[popover_key] = False


def _controls(proto: dict[str, Any], experiment: dict[str, Any]) -> None:
    pid = proto["id"]
    status = proto["status"]
    row = st.container(horizontal=True, gap="small", vertical_alignment="center")
    if status == "running":
        row.button(
            "暂停",
            icon=":material/pause:",
            key=f"pause_{pid}",
            on_click=_send,
            args=("pause", proto, f"暂停方案 {pid}"),
            help="不再发出下一步指令。设备上已经开始的程序（例如 PCR 程序）会继续运行。",
        )
    if status == "paused":
        row.button(
            "继续执行",
            icon=":material/play_arrow:",
            type="primary",
            key=f"resume_{pid}",
            on_click=_send,
            args=("resume", proto, f"继续方案 {pid}"),
        )
    if status in ("running", "paused"):
        with row.popover(
            "全部停止",
            icon=":material/stop_circle:",
            key=f"pop_stop_{pid}",
            on_change="rerun",
        ):
            st.markdown("**停止所有正在运行的设备程序？**")
            st.caption(
                "PCR 程序和振荡会被打断，热盖加热关闭。停止后不能继续，剩下的步骤需要实验助手"
                "提出修订方案、您重新批准，或者终止实验。"
            )
            st.button(
                "确认全部停止",
                type="primary",
                key=f"stop_{pid}",
                on_click=_send,
                args=("stop_all", proto, "全部停止", f"pop_stop_{pid}"),
            )
    if experiment["status"] == "running" and status in ("running", "paused", "stopped", "failed"):
        with row.popover(
            "终止实验",
            icon=":material/cancel:",
            key=f"pop_abort_{pid}",
            on_change="rerun",
        ):
            st.markdown(f"**终止实验 {experiment['id']}？**")
            st.caption("方案记为已终止，实验结束，不再执行任何步骤。实验助手会据此决定下一步。")
            st.button(
                "确认终止",
                type="primary",
                key=f"abort_{pid}",
                on_click=_send,
                args=("abort", proto, f"终止实验 {experiment['id']}", f"pop_abort_{pid}"),
            )
    st.caption(STOP_LIMITS)
    if status == "stopped":
        st.info("方案已被人工停止，不能直接继续。请等实验助手提出修订方案，或终止实验。")
    if status == "failed":
        st.warning("方案在某一步失败。实验助手会读取结果，提出修订方案或结束实验。")


@st.fragment(run_every=2)
def _live(protocol_id: str) -> None:
    with open_web_db() as conn:
        state = read_island(conn)
        now = state["now"]
        proto = runs.get_protocol(conn, protocol_id)
        assert proto is not None
        rows = step_rows(proto, runs.list_steps(conn, protocol_id), now)
    is_active = proto["status"] in ("running", "paused")
    current = next((r["n"] for r in rows if r["status"] != "done"), None) if is_active else None
    rem = remaining_estimate(rows, now)
    st.html(components.run_now(rows, status=proto["status"], remaining=rem, frozen=state["frozen"]))
    ui.section("步骤", "当前步骤高亮；等待类步骤显示最多还会等多久")
    st.html(components.steps(rows, current=current, show_status=True))


def render(detail: dict[str, Any]) -> None:
    proto = detail["run"]
    if proto is None:
        ui.empty("还没有开始执行", "方案经您批准后，这里显示每一步的进度，并可以暂停或停止。")
        return
    experiment = next(e for e in detail["experiments"] if e["id"] == proto["experiment_id"])
    text, tone = labels.status(labels.PROTOCOL_STATUS, proto["status"])
    pill_html = ui.pill(text, tone, live=proto["status"] == "running")
    decided_str = labels.clock_text(proto["decided_at"])
    by_str = ui.esc(proto["decided_by"] or "")
    st.html(
        f'<div class="wl">{pill_html} <b>{ui.esc(proto["content"].get("name", ""))}</b> '
        f'<span class="wl-muted">{ui.esc(proto["id"])} · {by_str} 批准于 '
        f"{ui.esc(decided_str)}</span></div>"
    )
    _controls(proto, experiment)
    _live(proto["id"])
