"""Starting a research mission: the goal or SOP in plain words (`new_mission`)."""

import streamlit as st

from wetlab.app.web import ISLAND_PAGE, actions, labels, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.island_data import read_island

_HINT = (
    "例如：PCR 仪里的 LW-001 依次 37℃ 15 分钟、53℃ 15 分钟、90℃ 10 分钟，"
    "再跑 98℃ 1 分钟 →（98℃ 10 秒 → 60℃ 5 秒 → 72℃ 5 秒）× 23 → 72℃ 1 分钟。孔内 50 µL。"
)


def _submit() -> None:
    title = st.session_state["nm_title"].strip()
    text = st.session_state["nm_text"].strip()
    if not title or not text:
        actions.flash("error", "请填写任务标题和实验要求。")
        return
    actions.send("new_mission", {"title": title, "input_text": text}, label=f"新建任务“{title}”")
    st.session_state["nm_title"] = ""
    st.session_state["nm_text"] = ""


def render() -> None:
    ui.page_head(
        "新建研究任务",
        "写下实验目标或 SOP。实验助手据此制定方案，经您审阅批准后才会执行。",
    )
    with open_web_db() as conn:
        state = read_island(conn)
    plates = state["labware"]
    on_bench = "、".join(
        f"{lw_id}（{labels.position_label(info['position'])}）"
        for lw_id, info in sorted(plates.items())
    )
    note, link = st.columns([4, 1], vertical_alignment="center")
    bench_msg = (
        f"台面上已登记：{on_bench}"
        if on_bench
        else "台面上还没有登记孔板。需要用到孔板的实验，请先把板放上台面并登记。"
    )
    note.caption(bench_msg)
    link.page_link(str(ISLAND_PAGE), label="登记孔板", icon=":material/arrow_forward:")
    if state["closed_skills"]:
        closed_str = "、".join(state["closed_skills"])
        st.info(f"当前实验岛暂未开放的技能：{closed_str}（提交方案时会自动避开未开放技能）。")
    mode_name = labels.MODE_LABELS.get(state["mode"], state["mode"])
    with st.form("form_new_mission", border=True):
        st.text_input("任务标题", key="nm_title", placeholder="例如：SynCom 文库制备 · 温度部分")
        st.text_area("实验要求", key="nm_text", height=200, placeholder=_HINT)
        st.caption(f"任务将提交到当前连接的实验岛：{mode_name}（{state['mode']}）")
        st.form_submit_button("提交任务", type="primary", on_click=_submit, key="nm_submit")
