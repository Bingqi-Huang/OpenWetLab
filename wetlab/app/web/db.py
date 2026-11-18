"""Where the web process finds the shared database, island folder, mode, and clock."""

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import sys
import time

from wetlab.lab.store import db as store_db, open_db


def _argv_mode() -> str | None:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--mode", default=None)
    parsed, _ = parser.parse_known_args(sys.argv[1:])
    return str(parsed.mode) if parsed.mode is not None else None


def _explicit_mode() -> str | None:
    return _argv_mode() or os.environ.get("WETLAB_MODE")


def configured_mode() -> str:
    """Island mode from `--mode`, `WETLAB_MODE`, or default `'virtual'`."""
    return store_db.resolve_mode(_explicit_mode())


def _db_override() -> str | None:
    return os.environ.get("WETLAB_DB_PATH") or os.environ.get("WETLAB_DB")


def db_path() -> Path:
    """SQLite file shared with service and Agent (`WETLAB_DB_PATH`/`WETLAB_DB` or mode default)."""
    override = _db_override()
    if override:
        return Path(override)
    return store_db.default_db_path(configured_mode())


def runs_dir() -> Path:
    """Island folder holding per-mission `plan.md` and `report.md`."""
    override = os.environ.get("WETLAB_RUNS_DIR")
    if override:
        return Path(override)
    db_override = _db_override()
    if db_override:
        return Path(db_override).parent
    return store_db.island_dir(configured_mode())


@contextmanager
def open_web_db() -> Iterator[sqlite3.Connection]:
    """Open one connection for one page run; enforce `--mode` / `WETLAB_MODE` when configured."""
    explicit_mode = _explicit_mode()
    if explicit_mode:
        mode: str | None = store_db.resolve_mode(explicit_mode)
    elif _db_override():
        mode = None
    else:
        mode = store_db.resolve_mode(None)
    conn = open_db(db_path(), mode=mode)
    try:
        yield conn
    finally:
        conn.close()


def island_now(last_timestamp: float) -> float:
    """Island clock seen by the web: wall time, or latest DB time when virtual runs ahead."""
    return max(time.time(), last_timestamp)
