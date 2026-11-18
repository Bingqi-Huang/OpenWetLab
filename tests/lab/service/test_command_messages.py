"""Tests for Chinese command error translation (`messages.py` & `commands.py`, 04 §16.7 #43)."""

import json
from pathlib import Path
import sqlite3

from pydantic import BaseModel, Field, ValidationError
import pytest

from wetlab.lab.errors import Blocked
from wetlab.lab.service import create_virtual_service, messages
from wetlab.lab.service.schemas import CommandError, validate_command_payload
from wetlab.lab.store import inbox, open_db

SOP_PATH = Path(__file__).resolve().parents[3] / "sops" / "pcr_37c_30s.json"


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
    err_task = _capture_validation_error("create_experiment", {"mission_id": "M-1", "task": "str"})
    msg_task = messages.format_command_error(err_task, {"kind": "create_experiment"})
    assert msg_task == "任务单格式不对：task 应该是一张表"

    err_missing = _capture_validation_error("approve", {"protocol_id": "P-1", "fingerprint": "x"})
    msg_missing = messages.format_command_error(err_missing, {"kind": "approve"})
    assert "缺少必填项" in msg_missing and "decided_by" in msg_missing

    err_enum = _capture_validation_error("finish_experiment", {"status": "great success"})
    msg_enum = messages.format_command_error(err_enum, {"kind": "finish_experiment"})
    assert "status" in msg_enum and "不在允许范围内" in msg_enum and "completed" in msg_enum

    err_empty = _capture_validation_error("reject", {"protocol_id": "P-1", "comment": "   "})
    msg_empty = messages.format_command_error(err_empty, {"kind": "reject"})
    assert "comment" in msg_empty and "不能为空" in msg_empty

    err_extra = _capture_validation_error(
        "place_labware", {"labware_id": "LW-1", "position": "pcr", "unexpected_key": 1}
    )
    msg_extra = messages.format_command_error(err_extra, {"kind": "place_labware"})
    assert "unexpected_key" in msg_extra and "多余字段" in msg_extra

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
        assert expected_field in translated and expected_phrase in translated
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
            dup_m.value, {"kind": "new_mission", "payload": {"mission_id": "M-1"}}
        )
        assert msg_m == "任务编号 M-1 已存在"

        with pytest.raises(sqlite3.IntegrityError) as dup_lw:
            conn.execute("INSERT INTO labware (id, type, position) VALUES ('LW-1', '96', 'shaker')")
        msg_lw = messages.format_command_error(
            dup_lw.value, {"kind": "place_labware", "payload": {"labware_id": "LW-1"}}
        )
        assert "器皿编号 LW-1 已存在" in msg_lw

        with pytest.raises(sqlite3.IntegrityError) as dup_pos:
            conn.execute("INSERT INTO labware (id, type, position) VALUES ('LW-2', '96', 'pcr')")
        msg_pos = messages.format_command_error(
            dup_pos.value, {"kind": "place_labware", "payload": {"position": "pcr"}}
        )
        assert "pcr" in msg_pos and "已有其他器皿" in msg_pos

        with pytest.raises(sqlite3.IntegrityError) as fk_exp:
            conn.execute(
                "INSERT INTO experiments (id, mission_id, n, task_json, status) "
                "VALUES ('E-9', 'M-NOPE', 1, '{}', 'running')"
            )
        msg_fk = messages.format_command_error(
            fk_exp.value, {"kind": "create_experiment", "payload": {"mission_id": "M-NOPE"}}
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


def test_domain_and_materials_errors_translated_and_chinese_preserved() -> None:
    """`CommandError` and `Blocked` English strings become clear Chinese; Chinese stays intact."""
    domain_cases = [
        (CommandError("unknown command kind: teleport"), None, ["不支持", "teleport"]),
        (CommandError("command payload must be a JSON object"), None, ["payload", "一张表"]),
        (CommandError("unknown mission: M-999"), None, ["任务", "M-999", "不存在"]),
        (CommandError("unknown experiment: E-999"), None, ["实验", "E-999", "不存在"]),
        (CommandError("experiment E-001 is already completed"), None, ["E-001", "已完成", "不能"]),
        (
            CommandError("protocol is not in submitted status"),
            {"payload": {"protocol_id": "E-001-v1"}},
            ["E-001-v1", "待审阅", "批准"],
        ),
        (CommandError("fingerprint mismatch"), None, ["fingerprint", "不一致"]),
        (CommandError("another protocol is already active"), None, ["正在执行", "暂停"]),
        (
            CommandError("reject requires a submitted protocol"),
            {"payload": {"protocol_id": "E-001-v1"}},
            ["E-001-v1", "待审阅", "退回"],
        ),
        (CommandError("no running protocol to pause"), None, ["没有正在运行的方案", "暂停"]),
        (CommandError("resume is only allowed for a paused protocol"), None, ["已暂停", "继续"]),
        (CommandError("island is not frozen"), None, ["冻结", "无需"]),
        (
            CommandError("unfreeze on a transfer step requires labware_position"),
            None,
            ["搬运", "labware_position"],
        ),
        (CommandError("cannot resolve experiment to abort"), None, ["实验", "终止"]),
        (Blocked("unknown position: turntable"), None, ["turntable", "不能放置器皿"]),
        (Blocked("unknown position: mars"), None, ["未知", "mars"]),
        (
            Blocked("labware LW-1 is already on the island at pcr"),
            None,
            ["LW-1", "pcr", "不能重复"],
        ),
        (Blocked("position pcr is already occupied by LW-1"), None, ["pcr", "LW-1", "占用"]),
        (Blocked("labware LW-9 is not on the island"), None, ["LW-9", "不在"]),
        (Blocked("unknown labware: LW-9"), None, ["LW-9", "未"]),
    ]
    for exc, cmd_ctx, expected_tokens in domain_cases:
        out = messages.format_command_error(exc, cmd_ctx)
        for tok in expected_tokens:
            assert tok in out, f"expected {tok!r} in {out!r} for {exc!r}"
        assert "命令未能执行：" not in out

    kept = messages.format_command_error(CommandError("示教点位已更新，请重新预演并批准"), None)
    assert kept == "示教点位已更新，请重新预演并批准"


def test_service_commands_store_chinese_error_and_error_raw_end_to_end(tmp_path: Path) -> None:
    """§16.7 #43: `commands.outcome` stores `ok=False`, Chinese `error`, and `error_raw`."""
    sop = json.loads(SOP_PATH.read_text(encoding="utf-8"))
    svc = create_virtual_service(tmp_path / "wetlab.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        with svc.conn:
            inbox.enqueue_command(
                svc.conn, key="m1", at=1.0, who="web", kind="new_mission",
                payload={"mission_id": "M-1", "title": "首个任务"},
            )
            inbox.enqueue_command(
                svc.conn, key="lw1", at=1.0, who="web", kind="place_labware",
                payload={"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            )
        svc.step()

        bad_cmds = [
            ("dup-m", "web", "new_mission", None, {"mission_id": "M-1", "title": "重复"}),
            (
                "bad-task", "coordinator", "create_experiment", "M-1",
                {"mission_id": "M-1", "task": "字符串"},
            ),
            (
                "miss-by", "web", "approve", "E-001-v1",
                {"protocol_id": "E-001-v1", "fingerprint": "x"},
            ),
            ("bad-enum", "operator", "finish_experiment", "E-001", {"status": "great"}),
            ("dup-lw", "web", "place_labware", None, {"labware_id": "LW-001", "position": "pcr"}),
            ("occ-pos", "web", "place_labware", None, {"labware_id": "LW-002", "position": "pcr"}),
            (
                "bad-pos", "web", "place_labware", None,
                {"labware_id": "LW-003", "position": "turntable"},
            ),
            ("no-pause", "web", "pause", None, {}),
            ("no-res", "web", "resume", None, {}),
            ("no-unf", "web", "unfreeze", None, {"conclusion": "step_done"}),
            ("bad-kind", "web", "no_such_cmd", None, {}),
        ]
        with svc.conn:
            for key, who, kind, tid, payload in bad_cmds:
                inbox.enqueue_command(
                    svc.conn, key=key, at=2.0, who=who, kind=kind, target_id=tid, payload=payload,
                )
        svc.step()

        row_dup = inbox.get_command_by_key(svc.conn, "dup-m")
        assert row_dup is not None
        assert json.loads(row_dup["outcome"]) == {
            "ok": False,
            "error": "任务编号 M-1 已存在",
            "error_raw": "UNIQUE constraint failed: missions.id",
        }

        row_task = inbox.get_command_by_key(svc.conn, "bad-task")
        assert row_task is not None
        out_task = json.loads(row_task["outcome"])
        assert out_task["ok"] is False
        assert out_task["error"] == "任务单格式不对：task 应该是一张表"
        assert "TaskSheetPayload" in out_task["error_raw"]

        checks = [
            ("miss-by", "decided_by", "Field required"),
            ("bad-enum", "status", "Input should be"),
            ("dup-lw", "LW-001", "already on the island"),
            ("occ-pos", "pcr", "already occupied"),
            ("bad-pos", "turntable", "unknown position: turntable"),
            ("no-pause", "暂停", "no running protocol"),
            ("no-res", "已暂停", "resume is only allowed"),
            ("no-unf", "冻结", "island is not frozen"),
            ("bad-kind", "no_such_cmd", "unknown command kind"),
        ]
        for key, cn_token, raw_token in checks:
            row = inbox.get_command_by_key(svc.conn, key)
            assert row is not None
            out = json.loads(row["outcome"])
            assert out["ok"] is False
            assert cn_token in out["error"]
            assert raw_token in out["error_raw"]

        events = inbox.list_events_after(svc.conn, 0)
        cmd_failed = [e for e in events if e["kind"] == "command_failed"]
        assert len(cmd_failed) == 2
        assert cmd_failed[0]["payload"]["error"] == "任务单格式不对：task 应该是一张表"
        assert "TaskSheetPayload" in cmd_failed[0]["payload"]["error_raw"]

        for ver in range(1, 7):
            with svc.conn:
                inbox.enqueue_command(
                    svc.conn, key=f"sub-{ver}", at=3.0, who="operator",
                    kind="submit_protocol", payload={"mission_id": "M-1", "protocol": sop},
                )
            svc.step()
        row_v6 = inbox.get_command_by_key(svc.conn, "sub-6")
        assert row_v6 is not None
        out_v6 = json.loads(row_v6["outcome"])
        assert out_v6["ok"] is False
        assert out_v6["error"] == "方案修改次数超过上限"
        assert out_v6["error_raw"] == "方案修改次数超过上限"
    finally:
        svc.conn.close()
