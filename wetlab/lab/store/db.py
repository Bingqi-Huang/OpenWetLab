"""SQLite connection setup, island path resolution, and numbered SQL migration runner."""

from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import uuid

from . import island

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
VALID_MODES = ("virtual", "real")


def _validate_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid island mode {mode!r}; expected 'virtual' or 'real'")
    return mode


def resolve_mode(mode: str | None = None) -> str:
    """Resolve island mode from explicit `mode`, `WETLAB_MODE`, or default `'virtual'`."""
    chosen = mode if mode is not None else (os.environ.get("WETLAB_MODE") or "virtual")
    return _validate_mode(chosen)


def island_dir(mode: str | None = None, *, base_dir: str | Path = Path("runs")) -> Path:
    """Return the per-mode runtime directory (`<base_dir>/virtual` or `<base_dir>/real`)."""
    return Path(base_dir) / resolve_mode(mode)


def default_db_path(mode: str | None = None, *, base_dir: str | Path = Path("runs")) -> Path:
    """Return the default SQLite path (`<base_dir>/<mode>/wetlab.sqlite`)."""
    return island_dir(mode, base_dir=base_dir) / "wetlab.sqlite"


@contextmanager
def _init_lock(target: str) -> Iterator[None]:
    if target == ":memory:":
        yield
        return
    with open(f"{target}.lock", "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _migration_files(migrations_dir: Path) -> list[tuple[int, Path]]:
    items = [
        (int(sql_path.stem.split("_", 1)[0]), sql_path)
        for sql_path in migrations_dir.glob("*.sql")
    ]
    items.sort(key=lambda item: item[0])
    return items


def _split_sql(script: str) -> list[str]:
    stmts: list[str] = []
    start = 0
    idx = 0
    while True:
        semi = script.find(";", idx)
        if semi == -1:
            tail = script[start:].strip()
            if tail:
                stmts.append(tail)
            return stmts
        candidate = script[start : semi + 1]
        if sqlite3.complete_statement(candidate):
            stmts.append(candidate)
            start = semi + 1
        idx = semi + 1


def _apply_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    files = _migration_files(migrations_dir)
    if not files:
        return
    if conn.execute("PRAGMA user_version").fetchone()[0] >= files[-1][0]:
        return

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current_version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        for version, sql_path in files:
            if version > current_version:
                for stmt in _split_sql(sql_path.read_text(encoding="utf-8")):
                    conn.execute(stmt)
                conn.execute(f"PRAGMA user_version = {version}")
                current_version = version


def _ensure_identity(conn: sqlite3.Connection, mode: str | None) -> None:
    existing_mode = island.get_island_mode(conn)
    if mode is not None and existing_mode is not None and existing_mode != mode:
        raise ValueError(
            f"database belongs to {existing_mode!r} island, cannot open in {mode!r} mode"
        )
    existing_id = island.get_meta(conn, "db_id")
    if existing_id is not None and (mode is None or existing_mode == mode):
        return
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if island.get_meta(conn, "db_id") is None:
            island.set_meta(conn, "db_id", uuid.uuid4().hex)
        if mode is not None:
            cur_mode = island.get_island_mode(conn)
            if cur_mode is None:
                island.set_meta(conn, "island_mode", mode)
            elif cur_mode != mode:
                raise ValueError(
                    f"database belongs to {cur_mode!r} island, cannot open in {mode!r} mode"
                )


def open_db(
    path: str | Path = ":memory:",
    *,
    mode: str | None = None,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> sqlite3.Connection:
    """Open a SQLite database, configure pragmas, migrate, and verify `island_mode`."""
    if mode is not None:
        _validate_mode(mode)
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)

    with _init_lock(target):
        conn = sqlite3.connect(target, isolation_level="IMMEDIATE")
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            _apply_migrations(conn, Path(migrations_dir))
            _ensure_identity(conn, mode)
        except Exception:
            conn.close()
            raise
        return conn
