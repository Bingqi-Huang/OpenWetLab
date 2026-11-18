"""Shown on every page: island mode banner, live status bar, command outcomes, and alerts.

The bar re-reads the database every 2 seconds. When the service records something new (an event
or a handled command) or the freeze state flips, the whole page reruns so every view updates.
"""

import streamlit as st

from wetlab.lab.store import inbox

from . import ISLAND_PAGE, actions, components, labels
from .db import open_web_db
from .island_data import frozen_context, read_island

_MARKER = "wl_change_marker"


@st.fragment(run_every=2)
def live_bar() -> None:
    with open_web_db() as conn:
        finished = actions.settle(conn)
        island = read_island(conn)
        marker = (*inbox.change_marker(conn), island["frozen"])
    seen = st.session_state.get(_MARKER)
    st.session_state[_MARKER] = marker
    if finished or (seen is not None and seen != marker):
        st.rerun(scope="app")
    st.html(components.mode_banner(island["mode"]))
    st.html(components.status_bar(island, actions.pending()))
    if island["stop_failed_devices"]:
        st.error(labels.stop_failed_text(island["stop_failed_devices"]))
    last_cmd = island["last_human_command"]
    if last_cmd is not None and not last_cmd["ok"]:
        is_covered_stop = (
            last_cmd["kind"] == "stop_all"
            and bool(island["stop_failed_devices"])
            and not last_cmd["error"]
        )
        if not is_covered_stop:
            reason = last_cmd["error"] or "处理失败"
            st.error(f"最近一条命令未执行（{last_cmd['label']}）：{reason}")


def freeze_notice(*, detailed: bool) -> bool:
    """Red notice while the island is frozen; True when frozen."""
    with open_web_db() as conn:
        ctx = frozen_context(conn)
    if not ctx["freeze"]["frozen"]:
        return False
    st.html(components.freeze_notice(ctx, detailed=detailed))
    if not detailed:
        st.page_link(
            str(ISLAND_PAGE),
            label="去实验岛页处理冻结",
            icon=":material/arrow_forward:",
        )
    return True


def render() -> None:
    actions.show_flash()
    live_bar()
