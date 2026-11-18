"""Small HTML building blocks styled by style.css. Every value is escaped before markup."""

import html
from pathlib import Path
from typing import Any

import streamlit as st

_CSS = Path(__file__).with_name("style.css")


def load_css() -> None:
    st.html(_CSS)


def esc(value: Any) -> str:
    return html.escape(str(value))


def pill(text: str, tone: str, *, live: bool = False) -> str:
    """Rounded status label; `tone` is green, amber, red, blue, gray, or primary."""
    live_cls = " live" if live else ""
    return f'<span class="wl-pill {tone}{live_cls}"><i></i>{esc(text)}</span>'


def page_head(title: str, sub: str) -> None:
    markup = f'<div class="wl wl-head"><div class="t">{esc(title)}</div>'
    st.html(f'{markup}<div class="s">{esc(sub)}</div></div>')


def section(title: str, sub: str = "") -> None:
    markup = f'<div class="wl wl-sec"><span class="t">{esc(title)}</span>'
    st.html(f'{markup}<span class="s">{esc(sub)}</span></div>')


def empty(title: str, sub: str = "") -> None:
    st.html(f'<div class="wl wl-empty"><b>{esc(title)}</b>{esc(sub)}</div>')


def sys_line(text: str, at: str) -> None:
    st.html(f'<div class="wl wl-sys"><span>{esc(text)} · {esc(at)}</span></div>')


def kv(rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in rows)
    return f'<dl class="wl-kv">{body}</dl>'


def table(headers: list[str], rows: list[list[str]], *, wrap: int) -> None:
    """Plain table; column `wrap` takes spare width and wraps, others stay one line."""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    tr_list = []
    for row in rows:
        cells = "".join(
            f'<td{" class=wrap" if i == wrap else ""}>{esc(c)}</td>'
            for i, c in enumerate(row)
        )
        tr_list.append(f"<tr>{cells}</tr>")
    body = "".join(tr_list)
    st.html(
        '<div class="wl wl-tablewrap"><table class="wl-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )
