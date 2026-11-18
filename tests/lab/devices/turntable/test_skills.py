"""Tests for rotate_turntable skill."""

from wetlab.lab import registry
from wetlab.lab.lab import Lab
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.store import open_db, records


def test_rotate_turntable_succeeds_and_blocks_invalid_angle_or_moving_arm():
    conn = open_db(":memory:")
    backend = SimBackend(Scenario())
    lab = Lab(backend, conn)

    ok = registry.call(lab, "rotate_turntable", angle=90)
    assert ok["ok"] is True
    assert ok["state_after"]["turntable"]["Angles"] == "90"

    # Invalid angle -> blocked / INVALID_ARGUMENT, zero commands sent.
    before = len(records.list_operations(conn))
    bad = registry.call(lab, "rotate_turntable", angle=45)
    assert bad["status"] == "blocked"
    assert bad["error"]["code"] == "INVALID_ARGUMENT"
    assert len(records.list_operations(conn)) == before

    # Blocked while arm is moving, 0 accidents.
    backend.world.device("arm").props["RobotMode"] = "运行中"
    moving_blk = registry.call(lab, "rotate_turntable", angle=180)
    assert moving_blk["status"] == "blocked"
    assert backend.world.accidents == []
