"""方案: what will run, rehearsal results, on-site checklist, and approve or reject."""

from typing import Any

import streamlit as st

from wetlab.app.web import actions, components, labels, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.mission_data import planned_rows
from wetlab.lab.protocol import labware_paths
from wetlab.lab.store import runs


def _approve(proto: dict[str, Any]) -> None:
    name = st.session_state[f"approver_{proto['id']}"].strip()
    payload = {
        "protocol_id": proto["id"],
        "decided_by": name,
        "fingerprint": proto["fingerprint"],
    }
    actions.send(
        "approve",
        payload,
        who=name,
        target_id=proto["id"],
        label=f"批准方案 {proto['id']}",
    )


def _reject(proto: dict[str, Any]) -> None:
    note = st.session_state[f"reject_note_{proto['id']}"].strip()
    if not note:
        actions.flash("error", "退回时请写下修改意见，实验助手会按意见修改。")
        return
    who = st.session_state[f"approver_{proto['id']}"].strip() or "研究人员"
    payload = {"protocol_id": proto["id"], "decided_by": who, "comment": note}
    actions.send(
        "reject",
        payload,
        who=who,
        target_id=proto["id"],
        label=f"退回方案 {proto['id']}",
    )


def _go(view_key: str, view: str) -> None:
    st.session_state[view_key] = view


def _banner(proto: dict[str, Any], view_key: str) -> None:
    text, tone = labels.status(labels.PROTOCOL_STATUS, proto["status"])
    decided_at = labels.clock_text(proto["decided_at"])
    if proto["status"] == "submitted":
        note = f"{labels.clock_text(proto['submitted_at'])} 提交，等待您审阅"
    elif proto["status"] == "rejected":
        by = proto["decided_by"] or "系统"
        note = f"{by} 于 {decided_at} 退回：{proto['reject_comment'] or ''}"
    else:
        note = f"{proto['decided_by']} 于 {decided_at} 批准"
    left, right = st.columns([5, 1], vertical_alignment="center")
    pill_html = ui.pill(text, tone)
    left.html(f'<div class="wl">{pill_html} <span class="wl-muted">{ui.esc(note)}</span></div>')
    if proto["status"] not in ("submitted", "rejected"):
        right.button(
            "查看执行",
            key=f"goto_run_{proto['id']}",
            on_click=_go,
            args=(view_key, "执行"),
            width="stretch",
        )


def _summary(proto: dict[str, Any]) -> None:
    reh = proto["rehearsal"] or {}
    verdict = (
        ui.pill("预演通过，无事故", "green")
        if reh.get("ok")
        else ui.pill("预演未通过", "red")
    )
    rows = [
        ("预计总耗时", labels.duration_text(float(reh.get("total_s", 0.0)))),
        ("步骤", f"{len(proto['content']['steps'])} 步"),
        ("版本", f"第 {proto['version']} 版（{proto['id']}）"),
    ]
    fail = reh.get("failure")
    if fail:
        rows.append(("未通过原因", f"第 {fail.get('step_n', '?')} 步：{fail.get('reason', '')}"))
    for acc in reh.get("accidents") or []:
        rows.append(("虚拟岛事故", str(acc)))
    st.html(
        f'<div class="wl wl-card"><div style="margin-bottom:10px">{verdict}</div>'
        f"{ui.kv(rows)}</div>"
    )


def _decision(proto: dict[str, Any]) -> None:
    pid = proto["id"]
    with open_web_db() as conn:
        running = runs.list_protocols(conn, status="running")
        paused = runs.list_protocols(conn, status="paused")
        busy = running + paused
    with st.container(border=True):
        st.markdown("**批准前，请到现场逐项核对**")
        checks = [
            st.checkbox(f"{lw} 已放在 {labels.position_label(pos)}", key=f"chk_{pid}_{lw}")
            for lw, pos in sorted(proto["content"].get("labware", {}).items())
        ]
        checks.append(
            st.checkbox("其余台面、仪器门盖与耗材也与方案要求一致", key=f"chk_{pid}_site")
        )
        name = st.text_input("批准人", key=f"approver_{pid}", placeholder="您的姓名")
        ready = all(checks) and bool(name.strip()) and not busy
        st.button(
            "批准并开始执行",
            type="primary",
            key=f"approve_{pid}",
            disabled=not ready,
            on_click=_approve,
            args=(proto,),
            width="stretch",
        )
        if busy:
            st.caption(f"方案 {busy[-1]['id']} 正在执行。它结束后才能批准新方案。")
        elif not ready:
            st.caption("勾选全部核对项并填写姓名后，才能批准。")
    with st.container(border=True):
        st.markdown("**需要修改？**")
        st.text_area(
            "修改意见",
            key=f"reject_note_{pid}",
            label_visibility="collapsed",
            height=90,
            placeholder="例如：退火温度改为 62℃；开始前先振荡混匀 10 秒",
        )
        st.button(
            "退回给实验助手",
            key=f"reject_{pid}",
            on_click=_reject,
            args=(proto,),
            width="stretch",
        )


def _paths(proto: dict[str, Any]) -> str:
    paths = labware_paths(proto["content"])
    if not paths:
        return '<div class="wl wl-muted">方案不需要台面上预置孔板。</div>'
    rows = []
    for lw, stops in paths.items():
        chain = " → ".join(labels.position_label(p) for p in stops)
        suffix = "（全程不移动）" if len(stops) == 1 else ""
        rows.append((lw, f"{chain}{suffix}"))
    return f'<div class="wl">{ui.kv(rows)}</div>'


def render(detail: dict[str, Any], view_key: str) -> None:
    protos = detail["protocols"]
    if not protos:
        ui.empty("还没有方案", "实验助手正在根据您的要求制定方案，提交后会出现在这里。")
        return
    by_id = {p["id"]: p for p in protos}
    default = (detail["submitted"] or protos)[-1]["id"]
    chosen = default
    if len(protos) > 1:
        chosen = st.segmented_control(
            "版本",
            [p["id"] for p in reversed(protos)],
            default=default,
            required=True,
            key=f"version_{detail['mission']['id']}",
            label_visibility="collapsed",
            format_func=lambda pid: (
                f"{pid} · {labels.status(labels.PROTOCOL_STATUS, by_id[pid]['status'])[0]}"
            ),
        )
    proto = by_id[chosen]
    _banner(proto, view_key)
    main, side = st.columns([3, 2], gap="large")
    with main:
        ui.section(proto["content"].get("name", proto["id"]), "每一步的内容与预演用时")
        st.html(components.steps(planned_rows(proto), current=None, show_status=False))
        ui.section("孔板去向")
        st.html(_paths(proto))
    with side:
        ui.section("预演结论")
        _summary(proto)
        if proto["status"] == "submitted":
            ui.section("您的决定")
            _decision(proto)
