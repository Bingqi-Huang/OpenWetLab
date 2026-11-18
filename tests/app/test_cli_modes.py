"""v0.6 #3 CLI tests: `--mode` / `WETLAB_MODE` isolation and mode mismatch fail-fast."""

import json
from pathlib import Path

import pytest

from wetlab.app.cli import build_parser, main
from wetlab.lab.mqtt import MqttBackend
from wetlab.lab.store import inbox, island, open_db, runs

REPO_ROOT = Path(__file__).resolve().parents[2]
SOP_FILE = str(REPO_ROOT / "sops" / "pcr_37c_30s.json")


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("WETLAB_MODE", "WETLAB_DB", "WETLAB_DB_PATH", "WETLAB_RUNS_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_cli_mode_and_env_isolate_virtual_and_real_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting `--db`/`--runs-dir` resolves `runs/<mode>/wetlab.sqlite` by mode."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    # 1. Default (`WETLAB_MODE` unset) resolves to `runs/virtual/wetlab.sqlite`
    assert main(["serve", "--once"]) == 0
    v_db = tmp_path / "runs" / "virtual" / "wetlab.sqlite"
    r_db = tmp_path / "runs" / "real" / "wetlab.sqlite"
    assert v_db.is_file()
    assert not r_db.exists()
    with open_db(v_db) as conn:
        assert island.get_island_mode(conn) == "virtual"

    assert main(["labware", "place", "LW-VIRT", "96_plate", "pcr"]) == 0
    assert main(["serve", "--once"]) == 0
    capsys.readouterr()

    # 2. `WETLAB_MODE=real` isolates commands into `runs/real/wetlab.sqlite` (zero MQTT connection)
    monkeypatch.setenv("WETLAB_MODE", "real")
    assert main(["labware", "place", "LW-REAL", "96_plate", "shaker"]) == 0
    capsys.readouterr()
    assert r_db.is_file()
    with open_db(r_db) as conn:
        assert island.get_island_mode(conn) == "real"
        cmds = inbox.list_unhandled_commands(conn)
        assert [c["payload"]["labware_id"] for c in cmds] == ["LW-REAL"]

    # 3. Explicit `--mode` overrides `WETLAB_MODE`
    assert main(["status", "--json", "--mode", "virtual"]) == 0
    v_status = json.loads(capsys.readouterr().out)
    assert v_status["mode"] == "virtual"
    assert v_status["snapshot"]["labware"] == {
        "LW-VIRT": {"position": "pcr", "type": "96_plate", "contents": []}
    }

    assert main(["status", "--json", "--mode", "real"]) == 0
    r_status = json.loads(capsys.readouterr().out)
    assert r_status["mode"] == "real"
    assert r_status["snapshot"]["labware"] == {}


def test_cli_mode_mismatch_fails_fast_without_connecting_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening a virtual DB in `real` mode raises immediately without starting MQTT."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    v_db = tmp_path / "virtual_only.sqlite"
    assert main(["--db", str(v_db), "serve", "--mode", "virtual", "--once"]) == 0

    def _never_start(self: MqttBackend) -> None:
        raise AssertionError("MqttBackend.start must never be called on mode mismatch")

    monkeypatch.setattr(MqttBackend, "start", _never_start)

    with pytest.raises(ValueError, match="virtual"):
        main(["--db", str(v_db), "serve", "--mode", "real", "--once"])

    with pytest.raises(ValueError, match="virtual"):
        main(["--db", str(v_db), "status", "--mode", "real"])

    with pytest.raises(ValueError, match="virtual"):
        main(["--db", str(v_db), "submit", SOP_FILE, "--mode", "real"])

    monkeypatch.setenv("WETLAB_MODE", "real")
    with pytest.raises(ValueError, match="virtual"):
        main(["--db", str(v_db), "doctor"])

    monkeypatch.setenv("WETLAB_MODE", "bogus")
    with pytest.raises(ValueError, match="invalid island mode"):
        main(["tools", "export"])


def test_every_cli_subcommand_accepts_mode_flag_and_mission_inherits_db_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every subcommand parses `--mode {virtual,real}`, and `submit` omits caller mode."""
    _clear_env(monkeypatch)
    parser = build_parser()
    cases = [
        ["serve", "--mode", "real", "--once"],
        ["agents", "--mode", "virtual", "--once"],
        ["submit", SOP_FILE, "--mode", "virtual"],
        ["rehearse", SOP_FILE, "--mode", "virtual"],
        ["approve", "E-001-v1", "--by", "张老师", "--fingerprint", "abc", "--mode", "real"],
        ["reject", "E-001-v1", "--comment", "改温度", "--mode", "virtual"],
        ["pause", "--mode", "virtual"],
        ["resume", "--mode", "virtual"],
        ["stop", "--mode", "real"],
        ["unfreeze", "--conclusion", "step_done", "--mode", "real"],
        ["abort", "--experiment-id", "E-001", "--mode", "virtual"],
        ["labware", "place", "LW-1", "96_plate", "pcr", "--mode", "virtual"],
        ["labware", "remove", "LW-1", "--mode", "real"],
        ["labware", "correct", "LW-1", "shaker", "--mode", "virtual"],
        ["status", "--mode", "real"],
        ["doctor", "--mode", "real"],
        ["tools", "export", "--mode", "virtual"],
    ]
    for argv in cases:
        parsed = parser.parse_args(argv)
        assert parsed.mode in ("virtual", "real")

    monkeypatch.chdir(tmp_path)
    assert main(["serve", "--mode", "virtual", "--once"]) == 0
    assert main(["labware", "place", "LW-001", "96_plate", "pcr", "--mode", "virtual"]) == 0
    assert main(["submit", SOP_FILE, "--mission-id", "M-MODE-1", "--mode", "virtual"]) == 0
    v_db = tmp_path / "runs" / "virtual" / "wetlab.sqlite"
    with open_db(v_db) as conn:
        new_m_cmd = inbox.list_unhandled_commands(conn)[1]
        assert new_m_cmd["kind"] == "new_mission"
        assert "mode" not in new_m_cmd["payload"]
    assert main(["serve", "--mode", "virtual", "--once"]) == 0
    with open_db(v_db) as conn:
        mission = runs.get_mission(conn, "M-MODE-1")
        assert mission is not None
        assert mission["mode"] == "virtual"
