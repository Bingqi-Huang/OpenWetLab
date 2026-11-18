"""v0.6 Section 16 tests: #8 strictly single-mission isolated `service/report.py`."""

import json
from pathlib import Path

from wetlab.lab.service import advance, create_virtual_service, generate_report
from wetlab.lab.store import island, records, runs

REPO_ROOT = Path(__file__).resolve().parents[3]
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


def test_report_strictly_isolated_per_mission_and_removed_from_advance(tmp_path: Path) -> None:
    """§16.1 #8: `advance.py` has no report writer; in a shared DB with two sequential missions
    where Mission 1 has `stop_all`, `correct_labware`, and `frozen`/`unfreeze`, Mission 2's
    `report.md` contains ONLY Mission 2's labware, operations, and events.
    """
    assert not hasattr(advance, "write_factual_mission_report")

    svc = create_virtual_service(tmp_path / "shared.sqlite", runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-M1", "type": "96_plate", "position": "shaker"},
            key="lw-m1",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-FIRST", "title": "前序任务", "input_text": "前序振荡"},
            key="m-first",
        )
        m1_proto = {
            "name": "前序长振荡",
            "labware": {"LW-M1": "shaker"},
            "steps": [{"skill": "shake_start", "args": {"rpm": 700}}, {"wait": {"seconds": 300}}],
        }
        _enqueue(
            svc, "submit_protocol", {"mission_id": "M-FIRST", "protocol": m1_proto}, key="s-m1"
        )
        svc.step()
        p_m1 = runs.get_protocol(svc.conn, "E-001-v1")
        assert p_m1 is not None
        _enqueue(
            svc,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "甲老师", "fingerprint": p_m1["fingerprint"]},
            key="app-m1",
        )
        svc.run(max_rounds=2)
        with svc.conn:
            island.set_frozen(svc.conn, reason="前序冻结事故", at=svc.backend.now())
        _enqueue(svc, "stop_all", {}, key="stop-m1")
        _enqueue(svc, "unfreeze", {"conclusion": "step_failed"}, key="unf-m1")
        _enqueue(
            svc,
            "correct_labware",
            {"labware_id": "LW-M1", "position": "rack:1-3"},
            key="corr-m1",
        )
        _enqueue(
            svc,
            "finish_experiment",
            {"experiment_id": "E-001", "status": "stopped", "notes": "前序实验已停"},
            key="fe-m1",
            who="operator",
        )
        _enqueue(
            svc,
            "finish_mission",
            {"mission_id": "M-FIRST", "status": "failed", "conclusion": "前序任务终止"},
            key="fm-m1",
            who="coordinator",
        )
        svc.step()

        _enqueue(
            svc,
            "place_labware",
            {"labware_id": "LW-001", "type": "96_plate", "position": "pcr"},
            key="lw-m2",
        )
        _enqueue(
            svc,
            "new_mission",
            {"mission_id": "M-SECOND", "title": "后序任务", "input_text": "37℃恒温"},
            key="m-second",
        )
        _enqueue(
            svc, "submit_protocol", {"mission_id": "M-SECOND", "protocol": SOP_37C}, key="s-m2"
        )
        svc.step()
        p_m2 = runs.get_protocol(svc.conn, "E-002-v1")
        assert p_m2 is not None
        _enqueue(
            svc,
            "approve",
            {"protocol_id": "E-002-v1", "decided_by": "乙老师", "fingerprint": p_m2["fingerprint"]},
            key="app-m2",
        )
        for _ in range(55):
            svc.step()
            if runs.get_protocol(svc.conn, "E-002-v1")["status"] == "done":  # type: ignore[index]
                break
        _enqueue(
            svc,
            "finish_experiment",
            {"experiment_id": "E-002", "status": "completed", "notes": "后序实验顺利"},
            key="fe-m2",
            who="operator",
        )
        _enqueue(
            svc,
            "finish_mission",
            {"mission_id": "M-SECOND", "status": "completed", "conclusion": "后序任务完成"},
            key="fm-m2",
            who="coordinator",
        )
        svc.step()

        m2_report_file = tmp_path / "runs" / "M-SECOND" / "report.md"
        assert m2_report_file.is_file()
        m2_text = m2_report_file.read_text(encoding="utf-8")
        assert "## 第一部分：事实记录" in m2_text
        assert "## 第二部分：说明与结论" in m2_text
        assert "LW-001" in m2_text
        assert "乙老师" in m2_text
        assert "后序实验顺利" in m2_text
        assert "后序任务完成" in m2_text

        m2_ops = records.list_operations(svc.conn, protocol_id="E-002-v1")
        table_rows = [
            ln for ln in m2_text.splitlines() if ln.startswith("| ") and not ln.startswith("| 序号 |")
        ]
        assert len(table_rows) == len(m2_ops)

        for leaked in (
            "M-FIRST",
            "E-001",
            "LW-M1",
            "甲老师",
            "stop_all",
            "unfreeze",
            "前序冻结事故",
            "correct_labware",
            "corrected",
            "前序实验已停",
            "前序任务终止",
        ):
            assert leaked not in m2_text, f"leaked {leaked!r} into M-SECOND report"

        m1_recheck = generate_report(svc.conn, "M-FIRST")
        for m2_token in ("M-SECOND", "E-002", "LW-001", "乙老师", "后序任务完成"):
            assert m2_token not in m1_recheck
    finally:
        svc.conn.close()
