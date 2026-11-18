"""Registering plates put on, moved, or taken off the island (`place/correct/remove_labware`)."""

import math
import re
from typing import Any

import streamlit as st

from wetlab.lab.devices import LABWARE_POSITIONS

from . import actions, labels

_REMOVED = "removed"
_EMPTY_ROWS: list[dict[str, Any]] = [{"name": "", "wells": "", "volume_ul": None, "note": ""}]


def _close(popover_key: str) -> None:
    st.session_state[popover_key] = False


def _clean_str(val: Any) -> str:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return ""
    return str(val).strip()


def _parse_wells(val: Any) -> list[str] | None:
    if isinstance(val, list):
        items = [str(w).strip() for w in val if str(w).strip()]
        return items or None
    text = _clean_str(val)
    if not text:
        return None
    items = [part.strip() for part in re.split(r"[,，;；]+", text) if part.strip()]
    return items or None


def _parse_volume(val: Any) -> tuple[float | None, str | None]:
    if val is None or val == "" or (isinstance(val, float) and math.isnan(val)):
        return None, None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None, "体积请填写数字，或留空。"
    if math.isnan(num) or num < 0:
        return None, "体积不能小于 0。"
    return num, None


def _resolve_editor_rows(
    base_rows: list[dict[str, Any]],
    state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    rows = [dict(r) for r in base_rows]
    edited = (state or {}).get("edited_rows") or {}
    for raw_idx in sorted(edited, key=int):
        rows[int(raw_idx)].update(edited[raw_idx])
    deleted = (state or {}).get("deleted_rows") or []
    for idx in sorted((int(i) for i in deleted), reverse=True):
        rows.pop(idx)
    for added in (state or {}).get("added_rows") or []:
        rows.append(dict(added))
    return rows


def _parse_contents(
    base_rows: list[dict[str, Any]],
    state: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    items: list[dict[str, Any]] = []
    for row in _resolve_editor_rows(base_rows, state):
        name = _clean_str(row.get("name"))
        wells = _parse_wells(row.get("wells"))
        note = _clean_str(row.get("note")) or None
        vol_res = _parse_volume(row.get("volume_ul"))
        if vol_res[1] is not None:
            return None, vol_res[1]
        vol = vol_res[0]
        if not name and wells is None and vol is None and note is None:
            continue
        if not name:
            return None, "填写孔板内容时，每一行都请填写样品或试剂名称。"
        items.append({"name": name, "wells": wells, "volume_ul": vol, "note": note})
    return items, None


def _contents_columns() -> dict[str, Any]:
    return {
        "name": st.column_config.TextColumn("名称"),
        "wells": st.column_config.TextColumn("孔位（留空为整板）"),
        "volume_ul": st.column_config.NumberColumn("体积 (µL)", min_value=0.0),
        "note": st.column_config.TextColumn("备注"),
    }


def _place() -> None:
    lw_id = st.session_state["place_id"].strip()
    lw_type = st.session_state["place_type"].strip()
    pos = st.session_state["place_pos"]
    if not lw_id or not lw_type or pos is None:
        actions.flash("error", "请填写孔板编号、类型，并选择位置。")
        return
    parsed = _parse_contents(_EMPTY_ROWS, st.session_state.get("place_contents"))
    if parsed[1] is not None:
        actions.flash("error", parsed[1])
        return
    payload: dict[str, Any] = {"labware_id": lw_id, "type": lw_type, "position": pos}
    if parsed[0]:
        payload["contents"] = parsed[0]
    label = f"登记 {lw_id} 放在 {labels.position_label(pos)}"
    actions.send("place_labware", payload, target_id=lw_id, label=label)
    st.session_state.pop("place_contents", None)
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
    free = [p for p in LABWARE_POSITIONS if p not in occupied]
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
            st.caption("板内物料（可留空；多个孔位用逗号分隔，如 A1, A2）")
            st.data_editor(
                _EMPTY_ROWS,
                num_rows="dynamic",
                hide_index=True,
                column_config=_contents_columns(),
                key="place_contents",
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
