"""任务: all missions on the left; conversation, protocol, execution, report on the right."""

import streamlit as st

from wetlab.app.web import actions, components, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.mission import conversation, execution, new, protocol, report
from wetlab.app.web.mission_data import VIEWS, list_missions, mission_detail

NEW = "new"

with open_web_db() as conn:
    missions = list_missions(conn)
by_id = {m["id"]: m for m in missions}
options = [NEW, *by_id]

# 选中的任务记在地址栏 ?mission=<编号>：其他页面的链接可以直接打开某个任务，刷新页面也不丢。
if actions.SELECT_MISSION in st.session_state:
    st.query_params["mission"] = st.session_state.pop(actions.SELECT_MISSION)
if st.query_params.get("mission") in options:
    st.session_state["mission"] = st.query_params["mission"]


def _follow_url() -> None:
    st.query_params["mission"] = st.session_state["mission"]


def _label(mid: str) -> str:
    if mid == NEW:
        return "＋ 新建任务"
    title = by_id[mid]["title"]
    return title if len(title) <= 16 else title[:15] + "…"


def _caption(mid: str) -> str:
    if mid == NEW:
        return ""
    m = by_id[mid]
    flag = f" · :orange[**{m['needs']}**]" if m["needs"] else ""
    return f"{m['status_text']}{flag}"


left, right = st.columns([1, 3.4], gap="large")
with left:
    ui.section("任务", f"共 {len(missions)} 个")
    default_idx = None if "mission" in st.session_state else (1 if missions else 0)
    chosen = st.radio(
        "任务",
        options,
        index=default_idx,
        format_func=_label,
        captions=[_caption(m) for m in options],
        key="mission",
        on_change=_follow_url,
        label_visibility="collapsed",
    )

with right:
    if chosen == NEW or chosen not in by_id:
        new.render()
    else:
        with open_web_db() as conn:
            detail = mission_detail(conn, chosen)
        st.html(components.mission_head(detail["mission"]))
        st.html(components.stepper(detail["stages"]))
        run_proto = detail["run"]
        flags = {
            "对话": detail["asking"],
            "方案": bool(detail["submitted"]),
            "执行": bool(run_proto and run_proto["status"] in ("running", "paused")),
        }
        view_key = f"view_{chosen}"
        seen_key = f"view_default_{chosen}"
        if st.session_state.get(seen_key) != detail["default_view"]:
            st.session_state[seen_key] = detail["default_view"]
            st.session_state[view_key] = detail["default_view"]
        view = st.segmented_control(
            "视图",
            list(VIEWS),
            required=True,
            key=view_key,
            label_visibility="collapsed",
            format_func=lambda v: f"{v} ●" if flags.get(v) else v,
        )
        if view == "对话":
            conversation.render(detail)
        elif view == "方案":
            protocol.render(detail, view_key)
        elif view == "执行":
            execution.render(detail)
        else:
            report.render(detail)
