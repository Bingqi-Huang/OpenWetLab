"""Step 6 CLI workflow tests and Step 7 `wetlab doctor` health check tests."""

import json
from pathlib import Path

import pytest

from wetlab.app.cli import main, run_doctor
from wetlab.lab.materials import Materials
from wetlab.lab.mqtt import MqttBackend
from wetlab.lab.service.reconcile import sync_device_state
from wetlab.lab.store import island, open_db, runs

REPO_ROOT = Path(__file__).resolve().parents[2]
SOP_FILE = str(REPO_ROOT / "sops" / "pcr_37c_30s.json")
CAPTURE_FILE = REPO_ROOT / "captures" / "lab_v1_20261117_173423.jsonl"


def _cli(db: Path, runs_dir: Path, *args: str) -> int:
    return main(["--db", str(db), "--runs-dir", str(runs_dir), *args])


def test_cli_full_workflow_labware_rehearse_submit_approve_pause_resume_status_and_export(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Step 6 row 18: Walk through the complete CLI lifecycle on a virtual island DB."""
    db = tmp_path / "wetlab.sqlite"
    runs_dir = tmp_path / "runs"

    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    assert _cli(db, runs_dir, "labware", "place", "LW-001", "96_plate", "pcr", "--key", "k1") == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    capsys.readouterr()

    assert _cli(db, runs_dir, "status", "--json") == 0
    empty_status = json.loads(capsys.readouterr().out)
    assert empty_status["snapshot"]["labware"] == {
        "LW-001": {"position": "pcr", "type": "96_plate", "contents": []}
    }

    with open_db(db) as conn:
        Materials(conn).set_contents(
            "LW-001",
            [{"name": "SynCom-1", "wells": ["A1"], "volume_ul": 25.0, "note": "待扩增"}],
        )

    assert _cli(db, runs_dir, "rehearse", SOP_FILE) == 0
    reh_out = capsys.readouterr().out
    assert "执行前现场应该是这样" in reh_out
    assert "37℃ 30 秒" in reh_out
    assert "SynCom-1" in reh_out

    mid = "M-20261118-099"
    assert _cli(db, runs_dir, "submit", SOP_FILE, "--mission-id", mid, "--key", "s1") == 0
    sub_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    fp = sub_json["fingerprint"]
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    capsys.readouterr()

    assert _cli(db, runs_dir, "status", "--json") == 0
    st_json = json.loads(capsys.readouterr().out)
    assert st_json["mode"] == "virtual"
    assert st_json["snapshot"]["labware"] == {
        "LW-001": {
            "position": "pcr",
            "type": "96_plate",
            "contents": [
                {"name": "SynCom-1", "wells": ["A1"], "volume_ul": 25.0, "note": "待扩增"}
            ],
        }
    }
    assert st_json["protocols"][0]["id"] == "E-001-v1"
    assert st_json["protocols"][0]["status"] == "submitted"
    assert "SynCom-1" in st_json["protocols"][0]["text_md"]
    assert _cli(db, runs_dir, "status") == 0
    assert "E-001-v1: status=submitted" in capsys.readouterr().out

    assert _cli(db, runs_dir, "approve", "E-001-v1", "--by", "钱研究员", "--fingerprint", fp) == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--rounds", "3") == 0
    with open_db(db) as conn:
        proto_running = runs.get_protocol(conn, "E-001-v1")
        assert proto_running is not None
        assert proto_running["status"] == "running"

    assert _cli(db, runs_dir, "pause", "--protocol-id", "E-001-v1") == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--rounds", "2") == 0
    with open_db(db) as conn:
        proto_paused = runs.get_protocol(conn, "E-001-v1")
        assert proto_paused is not None
        assert proto_paused["status"] == "paused"

    assert _cli(db, runs_dir, "resume", "--protocol-id", "E-001-v1") == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--rounds", "70") == 0
    with open_db(db) as conn:
        proto_done = runs.get_protocol(conn, "E-001-v1")
        assert proto_done is not None
        assert proto_done["status"] == "done"

    export_file = tmp_path / "catalog.json"
    assert _cli(db, runs_dir, "tools", "export", "--output", str(export_file)) == 0
    cat = json.loads(export_file.read_text(encoding="utf-8"))
    assert len(cat["skills"]) == 12


def test_cli_reject_stop_unfreeze_abort_and_labware_correct_remove(tmp_path: Path) -> None:
    """Exercise remaining CLI commands (`reject`, `stop`, `unfreeze`, `abort`, `labware`)."""
    db = tmp_path / "wetlab2.sqlite"
    runs_dir = tmp_path / "runs2"
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    assert _cli(db, runs_dir, "labware", "place", "LW-001", "96_plate", "pcr") == 0
    assert _cli(db, runs_dir, "submit", SOP_FILE, "--mission-id", "M-20261118-100") == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0

    assert _cli(db, runs_dir, "reject", "E-001-v1", "--comment", "请先混匀", "--by", "李老师") == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    with open_db(db) as conn:
        rejected = runs.get_protocol(conn, "E-001-v1")
        assert rejected is not None
        assert rejected["status"] == "rejected"
        with conn:
            island.set_frozen(conn, reason="测试冻结", at=1800000100.0)

    assert _cli(db, runs_dir, "unfreeze", "--conclusion", "step_done") == 0
    assert _cli(db, runs_dir, "stop") == 0
    assert _cli(db, runs_dir, "abort", "--experiment-id", "E-001") == 0
    assert _cli(db, runs_dir, "labware", "correct", "LW-001", "rack:1-3") == 0
    assert _cli(db, runs_dir, "labware", "remove", "LW-001") == 0
    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    with open_db(db) as conn:
        assert island.get_freeze(conn)["frozen"] is False
        exp = runs.get_experiment(conn, "E-001")
        assert exp is not None
        assert exp["status"] == "stopped"


def test_wetlab_doctor_healthy_and_all_five_failure_categories(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Step 7 `wetlab doctor`: healthy island and all 5 failure categories."""
    db = tmp_path / "doctor.sqlite"
    runs_dir = tmp_path / "runs"

    assert _cli(db, runs_dir, "serve", "--mode", "virtual", "--once") == 0
    capsys.readouterr()
    assert _cli(db, runs_dir, "doctor") == 0
    doc_ok = json.loads(capsys.readouterr().out)
    assert doc_ok["ok"] is True
    assert all(doc_ok["checks"].values())

    clock = [1_794_908_050.0]
    mqtt = MqttBackend(
        raw_dir=tmp_path / "raw",
        now_fn=lambda: clock[0],
        sleep_fn=lambda s: None,
    )
    with CAPTURE_FILE.open(encoding="utf-8") as fh:
        for idx, line in enumerate(fh):
            if idx >= 80:
                break
            rec = json.loads(line)
            mqtt.receive(rec["topic"], rec["payload"], received_at=clock[0])

    conn = open_db(db)
    try:
        rep_replay = run_doctor(conn, now=clock[0], backend=mqtt)
        assert rep_replay["ok"] is True

        with conn:
            island.upsert_device_state(
                conn,
                device="_gateway",
                props={"online": False, "busy_by_others": False},
                updated_at=clock[0],
            )
        assert _cli(db, runs_dir, "doctor") == 1
        r1 = json.loads(capsys.readouterr().out)
        assert r1["checks"]["gateway_online"] is False

        with conn:
            island.upsert_device_state(
                conn,
                device="_gateway",
                props={"online": True, "busy_by_others": False},
                updated_at=clock[0] + 25.0,
            )
        r2 = run_doctor(conn, now=clock[0] + 25.0)
        assert r2["ok"] is False
        assert r2["checks"]["devices_fresh"] is False

        with conn:
            sync_device_state(conn, mqtt.state(), now=clock[0])
            pcr_row = island.get_device_state(conn, "pcr")
            assert pcr_row is not None
            island.upsert_device_state(
                conn,
                device="pcr",
                props=pcr_row["props"],
                updated_at=clock[0],
                error="串口 COM12 断开",
            )
        r3 = run_doctor(conn, now=clock[0])
        assert r3["ok"] is False
        assert r3["checks"]["no_device_errors"] is False

        with conn:
            sync_device_state(conn, mqtt.state(), now=clock[0])
            sh_row = island.get_device_state(conn, "shaker")
            assert sh_row is not None
            bad_sh = {**sh_row["props"], "ErrorCode": 5}
            island.upsert_device_state(conn, device="shaker", props=bad_sh, updated_at=clock[0])
        r3b = run_doctor(conn, now=clock[0])
        assert r3b["ok"] is False
        assert r3b["checks"]["no_device_errors"] is False

        with conn:
            sync_device_state(conn, mqtt.state(), now=clock[0])
            island.upsert_device_state(
                conn,
                device="_gateway",
                props={"online": True, "busy_by_others": True},
                updated_at=clock[0],
            )
        r4 = run_doctor(conn, now=clock[0])
        assert r4["ok"] is False
        assert r4["checks"]["not_busy_by_others"] is False

        with conn:
            island.upsert_device_state(
                conn,
                device="_gateway",
                props={"online": True, "busy_by_others": False},
                updated_at=clock[0],
            )
        conn.execute("PRAGMA query_only = ON")
        r5 = run_doctor(conn, now=clock[0])
        assert r5["ok"] is False
        assert r5["checks"]["db_writable"] is False
    finally:
        conn.close()
