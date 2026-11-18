"""AppTest suite for #40: bench position dropdowns, contents display, and contents commands."""

import ast
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from wetlab.app.web import MAIN_PATH, labels
from wetlab.app.web.island_data import read_island
from wetlab.lab.service.main import create_virtual_service
from wetlab.lab.store import inbox, open_db, records


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


def test_place_labware_with_contents_table_enqueues_agreed_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#40(2): Filling the contents table on `place_labware` writes agreed `contents` payload."""
    db_file = tmp_path / "place_contents.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    conn = open_db(db_file, mode="virtual")
    try:
        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        assert len(at.data_editor(key="place_contents").value) == 1

        # Row with wells/volume but missing `name` is rejected with a Chinese toast
        at.text_input(key="place_id").set_value("LW-BAD")
        at.selectbox(key="place_pos").set_value("rack:1-2")
        at.session_state["place_contents"] = {
            "edited_rows": {0: {"name": "   ", "wells": "B1", "volume_ul": 10.0}},
            "added_rows": [],
            "deleted_rows": [],
        }
        at.button(key="place_submit").click().run()
        assert any("名称" in t.value for t in at.toast)
        assert inbox.list_unhandled_commands(conn) == []

        # Valid multi-row contents (specific wells + whole-plate blank wells)
        at.text_input(key="place_id").set_value("LW-010")
        at.text_input(key="place_type").set_value("96孔PCR板")
        at.selectbox(key="place_pos").set_value("rack:1-2")
        at.session_state["place_contents"] = {
            "edited_rows": {0: {"name": "样品 X", "wells": "A1, A2", "volume_ul": 20.0}},
            "added_rows": [{"name": "PCR master mix", "wells": "", "note": "4℃预冷"}],
            "deleted_rows": [],
        }
        at.button(key="place_submit").click().run()
        assert not at.exception

        cmds = inbox.list_unhandled_commands(conn)
        assert len(cmds) == 1
        assert cmds[0]["kind"] == "place_labware"
        assert cmds[0]["target_id"] == "LW-010"
        assert cmds[0]["payload"] == {
            "labware_id": "LW-010",
            "type": "96孔PCR板",
            "position": "rack:1-2",
            "contents": [
                {"name": "样品 X", "wells": ["A1", "A2"], "volume_ul": 20.0, "note": None},
                {
                    "name": "PCR master mix",
                    "wells": None,
                    "volume_ul": None,
                    "note": "4℃预冷",
                },
            ],
        }

        # Next untouched submission does not leak previous contents rows
        at.text_input(key="place_id").set_value("LW-011")
        at.selectbox(key="place_pos").set_value("shaker")
        at.button(key="place_submit").click().run()
        cmds_after = inbox.list_unhandled_commands(conn)
        assert len(cmds_after) == 2
        assert cmds_after[1]["payload"] == {
            "labware_id": "LW-011",
            "type": "96孔PCR板",
            "position": "shaker",
        }
    finally:
        conn.close()


def test_edit_existing_labware_contents_preloads_rows_and_enqueues_set_labware_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#40(3): Editing a registered plate's contents emits agreed `set_labware_contents` payload."""
    db_file = tmp_path / "edit_contents.sqlite"
    monkeypatch.delenv("WETLAB_MODE", raising=False)
    monkeypatch.setenv("WETLAB_DB", str(db_file))
    conn = open_db(db_file, mode="virtual")
    try:
        with conn:
            records.upsert_labware(conn, id="LW-001", type="96孔PCR板", position="pcr")
            records.upsert_labware(conn, id="LW-002", type="96孔PCR板", position="rack:1-3")
            conn.execute(
                "INSERT INTO labware_contents (labware_id, n, name, wells, volume_ul, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("LW-001", 1, "旧样品", '["A1"]', 15.0, None),
            )

        at = AppTest.from_file(str(MAIN_PATH), default_timeout=20).run()
        assert not at.exception
        assert "旧样品（A1 · 15 µL）" in _html(at)
        assert at.selectbox(key="contents_id").value == "LW-001"
        preloaded = at.data_editor(key="contents_rows").value
        assert list(preloaded["name"]) == ["旧样品"]
        assert list(preloaded["wells"]) == ["A1"]
        assert list(preloaded["volume_ul"]) == [15.0]

        at.session_state["contents_rows"] = {
            "edited_rows": {
                0: {"name": "样品 X", "wells": "B1, B2", "volume_ul": 30.0, "note": "复测"}
            },
            "added_rows": [{"name": "阴性对照", "wells": "H12", "volume_ul": 30.0}],
            "deleted_rows": [],
        }
        at.button(key="contents_submit").click().run()
        assert not at.exception

        cmds = inbox.list_unhandled_commands(conn)
        assert len(cmds) == 1
        assert cmds[0]["kind"] == "set_labware_contents"
        assert cmds[0]["target_id"] == "LW-001"
        assert cmds[0]["payload"] == {
            "labware_id": "LW-001",
            "contents": [
                {"name": "样品 X", "wells": ["B1", "B2"], "volume_ul": 30.0, "note": "复测"},
                {"name": "阴性对照", "wells": ["H12"], "volume_ul": 30.0, "note": None},
            ],
        }

        # Switching to LW-002 and submitting blank rows sends whole-table clear `contents: []`
        at.selectbox(key="contents_id").set_value("LW-002").run()
        at.button(key="contents_submit").click().run()
        cmds_all = inbox.list_unhandled_commands(conn)
        assert len(cmds_all) == 2
        assert cmds_all[1]["kind"] == "set_labware_contents"
        assert cmds_all[1]["payload"] == {"labware_id": "LW-002", "contents": []}
    finally:
        conn.close()


def test_g5_web_files_and_labels_respect_spec_conventions() -> None:
    """04 §12 & §16.8: Owned files stay `< 300` lines, `<= 100` chars, single assignment."""
    assert labels.COMMAND_LABELS["set_labware_contents"] == "修改孔板内容"
    updated_txt = labels.labware_event_text(
        "contents_updated",
        {"contents": [{"name": "样品 X", "wells": ["A1"], "volume_ul": 20.0, "note": None}]},
    )
    assert updated_txt == "更新孔板内容：样品 X（A1 · 20 µL）"
    assert labels.labware_event_text("contents_updated", {"contents": []}) == "清空孔板内容"

    repo = Path(__file__).resolve().parents[3]
    owned = [
        repo / "wetlab/app/web/labware.py",
        repo / "wetlab/app/web/labels.py",
        repo / "wetlab/app/web/island_data.py",
        Path(__file__).resolve(),
    ]
    for path in owned:
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        assert len(lines) < 300, f"{path.name} has {len(lines)} lines"
        over = [(idx + 1, len(ln)) for idx, ln in enumerate(lines) if len(ln) > 100]
        assert not over, f"{path.name} has lines > 100 chars: {over}"
        for node in ast.walk(ast.parse(src, filename=str(path))):
            if isinstance(node, ast.Assign):
                assert len(node.targets) == 1
                assert not isinstance(node.targets[0], (ast.Tuple, ast.List))
