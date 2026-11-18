"""The web's only write path: enqueue in `commands`, then report how service handled it."""

import json
from typing import Any
import uuid

import streamlit as st

from wetlab.lab.snapshot import read_snapshot
from wetlab.lab.store import inbox

from .db import island_now, open_web_db

_PENDING = "wl_pending_commands"
_FLASH = "wl_flash"
SELECT_MISSION = "wl_select_mission"


def send(
    kind: str,
    payload: dict[str, Any],
    *,
    label: str,
    target_id: str | None = None,
    who: str = "web",
) -> None:
    """Enqueue one command and remember it so the header can report the outcome."""
    key = f"web:{kind}:{uuid.uuid4().hex}"
    with open_web_db() as conn, conn:
        inbox.enqueue_command(
            conn,
            key=key,
            at=island_now(read_snapshot(conn)["last_timestamp"]),
            who=who,
            kind=kind,
            target_id=target_id,
            payload=payload,
        )
    items = st.session_state.setdefault(_PENDING, [])
    items.append({"key": key, "kind": kind, "label": label})


def pending() -> list[dict[str, str]]:
    """Commands sent from this browser session that the service has not handled yet."""
    return list(st.session_state.get(_PENDING, []))


def _failure_reason(outcome: dict[str, Any]) -> str:
    if outcome.get("error"):
        return str(outcome["error"])
    failed_devs = outcome.get("failed_devices")
    if failed_devs:
        return f"设备 {', '.join(str(d) for d in failed_devs)} 没有停下来"
    return "未说明"


def settle(conn: Any) -> bool:
    """Move handled commands into flash messages; True when any finished since last check."""
    still: list[dict[str, str]] = []
    finished = False
    for item in st.session_state.get(_PENDING, []):
        cmd = inbox.get_command_by_key(conn, item["key"])
        if cmd is None or cmd["handled_at"] is None:
            still.append(item)
            continue
        finished = True
        outcome = json.loads(cmd["outcome"]) if cmd["outcome"] else {}
        if outcome.get("ok"):
            flash("ok", f"{item['label']}：已完成")
            if item["kind"] == "new_mission" and outcome.get("mission_id"):
                st.session_state[SELECT_MISSION] = outcome["mission_id"]
        else:
            reason = _failure_reason(outcome)
            flash("error", f"{item['label']}：实验岛服务没有执行。原因：{reason}")
    st.session_state[_PENDING] = still
    return finished


def flash(kind: str, text: str) -> None:
    """Queue a message to show once on the next page run (`ok`, `info`, or `error`)."""
    st.session_state.setdefault(_FLASH, []).append((kind, text))


def show_flash() -> None:
    icons = {
        "ok": ":material/check_circle:",
        "info": ":material/info:",
        "error": ":material/error:",
    }
    for kind, text in st.session_state.pop(_FLASH, []):
        dur = "long" if kind == "error" else "short"
        st.toast(text, icon=icons[kind], duration=dur)
