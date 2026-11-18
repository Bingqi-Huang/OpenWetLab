"""Tests for wetlab.lab.store.db: schema, meta identity, mode isolation, migrations, concurrency."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
import sqlite3
import threading

import pytest

from wetlab.lab.store import island, open_db, records
from wetlab.lab.store.db import (
    DEFAULT_MIGRATIONS_DIR,
    default_db_path,
    island_dir,
    resolve_mode,
)

EXPECTED_COLUMNS: dict[str, list[str]] = {
    "missions": ["id", "title", "input_text", "mode", "status", "created_at", "conclusion"],
    "experiments": ["id", "mission_id", "n", "task_json", "status", "notes"],
    "protocols": [
        "id", "experiment_id", "version", "content_json", "fingerprint", "rehearsal_json",
        "text_md", "status", "submitted_at", "decided_by", "decided_at", "reject_comment",
    ],
    "steps": [
        "protocol_id", "n", "kind", "content_json", "status",
        "started_at", "finished_at", "result_json", "wait_deadline",
    ],
    "operations": [
        "id", "seq", "protocol_id", "step_n", "device", "action",
        "params_json", "status", "result_json", "error_code", "started_at", "elapsed_s",
    ],
    "labware": ["id", "type", "position"],
    "labware_events": ["id", "labware_id", "at", "what", "detail_json", "operation_id", "by"],
    "device_state": ["device", "props_json", "error", "updated_at"],
    "freeze": ["frozen", "reason", "operation_id", "at"],
    "commands": [
        "id", "key", "at", "who", "kind", "target_id", "payload_json", "handled_at", "outcome",
    ],
    "events": ["id", "at", "kind", "ref_id", "payload_json"],
    "cursors": ["name", "last_event_id"],
    "meta": ["key", "value"],
}


def test_schema_defines_all_13_tables_and_exact_columns() -> None:
    conn = open_db(":memory:")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {row["name"] for row in rows}
        assert tables == set(EXPECTED_COLUMNS.keys())
        for table, expected_cols in EXPECTED_COLUMNS.items():
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            assert cols == expected_cols, f"Column mismatch for {table}"
        db_id = island.get_db_id(conn)
        assert len(db_id) == 32
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_resolve_mode_island_dir_and_mode_mismatch_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    assert resolve_mode() == "virtual"
    assert resolve_mode("real") == "real"
    assert island_dir("virtual", base_dir=tmp_path) == tmp_path / "virtual"
    assert default_db_path("real", base_dir=tmp_path) == tmp_path / "real" / "wetlab.sqlite"

    monkeypatch.setenv("WETLAB_MODE", "real")
    assert resolve_mode() == "real"
    assert resolve_mode("virtual") == "virtual"

    monkeypatch.setenv("WETLAB_MODE", "bogus")
    with pytest.raises(ValueError):
        resolve_mode()
    with pytest.raises(ValueError):
        resolve_mode("")

    # Opening virtual DB in real mode must reject without corrupting db_id or island_mode
    v_db = default_db_path("virtual", base_dir=tmp_path)
    conn_v = open_db(v_db, mode="virtual")
    first_id = island.get_db_id(conn_v)
    assert island.get_island_mode(conn_v) == "virtual"
    conn_v.close()

    with pytest.raises(ValueError, match="virtual"):
        open_db(v_db, mode="real")

    conn_v2 = open_db(v_db, mode="virtual")
    try:
        assert island.get_db_id(conn_v2) == first_id
        assert island.get_island_mode(conn_v2) == "virtual"
    finally:
        conn_v2.close()


def test_pragmas_and_v1_to_v2_and_v3_migrations(tmp_path: Path) -> None:
    # Upgrade a v1 database (001_init.sql only) to v2 (002_meta.sql) via open_db
    v1_db = tmp_path / "legacy_v1.sqlite"
    raw = sqlite3.connect(v1_db)
    raw.executescript((DEFAULT_MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8"))
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    upgraded = open_db(v1_db, mode="real")
    try:
        assert upgraded.row_factory is sqlite3.Row
        assert upgraded.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert upgraded.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert upgraded.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 2
        assert island.get_island_mode(upgraded) == "real"
        assert len(island.get_db_id(upgraded)) == 32
    finally:
        upgraded.close()

    # Future 003 migration auto-executes and broken migration rolls back atomically
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    shutil.copy2(DEFAULT_MIGRATIONS_DIR / "001_init.sql", mig_dir / "001_init.sql")
    shutil.copy2(DEFAULT_MIGRATIONS_DIR / "002_meta.sql", mig_dir / "002_meta.sql")
    db_file = tmp_path / "nested" / "wetlab.sqlite"
    c1 = open_db(db_file, migrations_dir=mig_dir)
    with c1:
        records.upsert_labware(c1, id="LW-001", type="pcr_plate_96", position="pcr")
    c1.close()

    (mig_dir / "003_broken.sql").write_text(
        "CREATE TABLE partial_table (x INTEGER);\nSYNTAX ERROR HERE;",
        encoding="utf-8",
    )
    with pytest.raises(sqlite3.OperationalError):
        open_db(db_file, migrations_dir=mig_dir)
    (mig_dir / "003_broken.sql").unlink()

    (mig_dir / "003_wells.sql").write_text(
        "CREATE TABLE wells (labware_id TEXT NOT NULL REFERENCES labware(id), "
        "well TEXT NOT NULL, volume_ul REAL NOT NULL, PRIMARY KEY (labware_id, well));\n"
        "INSERT INTO wells (labware_id, well, volume_ul) VALUES ('LW-001', 'A1', 25.0);\n",
        encoding="utf-8",
    )
    c2 = open_db(db_file, migrations_dir=mig_dir)
    try:
        assert c2.execute("PRAGMA user_version").fetchone()[0] == 3
        well = c2.execute("SELECT * FROM wells WHERE labware_id = 'LW-001'").fetchone()
        assert dict(well) == {"labware_id": "LW-001", "well": "A1", "volume_ul": 25.0}
    finally:
        c2.close()


def test_foreign_keys_commands_uniqueness_and_rollback(tmp_path: Path) -> None:
    conn = open_db(tmp_path / "wetlab.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    "INSERT INTO experiments (id, mission_id, n, task_json, status) "
                    "VALUES ('E-1', 'NOPE', 1, '{}', 'open')"
                )

        with pytest.raises(RuntimeError, match="mid-tx"):
            with conn:
                seq = records.next_operation_seq(conn)
                op_id = f"WL-1000-{seq}"
                records.insert_operation(
                    conn,
                    id=op_id,
                    seq=seq,
                    device="pcr",
                    action="OpenDoor",
                    params={},
                    status="unknown",
                    error_code="TIMEOUT_UNKNOWN",
                    started_at=1.0,
                    elapsed_s=15.0,
                )
                island.set_frozen(conn, reason="TIMEOUT_UNKNOWN", at=16.0, operation_id=op_id)
                raise RuntimeError("mid-tx")

        assert records.list_operations(conn) == []
        assert island.get_freeze(conn)["frozen"] is False
    finally:
        conn.close()


def test_concurrent_connections_and_first_open_stress(tmp_path: Path) -> None:
    db_file = tmp_path / "concurrent.sqlite"
    barrier = threading.Barrier(2)
    iterations = 40

    def service_worker() -> int:
        barrier.wait()
        conn = open_db(db_file, mode="virtual")
        try:
            for i in range(iterations):
                with conn:
                    seq = records.next_operation_seq(conn)
                    records.insert_operation(
                        conn,
                        id=f"WL-1000-{seq}",
                        seq=seq,
                        device="pcr",
                        action="GetPcrStatus",
                        params={"iter": i},
                        status="succeeded",
                        result={"DoorState": "Closed"},
                        started_at=float(i),
                        elapsed_s=0.1,
                    )
                    island.upsert_device_state(
                        conn,
                        device="pcr",
                        props={"DoorState": "Closed", "iter": i},
                        updated_at=float(i),
                    )
            return iterations
        finally:
            conn.close()

    def client_worker() -> int:
        barrier.wait()
        conn = open_db(db_file, mode="virtual")
        try:
            for i in range(iterations):
                with conn:
                    conn.execute(
                        "INSERT INTO commands (key, at, who, kind, target_id, payload_json) "
                        "VALUES (?, ?, 'agent', 'reply', NULL, '{}')",
                        (f"cmd-{i}", float(i)),
                    )
                _ = island.get_all_device_states(conn)
            return iterations
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(service_worker)
        f2 = pool.submit(client_worker)
        assert (f1.result(), f2.result()) == (iterations, iterations)

    for round_idx in range(25):
        f_db = tmp_path / f"first_open_{round_idx}.sqlite"
        b2 = threading.Barrier(2)

        def open_and_check() -> tuple[int, str]:
            b2.wait()
            c = open_db(f_db, mode="virtual")
            try:
                ver = int(c.execute("PRAGMA user_version").fetchone()[0])
                return ver, island.get_db_id(c)
            finally:
                c.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(open_and_check)
            f2 = pool.submit(open_and_check)
            r1 = f1.result()
            r2 = f2.result()
            assert r1[0] == 2 and r2[0] == 2
            assert r1[1] == r2[1]
