"""对话: assistant questions and replies, user messages, milestones, and research plan."""

from typing import Any

import streamlit as st

from wetlab.app.web import actions, labels, ui
from wetlab.app.web.db import open_web_db
from wetlab.app.web.mission_data import conversation

_AVATARS = {"user": ":material/person:", "assistant": ":material/smart_toy:"}


def _messages(items: list[dict[str, Any]]) -> None:
    for it in items:
        when = labels.clock_text(it["at"])
        if it["role"] == "system":
            ui.sys_line(it["text"], when)
            continue
        with st.chat_message(it["role"], avatar=_AVATARS[it["role"]]):
            who = "您" if it["role"] == "user" else "实验助手"
            tag = f" · {it['tag']}" if it["tag"] else ""
            st.caption(f"{who}{tag} · {when}")
            st.markdown(it["text"])


def render(detail: dict[str, Any]) -> None:
    mission = detail["mission"]
    with open_web_db() as conn:
        items = conversation(conn, detail)
    if detail["plan"]:
        talk, side = st.columns([3, 2], gap="large")
    else:
        talk = st.container()
        side = None
    with talk:
        _messages(items)
        if mission["status"] == "running":
            hint = (
                "回答实验助手的提问"
                if detail["asking"]
                else "给实验助手留言：补充条件、修改要求……"
            )
            text = st.chat_input(hint, key=f"chat_{mission['id']}")
            if text and text.strip():
                actions.send(
                    "message",
                    {"mission_id": mission["id"], "text": text.strip()},
                    target_id=mission["id"],
                    label="发送留言",
                )
                st.rerun()
        else:
            st.caption("任务已结束，不再接收留言。")
    if side is not None:
        with side:
            ui.section("研究计划", "实验助手维护的待办")
            with st.container(border=True):
                st.markdown(detail["plan"])
