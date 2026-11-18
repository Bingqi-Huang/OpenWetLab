"""Tests for Chinese command error translation (`messages.py` & `commands.py`, 04 §16.7 #43)."""

import sqlite3

from pydantic import BaseModel, Field, ValidationError
import pytest

from wetlab.lab.service import messages
from wetlab.lab.service.schemas import validate_command_payload
from wetlab.lab.store import open_db


class _BoundsSample(BaseModel):
    count: int = Field(ge=1, le=10)
    volume_ul: float = Field(gt=0.0, lt=200.0)
    wells: list[str] = Field(min_length=1, max_length=8)
    flag: bool = True
    code: str = Field(min_length=2, max_length=6)


def _capture_validation_error(kind: str, payload: dict) -> ValidationError:
    with pytest.raises(ValidationError) as exc_info:
        validate_command_payload(kind, payload)
    return exc_info.value


def test_pydantic_validation_errors_translated_to_clear_chinese() -> None:
    """Pydantic errors name exact field path (`loc`) and human-friendly Chinese constraints."""
    err_task = _capture_validation_error(
        "create_experiment",
        {"mission_id": "M-1", "task": "这不是一张表"},
    )
    msg_task = messages.format_command_error(
        err_task,
        {"kind": "create_experiment", "payload": {"mission_id": "M-1", "task": "这不是一张表"}},
    )
    assert msg_task == "任务单格式不对：task 应该是一张表"

    err_missing = _capture_validation_error(
        "approve",
        {"protocol_id": "E-001-v1", "fingerprint": "abc"},
    )
    msg_missing = messages.format_command_error(err_missing, {"kind": "approve"})
    assert "缺少必填项" in msg_missing
    assert "decided_by" in msg_missing

    err_enum = _capture_validation_error(
        "finish_experiment",
        {"experiment_id": "E-001", "status": "great success"},
    )
    msg_enum = messages.format_command_error(err_enum, {"kind": "finish_experiment"})
    assert "status" in msg_enum
    assert "不在允许范围内" in msg_enum
    assert "completed" in msg_enum

    err_empty = _capture_validation_error(
        "reject",
        {"protocol_id": "E-001-v1", "comment": "   "},
    )
    msg_empty = messages.format_command_error(err_empty, {"kind": "reject"})
    assert "comment" in msg_empty
    assert "不能为空" in msg_empty

    err_extra = _capture_validation_error(
        "place_labware",
        {"labware_id": "LW-1", "position": "pcr", "unexpected_key": 1},
    )
    msg_extra = messages.format_command_error(err_extra, {"kind": "place_labware"})
    assert "unexpected_key" in msg_extra
    assert "多余字段" in msg_extra or "未定义" in msg_extra

    err_ag = _capture_validation_error("agent_error", {"mission_id": "M-1"})
    msg_ag = messages.format_command_error(err_ag, {"kind": "agent_error"})
    assert "error" in msg_ag and "不能为空" in msg_ag


def test_pydantic_numeric_list_bool_and_length_bounds_translated() -> None:
    """Numeric bounds, list/bool/int/float types, and string lengths produce pure Chinese."""
    cases = [
        ({"count": 0, "volume_ul": 10.0, "wells": ["A1"], "code": "AB"}, "count", "不能小于"),
        ({"count": 11, "volume_ul": 10.0, "wells": ["A1"], "code": "AB"}, "count", "不能大于"),
        ({"count": 1, "volume_ul": 0.0, "wells": ["A1"], "code": "AB"}, "volume_ul", "必须大于"),
        ({"count": 1, "volume_ul": 250.0, "wells": ["A1"], "code": "AB"}, "volume_ul", "必须小于"),
        ({"count": 1, "volume_ul": 10.0, "wells": "not_list", "code": "AB"}, "wells", "列表"),
        ({"count": 1, "volume_ul": 10.0, "wells": [], "code": "AB"}, "wells", "至少"),
        ({"count": "abc", "volume_ul": 10.0, "wells": ["A1"], "code": "AB"}, "count", "整数"),
        ({"count": 1, "volume_ul": "abc", "wells": ["A1"], "code": "AB"}, "volume_ul", "数字"),
        (
            {"count": 1, "volume_ul": 10.0, "wells": ["A1"], "flag": "not_bool", "code": "AB"},
            "flag",
            "布尔",
        ),
        ({"count": 1, "volume_ul": 10.0, "wells": ["A1"], "code": "A"}, "code", "至少"),
        ({"count": 1, "volume_ul": 10.0, "wells": ["A1"], "code": "ABCDEFG"}, "code", "最多"),
    ]
    for raw_input, expected_field, expected_phrase in cases:
        with pytest.raises(ValidationError) as exc_info:
            _BoundsSample.model_validate(raw_input)
        translated = messages.format_command_error(exc_info.value, None)
        assert expected_field in translated
        assert expected_phrase in translated
        assert "Input should be" not in translated


def test_sqlite_integrity_errors_translated_with_command_context() -> None:
    """Real SQLite IntegrityError exceptions translate to researcher-readable Chinese with IDs."""
    conn = open_db(":memory:")
    try:
        conn.execute(
            "INSERT INTO missions (id, title, input_text, mode, status, created_at) "
            "VALUES ('M-1', 't', 'i', 'virtual', 'running', 1.0)"
        )
        conn.execute("INSERT INTO labware (id, type, position) VALUES ('LW-1', '96_plate', 'pcr')")

        with pytest.raises(sqlite3.IntegrityError) as dup_m:
            conn.execute(
                "INSERT INTO missions (id, title, input_text, mode, status, created_at) "
                "VALUES ('M-1', 't2', 'i2', 'virtual', 'running', 2.0)"
            )
        msg_m = messages.format_command_error(
            dup_m.value,
            {"kind": "new_mission", "payload": {"mission_id": "M-1"}},
        )
        assert msg_m == "任务编号 M-1 已存在"

        with pytest.raises(sqlite3.IntegrityError) as dup_lw:
            conn.execute(
                "INSERT INTO labware (id, type, position) VALUES ('LW-1', '96_plate', 'shaker')"
            )
        msg_lw = messages.format_command_error(
            dup_lw.value,
            {"kind": "place_labware", "payload": {"labware_id": "LW-1", "position": "shaker"}},
        )
        assert "器皿编号 LW-1 已存在" in msg_lw

        with pytest.raises(sqlite3.IntegrityError) as dup_pos:
            conn.execute(
                "INSERT INTO labware (id, type, position) VALUES ('LW-2', '96_plate', 'pcr')"
            )
        msg_pos = messages.format_command_error(
            dup_pos.value,
            {"kind": "place_labware", "payload": {"labware_id": "LW-2", "position": "pcr"}},
        )
        assert "pcr" in msg_pos and "已有其他器皿" in msg_pos

        with pytest.raises(sqlite3.IntegrityError) as fk_exp:
            conn.execute(
                "INSERT INTO experiments (id, mission_id, n, task_json, status) "
                "VALUES ('E-9', 'M-NOPE', 1, '{}', 'running')"
            )
        msg_fk = messages.format_command_error(
            fk_exp.value,
            {"kind": "create_experiment", "payload": {"mission_id": "M-NOPE"}},
        )
        assert "M-NOPE" in msg_fk and "不存在" in msg_fk

        with pytest.raises(sqlite3.IntegrityError) as chk_mode:
            conn.execute(
                "INSERT INTO missions (id, title, input_text, mode, status, created_at) "
                "VALUES ('M-2', 't', 'i', 'invalid_mode', 'running', 1.0)"
            )
        msg_chk = messages.format_command_error(chk_mode.value, {"kind": "new_mission"})
        assert "mode" in msg_chk and "不合法" in msg_chk
    finally:
        conn.close()
