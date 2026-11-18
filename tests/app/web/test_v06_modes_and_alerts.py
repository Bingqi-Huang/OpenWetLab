"""v0.6 Web UI tests: mode banner (#3), `stop_failed` (#1), outcomes (#10), skills (#2)."""

import json
from pathlib import Path
import sys

import pytest
from streamlit.testing.v1 import AppTest

from wetlab.app.web import MAIN_PATH, MISSIONS_PAGE
from wetlab.lab.service.main import Service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, runs

SOPS_DIR = Path(__file__).resolve().parents[3] / "sops"


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["main.py"])
    for var in ("WETLAB_MODE", "WETLAB_DB", "WETLAB_DB_PATH", "WETLAB_RUNS_DIR"):
        monkeypatch.delenv(var, raising=False)


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


def test_web_mode_env_isolates_dirs_renders_top_banner_and_rejects_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3: Default vs `WETLAB_MODE=real` isolates `runs/<mode>/` and renders top mode banner."""
    _clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)

    # 1. Default (`WETLAB_MODE` unset) -> `runs/virtual/wetlab.sqlite` & virtual top banner
    at_virt = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
    assert not at_virt.exception
    assert "当前连接：虚拟实验岛（virtual）" in _html(at_virt)
    assert "当前连接：真实实验岛（real）" not in _html(at_virt)
    v_db = tmp_path / "runs" / "virtual" / "wetlab.sqlite"
    r_db = tmp_path / "runs" / "real" / "wetlab.sqlite"
    assert v_db.is_file()
    assert not r_db.exists()

    at_virt.switch_page(str(MISSIONS_PAGE)).run()
    assert "当前连接：虚拟实验岛（virtual）" in _html(at_virt)

    # 2. `WETLAB_MODE=real` -> `runs/real/wetlab.sqlite` & real top banner on both pages
    monkeypatch.setenv("WETLAB_MODE", "real")
    at_real = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
    assert not at_real.exception
    assert "当前连接：真实实验岛（real）" in _html(at_real)
    assert "当前连接：虚拟实验岛（virtual）" not in _html(at_real)
    assert r_db.is_file()
    with open_db(r_db) as conn:
        assert island.get_island_mode(conn) == "real"

    at_real.switch_page(str(MISSIONS_PAGE)).run()
    assert "当前连接：真实实验岛（real）" in _html(at_real)

    # 3. `--mode real` via `sys.argv` (e.g. `streamlit run main.py -- --mode real`) without env var
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["main.py", "--mode", "real"])
    at_argv = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
    assert not at_argv.exception
    assert "当前连接：真实实验岛（real）" in _html(at_argv)

    # 4. `WETLAB_DB_PATH` pointing to a virtual DB while `--mode real` fails fast
    monkeypatch.setenv("WETLAB_DB_PATH", str(v_db))
    at_mismatch = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
    assert len(at_mismatch.exception) == 1
    assert "virtual" in at_mismatch.exception[0].value


def test_stop_failed_renders_prominent_error_banner_across_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1: When `stop_all` fails to stop a device, `st.error` prominently warns on-site operator."""
    _clear_env(monkeypatch)
    db_file = tmp_path / "stop_fail.sqlite"
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("WETLAB_DB_PATH", str(db_file))
    monkeypatch.setenv("WETLAB_RUNS_DIR", str(runs_dir))

    conn = open_db(db_file, mode="virtual")
    sim = SimBackend(
        Scenario(
            labware={"LW-001": "pcr"},
            faults=[Fault(device="pcr", action="StopRun", nth=1, kind="fail")],
        )
    )
    svc = Service(sim, conn, runs_dir=runs_dir, on_labware_change=sim.sync_human_labware)
    svc.startup()
    try:
        sop = json.loads((SOPS_DIR / "pcr_37c_30s.json").read_text("utf-8"))
        with conn:
            inbox.enqueue_command(
                conn,
                key="p1",
                at=sim.now(),
                who="web",
                kind="place_labware",
                payload={"labware_id": "LW-001", "type": "96孔板", "position": "pcr"},
            )
            inbox.enqueue_command(
                conn,
                key="m1",
                at=sim.now(),
                who="web",
                kind="new_mission",
                payload={"mission_id": "M-STOP", "title": "停止告警测试", "input_text": "PCR 37℃"},
            )
            inbox.enqueue_command(
                conn,
                key="s1",
                at=sim.now(),
                who="operator",
                kind="submit_protocol",
                payload={"mission_id": "M-STOP", "protocol": sop},
            )
        svc.tick()
        sub = runs.list_protocols(conn, status="submitted")[0]
        with conn:
            inbox.enqueue_command(
                conn,
                key="a1",
                at=sim.now(),
                who="周老师",
                kind="approve",
                payload={
                    "protocol_id": sub["id"],
                    "decided_by": "周老师",
                    "fingerprint": sub["fingerprint"],
                },
            )
        svc.tick()
        svc.step()
        svc.step()

        # Researcher clicks `全部停止` -> `确认全部停止` while PCR is Running, and `StopRun` fails
        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20)
        at.switch_page(str(MISSIONS_PAGE)).run()
        at.segmented_control(key="view_M-STOP").set_value("执行").run()
        at.button(key=f"stop_{sub['id']}").click().run()
        svc.tick()

        at.run()
        assert not at.exception
        errors = [e.value for e in at.error]
        assert any(
            "pcr" in e
            and "PCR仪" in e
            and "没有停下来！请立即到现场处理（运动中请直接按物理急停按钮）。" in e
            for e in errors
        )

        # Island home page also shows the same prominent `st.error` banner in a fresh session
        at_home = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        home_errors = [e.value for e in at_home.error]
        assert any(
            "PCR仪（pcr）" in e
            and "没有停下来！请立即到现场处理（运动中请直接按物理急停按钮）。" in e
            for e in home_errors
        )

        # Subsequent successful `stop_all` at the SAME virtual clock timestamp clears `stop_failed`
        sim.world.device("pcr").props["PcrState"] = "Idle"
        sim.world.device("pcr").props["HeaterLidEnabled"] = False
        with conn:
            inbox.enqueue_command(
                conn,
                key="stop:retry",
                at=sim.now(),
                who="web",
                kind="stop_all",
            )
        svc.tick()
        at_cleared = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not any("没有停下来" in e.value for e in at_cleared.error)
    finally:
        conn.close()


def test_human_command_failure_is_visible_and_closed_skills_are_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#10 & #2: Human command failures show in `st.error`; `closed_skills` show in UI."""
    _clear_env(monkeypatch)
    db_file = tmp_path / "cmd_outcome.sqlite"
    monkeypatch.setenv("WETLAB_DB_PATH", str(db_file))

    conn = open_db(db_file, mode="real")
    try:
        with conn:
            island.set_closed_skills(conn, ["pcr_start_program", "transfer"])
            inbox.enqueue_command(
                conn,
                key="human:bad:1",
                at=1_800_000_010.0,
                who="web",
                kind="new_mission",
                payload={"mission_id": "M-DUP"},
            )
            cmd = inbox.get_command_by_key(conn, "human:bad:1")
            assert cmd is not None
            inbox.mark_command_handled(
                conn,
                cmd["id"],
                handled_at=1_800_000_011.0,
                outcome=json.dumps(
                    {"ok": False, "error": "任务编号 M-DUP 已存在"},
                    ensure_ascii=False,
                ),
            )
            # Newer failed Agent command must NOT overwrite/hide or masquerade as human outcome
            inbox.enqueue_command(
                conn,
                key="agent:bad:1",
                at=1_800_000_020.0,
                who="coordinator",
                kind="create_experiment",
                payload={"mission_id": "M-DUP"},
            )
            ag_cmd = inbox.get_command_by_key(conn, "agent:bad:1")
            assert ag_cmd is not None
            inbox.mark_command_handled(
                conn,
                ag_cmd["id"],
                handled_at=1_800_000_021.0,
                outcome=json.dumps({"ok": False, "error": "task must be dict"}, ensure_ascii=False),
            )

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        assert "当前连接：真实实验岛（real）" in _html(at)
        assert any("任务编号 M-DUP 已存在" in e.value for e in at.error)
        assert not any("task must be dict" in e.value for e in at.error)
        assert any(
            "pcr_start_program" in i.value and "transfer" in i.value
            for i in at.info
        )

        # New mission view also shows closed skills
        at.switch_page(str(MISSIONS_PAGE)).run()
        assert any(
            "pcr_start_program" in i.value and "transfer" in i.value
            for i in at.info
        )
    finally:
        conn.close()
