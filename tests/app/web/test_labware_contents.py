"""AppTest suite for #40: bench position dropdowns, contents display, and contents commands."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from wetlab.app.web import MAIN_PATH
from wetlab.app.web.island_data import read_island
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import open_db, records


def _html(at: AppTest) -> str:
    return "\n".join(h.value for h in at.html)


def test_position_dropdowns_show_slot_codes_and_exclude_non_labware_and_occupied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#40(1): Position dropdowns show slot codes and exclude turntable, arm:home, and occupied."""
    db_file = tmp_path / "positions.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    svc = create_virtual_service(db_file, runs_dir=tmp_path / "runs")
    svc.startup()
    try:
        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        place_opts = list(at.selectbox(key="place_pos").options)
        assert place_opts == ["料架 1-2", "料架 1-3", "PCR仪", "恒温振荡模组"]
        assert "转向台" not in place_opts
        assert "turntable" not in place_opts
        assert "机械臂原位" not in place_opts
        assert "arm:home" not in place_opts

        at.text_input(key="place_id").set_value("LW-001")
        at.selectbox(key="place_pos").set_value("pcr")
        at.button(key="place_submit").click().run()
        svc.tick()
        at.run()
        assert not at.exception

        place_after = list(at.selectbox(key="place_pos").options)
        assert place_after == ["料架 1-2", "料架 1-3", "恒温振荡模组"]
        assert "PCR仪" not in place_after
        assert "转向台" not in place_after
        assert "机械臂原位" not in place_after

        correct_opts = list(at.selectbox(key="correct_pos").options)
        assert correct_opts == ["料架 1-2", "料架 1-3", "恒温振荡模组", "已不在实验岛上"]
        assert "PCR仪" not in correct_opts
        assert "转向台" not in correct_opts
        assert "机械臂原位" not in correct_opts
    finally:
        svc.conn.close()


def test_bench_reads_and_displays_labware_contents_from_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#40(3b): When `labware_contents` has rows, `read_island` and the bench display them."""
    db_file = tmp_path / "bench_contents.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    conn = open_db(db_file, mode="virtual")
    try:
        with conn:
            records.upsert_labware(conn, id="LW-003", type="96孔PCR板", position="rack:1-2")
            conn.execute(
                "INSERT INTO labware_contents (labware_id, n, name, wells, volume_ul, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("LW-003", 1, "SynCom-1", '["A1", "A2"]', 20.0, None),
            )
            conn.execute(
                "INSERT INTO labware_contents (labware_id, n, name, wells, volume_ul, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("LW-003", 2, "PCR预混液", None, 25.0, "冰上保存"),
            )

        island_view = read_island(conn)
        assert island_view["labware"]["LW-003"] == {
            "type": "96孔PCR板",
            "position": "rack:1-2",
            "contents": [
                {"name": "SynCom-1", "wells": ["A1", "A2"], "volume_ul": 20.0, "note": None},
                {"name": "PCR预混液", "wells": None, "volume_ul": 25.0, "note": "冰上保存"},
            ],
        }

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        page_html = _html(at)
        assert "LW-003" in page_html
        assert "料架 1-2" in page_html
        assert "96孔PCR板" in page_html
        assert "SynCom-1（A1, A2 · 20 µL）" in page_html
        assert "PCR预混液（25 µL · 冰上保存）" in page_html
    finally:
        conn.close()
