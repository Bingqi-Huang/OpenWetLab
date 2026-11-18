"""v0.6 Section 16 tests: #10/#17/#21 command validation, rollback & 5-version cap, #15 style."""

import ast
import json
from pathlib import Path

from wetlab.lab.service import create_virtual_service, schemas
from wetlab.lab.store import inbox, island, runs

REPO_ROOT = Path(__file__).resolve().parents[3]
SERVICE_DIR = REPO_ROOT / "wetlab" / "lab" / "service"
SOP_37C = json.loads((REPO_ROOT / "sops" / "pcr_37c_30s.json").read_text(encoding="utf-8"))


def _enqueue(
    svc: object,
    kind: str,
    payload: object,
    *,
    key: str,
    who: str = "researcher",
    target_id: str | None = None,
) -> None:
    conn = svc.conn  # type: ignore[attr-defined]
    now = svc.backend.now()  # type: ignore[attr-defined]
    with conn:
        conn.execute(
            "INSERT INTO commands (key, at, who, kind, target_id, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, now, who, kind, target_id, json.dumps(payload, ensure_ascii=False)),
        )


def test_command_errors_rollback_without_freeze_and_emit_command_failed_and_agent_error(
    tmp_path: Path,
) -> None:
    """§16.2 #10 & §16.3 #17: String `task` on `create_experiment`, duplicate `new_mission`, and
    invalid status enums fail cleanly without freezing, allow subsequent commands to run, and
    emit `command_failed` for Agent commands; `agent_error` emits `agent_error` event.
    """
    assert len(schemas.PAYLOAD_SCHEMAS) == 19
    svc = create_virtual_service(tmp_path / "cmd_err.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(svc, "new_mission", {"mission_id": "M-100", "title": "正常任务"}, key="m100")
        svc.step()

        _enqueue(
            svc,
            "create_experiment",
            {"mission_id": "M-100", "task": "这不是字典而是字符串"},
            key="bad-ce",
            who="coordinator",
            target_id="M-100",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-100", "title": "重复编号"},
            key="dup-m100",
            who="web",
        )
        _enqueue(
            svc,
            "finish_mission",
            {"mission_id": "M-100", "status": "great success", "conclusion": "x"},
            key="bad-fm",
            who="coordinator",
            target_id="M-100",
        )
        _enqueue(
            svc,
            "agent_error",
            {"mission_id": "M-100", "error": "连接超时"},
            key="ag-err-1",
            who="coordinator",
            target_id="M-100",
        )
        _enqueue(
            svc,
            "message",
            {"mission_id": "M-100", "text": "后续正常留言"},
            key="ok-msg",
            who="web",
        )
        svc.step()

        assert island.get_freeze(svc.conn)["frozen"] is False
        for bad_key in ("bad-ce", "dup-m100", "bad-fm"):
            row = inbox.get_command_by_key(svc.conn, bad_key)
            assert row is not None and row["handled_at"] is not None
            assert json.loads(row["outcome"])["ok"] is False

        ok_row = inbox.get_command_by_key(svc.conn, "ok-msg")
        assert ok_row is not None and json.loads(ok_row["outcome"])["ok"] is True

        events = inbox.list_events_after(svc.conn, 0)
        cmd_failed = [e for e in events if e["kind"] == "command_failed"]
        assert [e["payload"]["kind"] for e in cmd_failed] == ["create_experiment", "finish_mission"]
        assert all(e["ref_id"] == "M-100" for e in cmd_failed)
        assert any(
            e["kind"] == "agent_error" and e["payload"]["error"] == "连接超时" for e in events
        )
        assert any(
            e["kind"] == "message" and e["payload"]["text"] == "后续正常留言" for e in events
        )
    finally:
        svc.conn.close()


def test_sixth_submit_protocol_marks_experiment_failed_and_bad_exp_status_rejected(
    tmp_path: Path,
) -> None:
    """§16.3 #17 & #21: `finish_experiment(status='great success')` is rejected; submitting a 6th
    protocol version marks the experiment `failed` ('方案修改次数超过上限') and emits
    `experiment_finished`.
    """
    svc = create_virtual_service(tmp_path / "cap5.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(svc, "new_mission", {"mission_id": "M-CAP", "title": "五版上限"}, key="m-cap")
        _enqueue(
            svc,
            "create_experiment",
            {
                "mission_id": "M-CAP",
                "task": {"objective": "测试上限", "procedure": "5版", "labware": {}},
            },
            key="ce-cap",
            who="coordinator",
        )
        svc.step()

        _enqueue(
            svc,
            "finish_experiment",
            {"experiment_id": "E-001", "status": "great success", "notes": "非法状态"},
            key="bad-fe",
            who="operator",
            target_id="E-001",
        )
        svc.step()
        bad_fe = inbox.get_command_by_key(svc.conn, "bad-fe")
        assert bad_fe is not None and json.loads(bad_fe["outcome"])["ok"] is False
        assert runs.get_experiment(svc.conn, "E-001")["status"] == "running"  # type: ignore[index]

        for ver in range(1, 6):
            _enqueue(
                svc,
                "submit_protocol",
                {"experiment_id": "E-001", "protocol": SOP_37C},
                key=f"sub-v{ver}",
                who="operator",
                target_id="E-001",
            )
            svc.step()
            assert runs.get_protocol(svc.conn, f"E-001-v{ver}") is not None
            exp_now = runs.get_experiment(svc.conn, "E-001")
            assert exp_now is not None and exp_now["status"] == "running"

        _enqueue(
            svc,
            "submit_protocol",
            {"experiment_id": "E-001", "protocol": SOP_37C},
            key="sub-v6",
            who="operator",
            target_id="E-001",
        )
        svc.step()

        assert runs.get_protocol(svc.conn, "E-001-v6") is None
        exp = runs.get_experiment(svc.conn, "E-001")
        assert exp is not None
        assert exp["status"] == "failed"
        assert exp["notes"] == "方案修改次数超过上限"
        fin_evs = [
            e
            for e in inbox.list_events_after(svc.conn, 0)
            if e["kind"] == "experiment_finished" and e["ref_id"] == "E-001"
        ]
        assert len(fin_evs) == 1
        assert fin_evs[0]["payload"]["status"] == "failed"
    finally:
        svc.conn.close()


def test_service_files_under_300_lines_max_100_chars_and_single_assignment() -> None:
    """§12 rules 8 & 11, §16.2 #15: Every `.py` file in `wetlab/lab/service/` is `< 300` lines,
    has zero lines `> 100` characters, and has zero multi-variable / tuple-packing assignments.
    """
    py_files = sorted(SERVICE_DIR.glob("*.py"))
    assert len(py_files) >= 8
    for path in py_files:
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        assert len(lines) < 300, f"{path.name} has {len(lines)} lines (>= 300)"
        over_100 = [(idx + 1, len(line)) for idx, line in enumerate(lines) if len(line) > 100]
        assert not over_100, f"{path.name} has lines > 100 chars: {over_100}"
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                assert len(node.targets) == 1, f"{path.name}:{node.lineno} chained assignment"
                assert not isinstance(
                    node.targets[0], (ast.Tuple, ast.List)
                ), f"{path.name}:{node.lineno} multi-target assignment"
