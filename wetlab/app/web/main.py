"""Streamlit entry point (`streamlit run wetlab/app/web/main.py` from repo root)."""

import streamlit as st

from wetlab.app.web import ISLAND_PAGE, MISSIONS_PAGE, header, ui

st.set_page_config(page_title="WetLab 实验岛", page_icon=":material/biotech:", layout="wide")
ui.load_css()

island_page = st.Page(ISLAND_PAGE, title="实验岛", icon=":material/biotech:", default=True)
missions_page = st.Page(
    MISSIONS_PAGE,
    title="任务",
    icon=":material/assignment:",
    url_path="missions",
)
page = st.navigation([island_page, missions_page], position="top")

header.render()
if page.url_path != island_page.url_path:
    header.freeze_notice(detailed=False)
page.run()
