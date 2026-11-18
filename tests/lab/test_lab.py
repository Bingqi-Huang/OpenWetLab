"""Tests for wetlab.lab.lab: freeze/stop (#1), fresh-frame wait (#4), operation_id (#12)."""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from wetlab.lab.errors import Blocked, DeviceFailed, Frozen, Unknown
from wetlab.lab.lab import Lab
from wetlab.lab.sim import DEFAULT_EPOCH, Fault, Scenario, SimBackend
from wetlab.lab.store import island, open_db, records


def test_normal_commands_operation_id_pass_through_and_last_operation() -> None:
    backend = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    conn = open_db(":memory:")
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "pcr_plate_96", "pcr")
    seen_op_ids: list[str | None] = []
    orig_call = backend.call

    def spy_call(
        device_code: str,
        action: str,
        params: dict[str, Any],
        timeout_s: float,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        seen_op_ids.append(operation_id)
        return orig_call(device_code, action, params, timeout_s, operation_id=operation_id)

    try:
        with patch.object(backend, "call", side_effect=spy_call):
            lab.do("pcr", "SetLidMotor", {"Action": "Lift"}, expect={"LidMotorState": "Lifted"})
            res = lab.do("pcr", "OpenDoor", expect={"DoorState": "Open"})
            assert res["success"] is True
            lab.do(
                "pcr",
                "CloseDoor",
                {"WaitLid": False},
                expect={"DoorState": "Closed", "LidMotorState": "Pressed"},
                within_s=15,
            )

        ops = records.list_operations(conn)
        assert [op["id"] for op in ops] == seen_op_ids
        assert ops[1]["elapsed_s"] == pytest.approx(4.1)
        assert ops[2]["elapsed_s"] >= 10.0
        assert lab.last_operation("pcr", "CloseDoor")["id"] == ops[2]["id"]  # type: ignore[index]
    finally:
        conn.close()


def test_operation_id_uniqueness_across_restarts_with_fixed_epoch(tmp_path: Path) -> None:
    db_file = tmp_path / "wetlab.sqlite"
    conn1 = open_db(db_file)
    sim1 = SimBackend(Scenario(start_time=DEFAULT_EPOCH))
    lab1 = Lab(sim1, conn1)
    lab1.do("pcr", "GetPcrStatus")
    lab1.do("pcr", "GetPcrStatus")
    conn1.close()

    conn2 = open_db(db_file)
    try:
        sim2 = SimBackend(Scenario(start_time=DEFAULT_EPOCH))
        lab2 = Lab(sim2, conn2)
        lab2.do("pcr", "GetPcrStatus")
        lab2.do("shaker", "GetStatus")
        ms = int(DEFAULT_EPOCH * 1000)
        assert [op["id"] for op in records.list_operations(conn2)] == [
            f"WL-{ms}-1",
            f"WL-{ms}-2",
            f"WL-{ms}-3",
            f"WL-{ms}-4",
        ]
    finally:
        conn2.close()


def test_frozen_blocks_normal_actions_but_allows_reads_and_stops() -> None:
    """#1: Frozen island rejects normal actions, allows READ and stop actions."""
    backend = SimBackend(
        Scenario(
            labware={"LW-001": "pcr"},
            faults=[Fault(device="shaker", action="SetClamp", nth=1, kind="no_result")],
        )
    )
    conn = open_db(":memory:")
    lab = Lab(backend, conn)
    lab.materials.place("LW-001", "pcr_plate_96", "pcr")
    try:
        lab.do(
            "pcr",
            "SetHeater",
            {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105},
            expect={"HeaterLidEnabled": True},
        )
        lab.do(
            "pcr",
            "RunProgram",
            {"Steps": '[{"ZoneATarget":37,"ZoneCTarget":37,"HoldSeconds":60,"Cycles":1}]'},
            expect={"PcrState": "Running"},
        )

        # Trigger freeze via shaker timeout
        with pytest.raises(Unknown) as exc_info:
            lab.do("shaker", "SetClamp", {"clamp": True})
        assert exc_info.value.code == "TIMEOUT_UNKNOWN"
        assert island.get_freeze(conn)["frozen"] is True

        # Normal ACT blocked with Frozen without calling backend
        with pytest.raises(Frozen):
            lab.do("shaker", "SetClamp", {"clamp": True})
        with pytest.raises(Frozen):
            lab.do("pcr", "SetHeater", {"Zone": "Lid", "Enabled": True, "TargetCelsius": 105})

        # Read-only AND stop actions (StopRun, SetHeater Lid off, StopShake) still execute!
        assert lab.do("pcr", "GetPcrStatus")["PcrState"] == "Running"
        lab.do("pcr", "StopRun", {}, expect={"PcrState": "Idle"})
        lab.do(
            "pcr",
            "SetHeater",
            {"Zone": "Lid", "Enabled": False},
            expect={"HeaterLidEnabled": False},
        )
        lab.do("shaker", "StopShake", {})
        assert lab.state()["pcr"]["PcrState"] == "Idle"
        assert lab.state()["pcr"]["HeaterLidEnabled"] is False

        lab.unfreeze()
        assert island.get_freeze(conn)["frozen"] is False
    finally:
        conn.close()


def test_step4_waits_for_post_return_fresh_frame_and_freezes_act_after_10s() -> None:
    """#4: Lab.do waits for a complete state frame received >= returned_at (10s timeout)."""
    backend = SimBackend()
    conn = open_db(":memory:")
    lab = Lab(backend, conn)
    try:
        # 1. Fresh frame arrives 1.6s after return -> Lab.do waits until +1.6s then succeeds
        frame_at = [backend.now()]
        orig_state = backend.state
        orig_call = backend.call
        orig_sleep = backend.sleep

        def delayed_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            res = orig_call(*args, **kwargs)
            frame_at[0] = backend.now() - 1.0  # Cached state predates return by 1.0s
            return res

        def delayed_sleep(sec: float) -> None:
            orig_sleep(sec)
            if backend.now() >= frame_at[0] + 2.6:
                frame_at[0] = backend.now()

        def custom_state() -> dict[str, Any]:
            st = orig_state()
            st["T002001"]["_age_s"] = max(0.0, backend.now() - frame_at[0])
            return st

        with (
            patch.object(backend, "call", side_effect=delayed_call),
            patch.object(backend, "sleep", side_effect=delayed_sleep),
            patch.object(backend, "state", side_effect=custom_state),
        ):
            t0 = backend.now()
            lab.do("shaker", "SetClamp", {"clamp": True}, expect={"ElmState": 1})
            assert backend.now() - t0 >= 2.0

        # 2. Read command without fresh frame for 10s -> waits 10s, does NOT freeze, succeeds
        frozen_age_at = backend.now() - 2.0

        def never_fresh_state() -> dict[str, Any]:
            st = orig_state()
            st["PCR002"]["_age_s"] = backend.now() - frozen_age_at
            return st

        with patch.object(backend, "state", side_effect=never_fresh_state):
            t_read = backend.now()
            res_read = lab.do("pcr", "GetPcrStatus")
            assert res_read["DoorState"] == "Closed"
            assert backend.now() - t_read == pytest.approx(10.0)
            assert island.get_freeze(conn)["frozen"] is False

        # 3. ACT command without fresh frame for 10s -> freezes with VERIFY_FAILED!
        fresh_before_check = [True]

        def act_goes_stale(*args: Any, **kwargs: Any) -> dict[str, Any]:
            res = orig_call(*args, **kwargs)
            fresh_before_check[0] = False
            return res

        stale_anchor = backend.now()

        def stale_after_call() -> dict[str, Any]:
            st = orig_state()
            if not fresh_before_check[0]:
                st["T002001"]["_age_s"] = backend.now() - stale_anchor + 0.5
            return st

        with (
            patch.object(backend, "call", side_effect=act_goes_stale),
            patch.object(backend, "state", side_effect=stale_after_call),
        ):
            with pytest.raises(Unknown) as exc_info:
                lab.do("shaker", "SetClamp", {"clamp": False})
            assert exc_info.value.code == "VERIFY_FAILED"
            assert island.get_freeze(conn)["frozen"] is True
    finally:
        conn.close()


def test_device_failed_waits_for_fresh_frame_without_freezing_and_rollback_safety() -> None:
    fault = Fault(device="shaker", action="SetClamp", nth=1, kind="fail", message="jam")
    backend = SimBackend(Scenario(faults=[fault]))
    conn = open_db(":memory:")
    lab = Lab(backend, conn)
    try:
        with pytest.raises(DeviceFailed, match="jam"):
            lab.do("shaker", "SetClamp", {"clamp": True})
        assert island.get_freeze(conn)["frozen"] is False
        op = records.list_operations(conn)[0]
        assert (op["status"], op["error_code"]) == ("failed", "DEVICE_FAILED")

        with patch.object(backend, "call", wraps=backend.call) as spy_call:
            with pytest.raises(Blocked):
                lab.do("shaker", "StartShakeTimed", {"seconds": 5})
            assert spy_call.call_count == 0

        backend2 = SimBackend(
            Scenario(faults=[Fault(device="pcr", action="OpenDoor", nth=1, kind="no_result")])
        )
        conn2 = open_db(":memory:")
        lab2 = Lab(backend2, conn2)
        with patch.object(island, "set_frozen", side_effect=RuntimeError("disk fault")):
            with pytest.raises(RuntimeError, match="disk fault"):
                lab2.do("pcr", "OpenDoor")
        assert records.list_operations(conn2) == []
        conn2.close()
    finally:
        conn.close()
