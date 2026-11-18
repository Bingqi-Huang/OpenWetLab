"""Tests for wetlab.lab.store.runs (missions, experiments, protocols, and steps)."""

from pathlib import Path
import sqlite3

import pytest

from wetlab.lab.store import open_db, runs


def test_runs_functions_never_auto_commit(tmp_path: Path) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    writer = open_db(db_file)
    reader = open_db(db_file)
    try:
        runs.insert_mission(
            writer, id="M-1", title="T", input_text="SOP", mode="virtual",
            status="open", created_at=1.0,
        )
        runs.insert_experiment(
            writer, id="E-1", mission_id="M-1", n=1,
            task={"objective": "heat"}, status="open",
        )
        runs.insert_protocol(
            writer, id="P-1", experiment_id="E-1", version=1,
            content={"name": "p1", "steps": []}, fingerprint="fp1",
            status="submitted", submitted_at=2.0,
        )
        runs.insert_steps(
            writer, "P-1", [{"n": 1, "kind": "skill", "content": {"skill": "pcr_open", "args": {}}}]
        )
        assert writer.in_transaction is True
        assert runs.list_missions(reader) == []
        assert runs.list_experiments(reader) == []
        assert runs.list_protocols(reader) == []
        assert runs.list_steps(reader, "P-1") == []

        writer.rollback()
        assert runs.list_missions(writer) == []
    finally:
        writer.close()
        reader.close()


def test_mission_and_experiment_lifecycle() -> None:
    conn = open_db(":memory:")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                runs.insert_mission(
                    conn, id="M-BAD", title="t", input_text="i", mode="invalid_mode",
                    status="open", created_at=1.0,
                )

        with conn:
            runs.insert_mission(
                conn, id="M-1", title="PCR library prep", input_text="Run 37C",
                mode="virtual", status="open", created_at=10.0,
            )
            runs.insert_mission(
                conn, id="M-2", title="Shake test", input_text="Shake 10s",
                mode="real", status="open", created_at=20.0,
            )

        assert [m["id"] for m in runs.list_missions(conn)] == ["M-1", "M-2"]

        with conn:
            n1 = runs.next_experiment_n(conn, "M-1")
            runs.insert_experiment(
                conn, id="E-1", mission_id="M-1", n=n1,
                task={"objective": "37C hold", "labware": {"LW-001": "pcr"}},
                status="open",
            )
            n2 = runs.next_experiment_n(conn, "M-1")
            runs.insert_experiment(
                conn, id="E-2", mission_id="M-1", n=n2,
                task={"objective": "98C cycle"}, status="open",
            )
            runs.finish_experiment(conn, "E-1", status="completed", notes="37C completed")
            runs.finish_mission(conn, "M-1", status="completed", conclusion="All steps succeeded")

        assert (n1, n2) == (1, 2)
        exp1 = runs.get_experiment(conn, "E-1")
        assert exp1 == {
            "id": "E-1",
            "mission_id": "M-1",
            "n": 1,
            "task": {"objective": "37C hold", "labware": {"LW-001": "pcr"}},
            "status": "completed",
            "notes": "37C completed",
        }
        assert [e["id"] for e in runs.list_experiments(conn, mission_id="M-1", status="open")] == ["E-2"]

        m1 = runs.get_mission(conn, "M-1")
        assert m1 is not None
        assert (m1["status"], m1["conclusion"]) == ("completed", "All steps succeeded")
        assert [m["id"] for m in runs.list_missions(conn, status="completed")] == ["M-1"]
    finally:
        conn.close()


def test_protocol_versions_decisions_and_status_transitions() -> None:
    conn = open_db(":memory:")
    try:
        with conn:
            runs.insert_mission(
                conn, id="M-1", title="t", input_text="i", mode="virtual", status="open", created_at=1.0
            )
            runs.insert_experiment(conn, id="E-1", mission_id="M-1", n=1, task={}, status="open")

            v1 = runs.next_protocol_version(conn, "E-1")
            runs.insert_protocol(
                conn, id="E-1-v1", experiment_id="E-1", version=v1,
                content={"name": "v1", "steps": []}, fingerprint="fp-v1",
                rehearsal={"ok": True, "total_s": 34.0}, text_md="# 方案 v1",
                status="submitted", submitted_at=10.0,
            )
            # Human rejects v1 with comment -> original rehearsal is preserved
            runs.decide_protocol(
                conn, "E-1-v1", status="rejected", decided_by="Alice",
                decided_at=15.0, reject_comment="请把温度改成 60℃",
            )

            v2 = runs.next_protocol_version(conn, "E-1")
            runs.insert_protocol(
                conn, id="E-1-v2", experiment_id="E-1", version=v2,
                content={"name": "v2", "steps": []}, fingerprint="fp-v2",
                rehearsal={"ok": True, "total_s": 40.0}, text_md="# 方案 v2",
                status="submitted", submitted_at=20.0,
            )
            # Human approves v2 with fresh rehearsal -> enters running, then paused -> running -> done
            runs.decide_protocol(
                conn, "E-1-v2", status="running", decided_by="Alice",
                decided_at=25.0, rehearsal={"ok": True, "total_s": 40.0, "rechecked": True},
                positions_hash="sha256-pos-v2",
            )
            runs.update_protocol_status(conn, "E-1-v2", "paused")
            runs.update_protocol_status(conn, "E-1-v2", "running")
            runs.update_protocol_status(conn, "E-1-v2", "done")

            v3 = runs.next_protocol_version(conn, "E-1")
            runs.insert_protocol(
                conn, id="E-1-v3", experiment_id="E-1", version=v3,
                content={"name": "v3", "steps": []}, fingerprint="fp-v3",
                status="submitted", submitted_at=30.0, positions_hash="sha256-pos-v3",
            )
            runs.decide_protocol(
                conn, "E-1-v3", status="rejected", decided_by="Alice",
                decided_at=35.0, reject_comment="无需 v3",
            )

        assert (v1, v2, v3) == (1, 2, 3)
        p1 = runs.get_protocol(conn, "E-1-v1")
        assert p1 is not None
        assert (p1["status"], p1["decided_by"], p1["reject_comment"]) == (
            "rejected", "Alice", "请把温度改成 60℃"
        )
        assert p1["rehearsal"] == {"ok": True, "total_s": 34.0}
        assert p1["positions_hash"] is None

        p2 = runs.get_protocol(conn, "E-1-v2")
        assert p2 is not None
        assert (p2["status"], p2["decided_by"], p2["decided_at"]) == ("done", "Alice", 25.0)
        assert p2["rehearsal"] == {"ok": True, "total_s": 40.0, "rechecked": True}
        assert p2["positions_hash"] == "sha256-pos-v2"

        p3 = runs.get_protocol(conn, "E-1-v3")
        assert p3 is not None
        assert p3["positions_hash"] == "sha256-pos-v3"
        assert [p["id"] for p in runs.list_protocols(conn, experiment_id="E-1")] == [
            "E-1-v1",
            "E-1-v2",
            "E-1-v3",
        ]
        assert [p["id"] for p in runs.list_protocols(conn, status="done")] == ["E-1-v2"]
    finally:
        conn.close()


def test_steps_bulk_insert_and_state_transitions_preserve_timestamps() -> None:
    conn = open_db(":memory:")
    try:
        with conn:
            runs.insert_mission(
                conn, id="M-1", title="t", input_text="i", mode="virtual", status="open", created_at=1.0
            )
            runs.insert_experiment(conn, id="E-1", mission_id="M-1", n=1, task={}, status="open")
            runs.insert_protocol(
                conn, id="P-1", experiment_id="E-1", version=1,
                content={}, fingerprint="fp", status="running", submitted_at=2.0,
            )
            runs.insert_steps(
                conn,
                "P-1",
                [
                    {"n": 1, "kind": "skill", "content": {"skill": "pcr_close", "args": {}}},
                    {"n": 2, "kind": "wait_until", "content": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 600}},
                    {"n": 3, "kind": "skill", "content": {"skill": "pcr_open", "args": {}}},
                ],
            )

            # Step 1: pending -> sent -> done
            runs.update_step(conn, "P-1", 1, status="sent", started_at=10.0)
            runs.update_step(conn, "P-1", 1, status="done", finished_at=14.1, result={"ok": True})

            # Step 2: pending -> waiting (with deadline) -> done
            runs.update_step(conn, "P-1", 2, status="waiting", started_at=15.0, wait_deadline=615.0)
            runs.update_step(conn, "P-1", 2, status="done", finished_at=45.0, result={"waited_s": 30.0})

            # Step 3: pending -> sent -> unknown -> concluded as failed via unfreeze
            runs.update_step(conn, "P-1", 3, status="sent", started_at=46.0)
            runs.update_step(conn, "P-1", 3, status="unknown", finished_at=61.0, result={"code": "TIMEOUT_UNKNOWN"})
            runs.update_step(conn, "P-1", 3, status="failed", finished_at=120.0, result={"unfrozen": "step_failed"})

        s1, s2, s3 = runs.list_steps(conn, "P-1")
        assert (s1["status"], s1["started_at"], s1["finished_at"], s1["result"]) == (
            "done", 10.0, 14.1, {"ok": True}
        )
        assert (s2["status"], s2["started_at"], s2["wait_deadline"], s2["finished_at"]) == (
            "done", 15.0, 615.0, 45.0
        )
        assert (s3["status"], s3["started_at"], s3["finished_at"], s3["result"]) == (
            "failed", 46.0, 120.0, {"unfrozen": "step_failed"}
        )
        assert runs.get_step(conn, "P-1", 99) is None
    finally:
        conn.close()
