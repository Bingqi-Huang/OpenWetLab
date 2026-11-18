"""v0.6 Section 16 tests: #3 mode isolation before backend & mission mode, #25 2nd-conn `sent`."""

from pathlib import Path
import sqlite3

import pytest

from wetlab.lab.service import Service, create_virtual_service, main as service_main, run_daemon
from wetlab.lab.sim import SimBackend
from wetlab.lab.store import db, inbox, open_db, runs


def _enqueue(svc: Service, kind: str, payload: dict, *, key: str) -> None:
    with svc.conn:
        inbox.enqueue_command(
            svc.conn,
            key=key,
            at=svc.backend.now(),
            who="researcher",
            kind=kind,
            payload=payload,
        )


def test_mode_isolation_rejects_mismatch_before_mqtt_and_sets_mission_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§16.1 #3: Virtual DB opened in `real` mode raises BEFORE instantiating `MqttBackend`;
    default paths isolate `runs/virtual` vs `runs/real`; `new_mission` sets `mode` from DB.
    """
    run_daemon(mode="virtual", runs_dir=tmp_path, max_rounds=1)
    virt_db = db.default_db_path("virtual", base_dir=tmp_path)
    assert virt_db == tmp_path / "virtual" / "wetlab.sqlite"
    assert virt_db.is_file()

    mqtt_calls: list[object] = []

    class ForbiddenMqtt:
        def __init__(self, *args: object, **kwargs: object) -> None:
            mqtt_calls.append((args, kwargs))
            raise AssertionError("MqttBackend must not be constructed on mode mismatch")

    monkeypatch.setattr(service_main, "MqttBackend", ForbiddenMqtt)
    with pytest.raises(ValueError, match="virtual"):
        run_daemon(mode="real", db_path=virt_db, runs_dir=tmp_path / "real", max_rounds=1)
    assert mqtt_calls == []

    real_conn = open_db(tmp_path / "real_test.sqlite", mode="real")
    svc_real = Service(SimBackend(), real_conn, runs_dir=tmp_path / "real_runs")
    try:
        svc_real.startup()
        _enqueue(
            svc_real,
            "new_mission",
            {"mission_id": "M-REAL-1", "title": "真机任务", "mode": "virtual"},
            key="nm-real",
        )
        svc_real.step()
        m_real = runs.get_mission(real_conn, "M-REAL-1")
        assert m_real is not None and m_real["mode"] == "real"
    finally:
        real_conn.close()


def test_step_persisted_as_sent_visible_from_second_sqlite_connection(tmp_path: Path) -> None:
    """§16.4 #25: At the exact moment a skill executes, an independent 2nd `sqlite3.connect`
    reads `steps.status == 'sent'` from disk (proving `sent` was committed before dispatch).
    """
    db_file = tmp_path / "sent_check.sqlite"
    svc = create_virtual_service(db_file, runs_dir=tmp_path / "runs")
    observer = sqlite3.connect(db_file)
    observed: list[str] = []
    orig_call = svc.backend.call

    def check_second_conn(
        device_code: str,
        action: str,
        params: dict,
        timeout_s: float,
        operation_id: str | None = None,
    ) -> dict:
        if action == "OpenDoor":
            row = observer.execute(
                "SELECT status FROM steps WHERE protocol_id = 'E-001-v1' AND n = 1"
            ).fetchone()
            assert row is not None
            observed.append(str(row[0]))
        return orig_call(device_code, action, params, timeout_s, operation_id=operation_id)

    svc.backend.call = check_second_conn  # type: ignore[method-assign]
    try:
        svc.startup()
        proto = {"name": "开门检查", "labware": {}, "steps": [{"skill": "pcr_open", "args": {}}]}
        _enqueue(svc, "new_mission", {"mission_id": "M-025", "title": "sent落盘"}, key="m25")
        _enqueue(svc, "submit_protocol", {"mission_id": "M-025", "protocol": proto}, key="s25")
        svc.step()
        p = runs.get_protocol(svc.conn, "E-001-v1")
        assert p is not None
        _enqueue(
            svc,
            "approve",
            {"protocol_id": "E-001-v1", "decided_by": "孙老师", "fingerprint": p["fingerprint"]},
            key="a25",
        )
        svc.step()
        assert observed == ["sent"]
        assert runs.get_step(svc.conn, "E-001-v1", 1)["status"] == "done"  # type: ignore[index]
    finally:
        observer.close()
        svc.conn.close()
