"""实验岛: what needs a person, what is running, plates on the bench, and device state."""

import streamlit as st

from wetlab.app.web import MISSIONS_PAGE, components, freeze, labware, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.island_data import read_island, todos
from wetlab.app.web.mission_data import remaining_estimate, step_rows
from wetlab.lab.store import records, runs

with open_web_db() as conn:
    data = read_island(conn)
    todo_items = todos(conn)
    known = [lw["id"] for lw in records.list_labware(conn, active_only=False)]

ui.page_head("实验岛", "现场设备与台面，以及需要您处理的事。页面每 2 秒自动更新。")

if data["frozen"]:
    freeze.page_section()

if todo_items:
    ui.section("需要您处理", f"{len(todo_items)} 项")
    for item in todo_items:
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.html(components.todo(item))
            st.page_link(
                str(MISSIONS_PAGE),
                label=item["action"],
                icon=":material/arrow_forward:",
                query_params={"mission": item["mission_id"]},
            )


@st.fragment(run_every=2)
def _running() -> None:
    with open_web_db() as conn:
        state = read_island(conn)
        active = state["active"]
        now = state["now"]
        if active is None:
            return
        proto = active["protocol"]
        rows = step_rows(proto, runs.list_steps(conn, proto["id"]), now)
    ui.section("正在执行", f"方案 {proto['id']} · {proto['content'].get('name', '')}")
    rem = remaining_estimate(rows, now)
    st.html(components.run_now(rows, status=proto["status"], remaining=rem, frozen=state["frozen"]))
    st.page_link(
        str(MISSIONS_PAGE),
        label="查看执行详情与控制",
        icon=":material/arrow_forward:",
        query_params={"mission": active["mission_id"]},
    )


_running()

ui.section("台面与器皿", "孔板由人放上和取走，在这里登记后系统按此记账")
labware.toolbar(data["labware"], known)
st.html(components.bench(data["tiles"]))


@st.fragment(run_every=2)
def _devices() -> None:
    with open_web_db() as conn:
        state = read_island(conn)
    if state["closed_skills"]:
        closed_str = "、".join(state["closed_skills"])
        st.info(f"当前实验岛暂未开放的技能：{closed_str}（含未开放技能的方案在提交和批准时会被拦截）。")
    st.html(components.device_cards(state["devices"]))


ui.section("设备", "关键读数与状态是否新鲜")
_devices()
