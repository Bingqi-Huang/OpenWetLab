"""Tests for `agents.sqlite` `db_id` binding and `--mode` path isolation (`04` v0.6 #3)."""

from pathlib import Path

import pytest

from wetlab.agents.worker import AgentWorker
from wetlab.lab.store import island, open_db


def test_agents_sqlite_db_id_binding_and_mode_directory_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.6 #3: mode resolves `runs/<mode>/`, records `db_id` in `agents.sqlite`, and rejects
    missing or recreated `wetlab.sqlite` with a different `db_id`.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WETLAB_MODE", raising=False)

    w_virt = AgentWorker(mode="virtual")
    w_real = AgentWorker(mode="real")
    try:
        assert w_virt.db_path == Path("runs/virtual/wetlab.sqlite")
        assert w_real.db_path == Path("runs/real/wetlab.sqlite")
        assert (tmp_path / "runs/virtual/agents.sqlite").is_file()
        assert (tmp_path / "runs/real/agents.sqlite").is_file()

        with open_db(w_virt.db_path) as conn:
            virt_db_id = island.get_db_id(conn)
        meta_row = w_virt._ckpt_conn.execute(
            "SELECT value FROM meta WHERE key = 'db_id'"
        ).fetchone()
        assert meta_row is not None and meta_row[0] == virt_db_id
    finally:
        w_virt.close()
        w_real.close()

    w_ok = AgentWorker(mode="virtual")
    w_ok.close()

    with pytest.raises(ValueError, match="island"):
        AgentWorker(db_path=tmp_path / "runs/virtual/wetlab.sqlite", mode="real")

    for p in (tmp_path / "runs/virtual").glob("wetlab.sqlite*"):
        p.unlink()
    with pytest.raises(RuntimeError, match="wetlab.sqlite"):
        AgentWorker(mode="virtual")

    fresh_conn = open_db(tmp_path / "runs/virtual/wetlab.sqlite", mode="virtual")
    fresh_conn.close()
    with pytest.raises(RuntimeError, match="db_id mismatch"):
        AgentWorker(mode="virtual")
