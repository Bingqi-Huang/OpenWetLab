"""Tests for `wetlab.lab.protocol.run` (`wait_until` #5 30s/61s stale freeze, #14, #30a)."""

from unittest.mock import patch

import pytest

from wetlab.lab.lab import Lab
from wetlab.lab.protocol import check_wait_until, run_step
from wetlab.lab.sim import Scenario, SimBackend
from wetlab.lab.store import island, open_db


def test_wait_skill_match_and_timeout() -> None:
    sim = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    conn = open_db(":memory:")
    lab = Lab(sim, conn)
    lab.materials.place("LW-001", "pcr_plate_96", "pcr")
    try:
        res_wait = run_step(lab, {"wait": {"seconds": 25}})
        assert res_wait["ok"] is True and res_wait["elapsed_s"] == pytest.approx(25.0)

        start_res = run_step(
            lab,
            {
                "skill": "pcr_start_program",
                "args": {
                    "stages": [{"temperature_c": 37, "hold_seconds": 20}],
                    "volume_ul": 50,
                    "lid_c": None,
                },
            },
        )
        assert start_res["ok"] is True

        # Timeout when max_seconds < remaining program time
        to_res = run_step(
            lab,
            {"wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 5}},
        )
        assert to_res["ok"] is False and to_res["error"]["code"] == "WAIT_TIMEOUT"

        # Succeeds when allowed enough time
        ok_res = run_step(
            lab,
            {"wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 120}},
        )
        assert ok_res["ok"] is True and ok_res["status"] == "succeeded"
    finally:
        conn.close()


def test_wait_until_fails_immediately_on_error_working_status_2_and_device_faults() -> None:
    """#14 & #30a: `_error`, `WorkingStatus=2`, and `rules.fault` each fail `wait_until`."""
    sim = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    conn = open_db(":memory:")
    lab = Lab(sim, conn)
    lab.materials.place("LW-001", "pcr_plate_96", "pcr")
    try:
        base_state = lab.state()
        base_state["pcr"]["PcrState"] = "Running"

        # 1. `_error` (#30a)
        s_err = {**base_state, "pcr": {**base_state["pcr"], "_error": "COM9 被其他程序占用"}}
        out_err, code_err, msg_err = check_wait_until(
            "pcr", {"PcrState": "Idle"}, s_err, now=10.0, deadline=100.0
        )
        assert (out_err, code_err) == ("failed", "DEVICE_ERROR")
        assert "COM9" in (msg_err or "")

        # 2. `WorkingStatus=2` (#30a)
        s_ws2 = {**base_state, "pcr": {**base_state["pcr"], "WorkingStatus": 2}}
        out_ws2, code_ws2, msg_ws2 = check_wait_until(
            "pcr", {"PcrState": "Idle"}, s_ws2, now=10.0, deadline=100.0
        )
        assert (out_ws2, code_ws2) == ("failed", "DEVICE_ERROR")
        assert "WorkingStatus=2" in (msg_ws2 or "")

        # 3. PCR `FaultText` and `PcrState == "Fault"` (#14)
        s_pcr_fault = {**base_state, "pcr": {**base_state["pcr"], "FaultText": "热盖传感器异常"}}
        assert check_wait_until(
            "pcr", {"PcrState": "Idle"}, s_pcr_fault, now=10.0, deadline=100.0
        )[:2] == ("failed", "DEVICE_ERROR")

        # 4. Shaker `ErrorCode` (#14)
        s_shk = {**base_state, "shaker": {**base_state["shaker"], "ErrorCode": 4}}
        assert check_wait_until(
            "shaker", {"ShakeState": 1}, s_shk, now=10.0, deadline=100.0
        )[:2] == ("failed", "DEVICE_ERROR")

        # Also verify end-to-end through `run_step` when `_error` appears mid-wait
        run_step(
            lab,
            {
                "skill": "pcr_start_program",
                "args": {
                    "stages": [{"temperature_c": 37, "hold_seconds": 60}],
                    "volume_ul": 50,
                    "lid_c": None,
                },
            },
        )
        orig_state = lab.state
        polls = [0]

        def inject_error_on_second_poll() -> dict:
            st = orig_state()
            polls[0] += 1
            if polls[0] >= 3:
                st["pcr"]["_error"] = "串口断开"
            return st

        with patch.object(lab, "state", side_effect=inject_error_on_second_poll):
            res = run_step(
                lab,
                {"wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 60}},
            )
        assert res["ok"] is False
        assert res["error"]["code"] == "DEVICE_ERROR"
        assert "串口断开" in res["error"]["message"]
    finally:
        conn.close()


def test_wait_until_30s_stale_recovers_while_61s_stale_freezes() -> None:
    """#5: Stale 30s keeps waiting and finishes; stale 61s freezes with STALE_TELEMETRY."""
    sim = SimBackend(Scenario(labware={"LW-001": "pcr"}))
    conn = open_db(":memory:")
    lab = Lab(sim, conn)
    lab.materials.place("LW-001", "pcr_plate_96", "pcr")
    try:
        # Direct `check_wait_until` checks at 30s vs 61s
        st_30 = lab.state()
        st_30["pcr"]["PcrState"] = "Running"
        st_30["pcr"]["_age_s"] = 30.0
        assert check_wait_until(
            "pcr", {"PcrState": "Idle"}, st_30, now=100.0, deadline=600.0
        ) == ("waiting", None, None)

        st_61 = lab.state()
        st_61["pcr"]["PcrState"] = "Running"
        st_61["pcr"]["_age_s"] = 61.0
        assert check_wait_until(
            "pcr", {"PcrState": "Idle"}, st_61, now=100.0, deadline=600.0
        ) == ("freeze", "STALE_TELEMETRY", "等待期间失去 pcr 的状态")

        # 1. End-to-end `run_step`: stale for 30s mid-program, then recovers -> completes!
        run_step(
            lab,
            {
                "skill": "pcr_start_program",
                "args": {
                    "stages": [{"temperature_c": 37, "hold_seconds": 45}],
                    "volume_ul": 50,
                    "lid_c": None,
                },
            },
        )
        t_start = sim.now()
        orig_state = lab.state

        def stale_for_30s_then_recover() -> dict:
            st = orig_state()
            dt = sim.now() - t_start
            if 5.0 <= dt <= 35.0:
                st["pcr"]["_age_s"] = dt - 5.0 + 11.0  # Up to 41s (< 60s), then recovers!
            return st

        wait_120 = {
            "wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 120}
        }
        with patch.object(lab, "state", side_effect=stale_for_30s_then_recover):
            ok_res = run_step(lab, wait_120)
        assert ok_res["ok"] is True
        assert island.get_freeze(conn)["frozen"] is False

        # 2. End-to-end `run_step`: stale continuously for >60s -> freezes island!
        run_step(
            lab,
            {
                "skill": "pcr_start_program",
                "args": {
                    "stages": [{"temperature_c": 37, "hold_seconds": 180}],
                    "volume_ul": 50,
                    "lid_c": None,
                },
            },
        )
        t_outage = sim.now()

        def stale_61s() -> dict:
            st = orig_state()
            st["pcr"]["_age_s"] = (sim.now() - t_outage) + 1.0
            return st

        wait_180 = {
            "wait_until": {"device": "pcr", "state": {"PcrState": "Idle"}, "max_seconds": 180}
        }
        with patch.object(lab, "state", side_effect=stale_61s):
            frz_res = run_step(lab, wait_180)
        assert frz_res["ok"] is False
        assert frz_res["status"] == "unknown"
        assert frz_res["error"]["code"] == "STALE_TELEMETRY"
        assert frz_res["error"]["message"] == "等待期间失去 pcr 的状态"
        frz = island.get_freeze(conn)
        assert frz["frozen"] is True
        assert frz["reason"] == "等待期间失去 pcr 的状态"
    finally:
        conn.close()
