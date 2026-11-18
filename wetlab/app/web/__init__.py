"""Streamlit web interface for non-coding biology researchers (docs/spec/04 section 8)."""

from pathlib import Path

MAIN_PATH = Path(__file__).resolve().parent / "main.py"
VIEWS_DIR = MAIN_PATH.parent / "views"
ISLAND_PAGE = VIEWS_DIR / "island.py"
MISSIONS_PAGE = VIEWS_DIR / "missions.py"

__all__ = ["ISLAND_PAGE", "MAIN_PATH", "MISSIONS_PAGE", "VIEWS_DIR"]
