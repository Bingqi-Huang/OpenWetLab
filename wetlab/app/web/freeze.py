"""Person's verdict after checking the island on site, lifting a freeze (04 §5.14 & §8)."""

import streamlit as st

from wetlab.lab.devices import POSITIONS

from . import actions, labels, ui
from .db import open_web_db
from .header import freeze_notice
from .island_data import frozen_context

CONCLUSIONS = {
    "step_done": ("这一步已经完成", "不再重复执行，从下一步继续"),
    "step_failed": ("这一步没有完成", "方案记为失败，实验助手会提出只含剩余步骤的修订方案"),
    "abort": ("终止实验", "结束本次实验，不再执行任何步骤"),
}


def _pos_label(pos: str) -> str:
    return "已不在实验岛上" if pos == "removed" else labels.position_label(pos)


def resolve_panel() -> None:
    with open_web_db() as conn:
        ctx = frozen_context(conn)
    lw_id = ctx["transfer_labware_id"]
    with st.container(border=True):
        st.markdown("**到现场检查后，请选择结论**")
        with st.form("wl_unfreeze", border=False):
            conclusion = st.radio(
                "结论",
                list(CONCLUSIONS),
                format_func=lambda k: CONCLUSIONS[k][0],
                captions=[CONCLUSIONS[k][1] for k in CONCLUSIONS],
                index=None,
                key="unfreeze_conclusion",
                label_visibility="collapsed",
            )
            position = None
            if lw_id is not None:
                position = st.selectbox(
                    f"冻结时机械臂正在搬运 {lw_id}。这块板现在实际在哪里？",
                    [*POSITIONS, "removed"],
                    format_func=_pos_label,
                    index=None,
                    placeholder="请到现场确认后选择",
                    key="unfreeze_position",
                )
            submitted = st.form_submit_button(
                "确认结论并解除冻结",
                type="primary",
                key="unfreeze_submit",
            )
    if not submitted:
        return
    if conclusion is None:
        st.warning("请先选择一个结论。")
        return
    if lw_id is not None and position is None:
        st.warning("这一步是机械臂搬运，必须先选择这块板现在的实际位置。")
        return
    payload: dict[str, str] = {"conclusion": conclusion}
    if lw_id is not None and position is not None:
        payload["labware_id"] = lw_id
        payload["labware_position"] = position
    actions.send("unfreeze", payload, label=f"解除冻结（{CONCLUSIONS[conclusion][0]}）")
    st.rerun()


def page_section() -> None:
    """Top of the island page while frozen: what happened, then the verdict form."""
    ui.section("冻结处理", "设备已停止接收新指令，等您到现场确认")
    freeze_notice(detailed=True)
    resolve_panel()
