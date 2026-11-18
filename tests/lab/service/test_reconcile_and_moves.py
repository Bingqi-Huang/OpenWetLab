"""v0.6 §16.6 tests: #31 moving failure freeze in service & #34 startup GetDevices reconcile."""

from pathlib import Path

from wetlab.lab.service import Service
from wetlab.lab.sim import Fault, Scenario, SimBackend
from wetlab.lab.store import inbox, island, open_db, records, runs


def test_turntable_failed_after_act_freezes_island_and_marks_step_unknown(
    tmp_path: Path,
) -> None:
    """§16.6 #31: Virtual island injecting turntable 'failed after rotating to 90' freezes island
    and marks the protocol step `unknown` with `DEVICE_FAILED_MAY_HAVE_MOVED`.
    """
    fault = Fault(
        device="turntable",
        action="SingleLoop",
        nth=1,
        kind="failed_after_act",
        message="转向命令未收到电机应答",
    )
    sim = SimBackend(Scenario(faults=[fault]))
    conn = open_db(tmp_path / "tt.sqlite", mode="virtual")
    svc = Service(sim, conn, runs_dir=tmp_path / "runs")
    try:
        svc.startup()
        proto = {
            "name": "转向台90度",
            "labware": {},
            "steps": [{"skill": "rotate_turntable", "args": {"angle": 90}}],
        }
        with conn:
            inbox.enqueue_command(
                conn,
                key="m31",
                at=sim.now(),
                who="researcher",
                kind="new_mission",
                payload={"mission_id": "M-31", "title": "转向台故障"},
            )
            inbox.enqueue_command(
                conn,
                key="s31",
                at=sim.now(),
                who="operator",
                kind="submit_protocol",
                payload={"mission_id": "M-31", "protocol": proto},
            )
        svc.step()
        submitted = runs.get_protocol(conn, "E-001-v1")
        assert submitted is not None
        with conn:
            inbox.enqueue_command(
                conn,
                key="a31",
                at=sim.now(),
                who="researcher",
                kind="approve",
                payload={
                    "protocol_id": "E-001-v1",
                    "decided_by": "林老师",
                    "fingerprint": submitted["fingerprint"],
                },
            )
        svc.step()

        assert sim.state()["STEERING_PLATFORM001"]["Angles"] == "90"
        frz = island.get_freeze(conn)
        assert frz["frozen"] is True
        assert "转向命令未收到电机应答" in (frz["reason"] or "")

        step1 = runs.get_step(conn, "E-001-v1", 1)
        assert step1 is not None
        assert step1["status"] == "unknown"
        assert step1["result"]["error"]["code"] == "DEVICE_FAILED_MAY_HAVE_MOVED"
        assert runs.get_protocol(conn, "E-001-v1")["status"] == "running"  # type: ignore[index]

        tt_op = records.list_operations(conn, device="turntable")[-1]
        assert tt_op["status"] == "unknown"
        assert tt_op["error_code"] == "DEVICE_FAILED_MAY_HAVE_MOVED"
        assert any(e["kind"] == "frozen" for e in inbox.list_events_after(conn, 0))
    finally:
        conn.close()
