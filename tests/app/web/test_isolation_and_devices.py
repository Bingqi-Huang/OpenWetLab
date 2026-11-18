"""Web UI tests for service refusal feedback, commands-only writes, and device telemetry cards."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from wetlab.app.web import MAIN_PATH, MISSIONS_PAGE
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, island, open_db, records


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


def test_service_refusal_is_reported_back_to_the_person(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "refuse.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    svc = create_virtual_service(db_file, runs_dir=tmp_path / "runs")
    svc.startup()
    try:
        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        at.text_input(key="place_id").set_value("LW-007")
        at.selectbox(key="place_pos").set_value("pcr")
        at.button(key="place_submit").click().run()
        svc.tick()
        at.run()
        at.text_input(key="place_id").set_value("LW-007")
        at.selectbox(key="place_pos").set_value("shaker")
        at.button(key="place_submit").click().run()
        svc.tick()
        at.run()
        assert any("实验岛服务没有执行" in t.value and "LW-007" in t.value for t in at.toast)
        assert any("最近一条命令未执行" in e.value and "LW-007" in e.value for e in at.error)
        lw_keep = records.get_labware(svc.conn, "LW-007")
        assert lw_keep is not None
        assert lw_keep["position"] == "pcr"
    finally:
        svc.conn.close()


def test_web_writes_exclusively_to_commands_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "isolated.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    conn = open_db(db_file, mode="virtual")
    try:
        sql = (
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'commands'"
        )
        tables = [r["name"] for r in conn.execute(sql)]
        before = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        at.text_input(key="place_id").set_value("LW-009")
        at.button(key="place_submit").click().run()
        at.switch_page(str(MISSIONS_PAGE)).run()
        at.text_input(key="nm_title").set_value("隔离测试")
        at.text_area(key="nm_text").set_value("测试只写 commands 表")
        at.button(key="nm_submit").click().run()
        assert not at.exception

        after = {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")] for t in tables}
        assert after == before
        kinds = [c["kind"] for c in inbox.list_unhandled_commands(conn)]
        assert kinds == ["place_labware", "new_mission"]
    finally:
        conn.close()


def test_device_cards_show_errors_stale_state_and_a_silent_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_file = tmp_path / "devices_stale.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    conn = open_db(db_file, mode="virtual")
    try:
        with conn:
            island.upsert_device_state(
                conn,
                device="pcr",
                error="COM9 串口打开失败",
                updated_at=1_800_000_000.0,
                props={
                    "DoorState": "Closed",
                    "LidMotorState": "Lifted",
                    "PcrState": "Idle",
                    "FaultText": "无故障",
                },
            )
            island.upsert_device_state(
                conn,
                device="shaker",
                error=None,
                updated_at=1_800_000_025.0,
                props={"ShakeState": 1, "ActualSpeed": 0, "ErrorCode": 5, "WorkingStatus": 0},
            )
            island.upsert_device_state(
                conn,
                device="_gateway",
                props={"online": True, "busy_by_others": False},
                updated_at=1_800_000_025.0,
            )
        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        page = _html(at)
        assert "网关在线" in page
        assert "设备报错：COM9 串口打开失败" in page
        assert "故障</span>" in page
        assert "状态更新于 25 秒前" in page
        assert "仓门</dt><dd>关闭" in page

        with conn:
            inbox.append_event(
                conn,
                at=1_800_000_055.0,
                kind="message",
                ref_id="M-1",
                payload={"text": "ping"},
            )
        at.run()
        assert "实验岛服务 30 秒前更新" in _html(at)
        assert "网关在线" not in _html(at)
    finally:
        conn.close()
