"""Registering plates put on, moved, or taken off the island (`place/correct/remove_labware`)."""

from typing import Any

import streamlit as st

from wetlab.lab.devices import POSITIONS

from . import actions, labels

_REMOVED = "removed"


def _close(popover_key: str) -> None:
    st.session_state[popover_key] = False


def _place() -> None:
    lw_id = st.session_state["place_id"].strip()
    lw_type = st.session_state["place_type"].strip()
    pos = st.session_state["place_pos"]
    if not lw_id or not lw_type or pos is None:
        actions.flash("error", "请填写孔板编号、类型，并选择位置。")
        return
    payload = {"labware_id": lw_id, "type": lw_type, "position": pos}
    label = f"登记 {lw_id} 放在 {labels.position_label(pos)}"
    actions.send("place_labware", payload, target_id=lw_id, label=label)
    _close("pop_place")


def _correct() -> None:
    lw_id = st.session_state["correct_id"]
    pos = st.session_state["correct_pos"]
    if lw_id is None or pos is None:
        actions.flash("error", "请选择孔板和它现在的实际位置。")
        return
    actual_pos = None if pos == _REMOVED else pos
    label = f"更正 {lw_id} 的位置为 {labels.position_label(actual_pos)}"
    actions.send(
        "correct_labware",
        {"labware_id": lw_id, "position": actual_pos},
        target_id=lw_id,
        label=label,
    )
    _close("pop_correct")


def _remove() -> None:
    lw_id = st.session_state["remove_id"]
    if lw_id is None:
        actions.flash("error", "请选择要取走的孔板。")
        return
    actions.send(
        "remove_labware",
        {"labware_id": lw_id},
        target_id=lw_id,
        label=f"登记 {lw_id} 已取走",
    )
    _close("pop_remove")


def _next_id(known: list[str]) -> str:
    n = len(known) + 1
    while f"LW-{n:03d}" in known:
        n += 1
    return f"LW-{n:03d}"


def _actual_pos_label(pos: str) -> str:
    return "已不在实验岛上" if pos == _REMOVED else labels.position_label(pos)


def toolbar(on_island: dict[str, dict[str, Any]], known: list[str]) -> None:
    """Three actions; each list only offers choices the ledger can accept right now."""
    occupied = {info["position"] for info in on_island.values()}
    free = [p for p in POSITIONS if p not in occupied]
    row = st.container(horizontal=True, gap="small")
    with row.popover("放上孔板", icon=":material/add:", key="pop_place", on_change="rerun"):
        with st.form("form_place", border=False, clear_on_submit=True):
            st.text_input("孔板编号", value=_next_id(known), key="place_id")
            st.text_input("类型", value="96孔PCR板", key="place_type")
            st.selectbox(
                "放在",
                free,
                format_func=labels.position_label,
                index=0 if free else None,
                placeholder="台面已满",
                key="place_pos",
            )
            st.form_submit_button(
                "登记放置",
                type="primary",
                on_click=_place,
                key="place_submit",
                disabled=not free,
            )
    with row.popover(
        "更正位置",
        icon=":material/edit_location_alt:",
        key="pop_correct",
        on_change="rerun",
        disabled=not known,
    ):
        with st.form("form_correct", border=False):
            st.selectbox("孔板", known, index=None, placeholder="选择孔板", key="correct_id")
            st.selectbox(
                "现场实际位置",
                [*free, _REMOVED],
                index=None,
                placeholder="选择位置",
                format_func=_actual_pos_label,
                key="correct_pos",
            )
            st.caption("账本和现场不一致时使用。只能更正到空位。")
            st.form_submit_button(
                "提交更正",
                type="primary",
                on_click=_correct,
                key="correct_submit",
            )
    with row.popover(
        "取走孔板",
        icon=":material/logout:",
        key="pop_remove",
        on_change="rerun",
        disabled=not on_island,
    ):
        with st.form("form_remove", border=False):
            st.selectbox(
                "孔板",
                sorted(on_island),
                index=None,
                placeholder="选择孔板",
                format_func=lambda i: f"{i}（{labels.position_label(on_island[i]['position'])}）",
                key="remove_id",
            )
            st.form_submit_button(
                "确认已取走",
                type="primary",
                on_click=_remove,
                key="remove_submit",
            )
