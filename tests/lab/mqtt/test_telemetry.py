"""Capture-replay tests for `wetlab.lab.mqtt.telemetry` (`TelemetryTracker`, #13 300s expiry)."""

from datetime import datetime
import json
import logging
from pathlib import Path

import pytest

from wetlab.lab import safety
from wetlab.lab.devices import CODE_TO_NAME
from wetlab.lab.mqtt.codec import IgnoredFrame, parse_uplink
from wetlab.lab.mqtt.telemetry import EXTERNAL_REQUEST_EXPIRY_S, TelemetryTracker

CAPTURES = Path(__file__).resolve().parents[3] / "captures"


def _capture_line(filename: str, lineno: int) -> dict:
    lines = (CAPTURES / filename).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[lineno - 1])


def _tst_epoch(rec: dict) -> float:
    return datetime.fromisoformat(rec["tst"]).timestamp()


def _to_lab_state(raw_state: dict) -> dict:
    out: dict = {"_gateway": dict(raw_state["_gateway"])}
    for code, props in raw_state.items():
        if code in CODE_TO_NAME:
            out[CODE_TO_NAME[code]] = dict(props)
    return out


def test_gw002_arm_telemetry_discarded_leaving_gw001_arm_untouched() -> None:
    tracker = TelemetryTracker()
    gw1_arm = _capture_line("lab_v1_20261117_173423.jsonl", 10)
    tracker.apply(parse_uplink(gw1_arm["topic"], gw1_arm["payload"]), received_at=100.0)

    gw2_count = 0
    raw_lines = (CAPTURES / "lab_v1_20261117_173423.jsonl").read_text(encoding="utf-8").splitlines()
    for raw_line in raw_lines:
        rec = json.loads(raw_line)
        if rec["topic"] != "lab/v1/devices/TYPE_ROBOT_ARM001/telemetry":
            continue
        payload = json.loads(rec["payload"])
        if payload.get("GatewayCode") == "GW_002":
            frame = parse_uplink(rec["topic"], rec["payload"])
            assert frame == IgnoredFrame("other_gateway")
            tracker.apply(frame, received_at=105.0)
            gw2_count += 1

    assert gw2_count == 10
    after = tracker.state(now=105.0)["TYPE_ROBOT_ARM001"]
    assert after["RobotMode"] == "运行中"
    assert after["SafetyStatus"] == "NORMAL"
    assert after["_error"] is None
    assert after["_age_s"] == pytest.approx(5.0)


def test_pcr_serial_disconnect_error_only_telemetry_blocks_safety() -> None:
    tracker = TelemetryTracker()
    hb = _capture_line("lab_v1_20261117_173423.jsonl", 8)
    pcr_full = _capture_line("pcr_door_cycle_175845.jsonl", 18)
    pcr_com9_err = _capture_line("points_wide_184152.jsonl", 31)

    tracker.apply(parse_uplink(hb["topic"], hb["payload"]), received_at=1000.0)
    tracker.apply(parse_uplink(pcr_full["topic"], pcr_full["payload"]), received_at=1000.0)
    lab_1001 = _to_lab_state(tracker.state(now=1001.0))
    assert safety.check("pcr", "CloseDoor", {"WaitLid": False}, lab_1001)[0]

    for t in (1003.0, 1006.0, 1009.0, 1012.0):
        tracker.apply(parse_uplink(hb["topic"], hb["payload"]), received_at=t)
        tracker.apply(parse_uplink(pcr_com9_err["topic"], pcr_com9_err["payload"]), received_at=t)

    s_1003 = tracker.state(now=1003.0)
    assert "COM9 被其他程序占用" in (s_1003["PCR002"]["_error"] or "")
    assert s_1003["PCR002"]["_age_s"] == pytest.approx(3.0)
    ok_act, reason_act = safety.check("pcr", "CloseDoor", {"WaitLid": False}, _to_lab_state(s_1003))
    assert ok_act is False and "COM9" in reason_act
    assert safety.check("pcr", "GetPcrStatus", {}, _to_lab_state(s_1003))[0] is True

    s_1012 = tracker.state(now=1012.0)
    assert s_1012["PCR002"]["_age_s"] == pytest.approx(12.0)

    tracker.apply(parse_uplink(hb["topic"], hb["payload"]), received_at=1015.0)
    tracker.apply(parse_uplink(pcr_full["topic"], pcr_full["payload"]), received_at=1015.0)
    s_recovered = tracker.state(now=1015.0)
    assert s_recovered["PCR002"]["_error"] is None
    assert s_recovered["PCR002"]["_age_s"] == pytest.approx(0.0)


def test_gateway_offline_after_10s_in_points_wide_capture() -> None:
    tracker = TelemetryTracker()
    lines = (CAPTURES / "points_wide_184152.jsonl").read_text(encoding="utf-8").splitlines()

    last_gw1_hb_ts: float | None = None
    for idx, raw_line in enumerate(lines, 1):
        rec = json.loads(raw_line)
        ts = _tst_epoch(rec)
        tracker.apply(parse_uplink(rec["topic"], rec["payload"]), received_at=ts)
        if rec["topic"] == "lab/v1/gateway/GW_001/telemetry":
            last_gw1_hb_ts = ts
        if idx == 29:
            break

    assert last_gw1_hb_ts is not None
    assert tracker.state(now=last_gw1_hb_ts + 10.0)["_gateway"]["online"] is True
    assert tracker.state(now=last_gw1_hb_ts + 10.01)["_gateway"]["online"] is False

    for raw_line in lines[29:]:
        rec = json.loads(raw_line)
        ts = _tst_epoch(rec)
        tracker.apply(parse_uplink(rec["topic"], rec["payload"]), received_at=ts)
        if ts > last_gw1_hb_ts + 10.0:
            assert tracker.state(now=ts)["_gateway"]["online"] is False


def test_external_128s_transfer_stays_busy_throughout_and_unanswered_expires_at_300s(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#13: Recorded 128.18s transfer stays `busy_by_others=True`全程; unanswered expires at 300s."""
    assert EXTERNAL_REQUEST_EXPIRY_S == 300.0
    tracker = TelemetryTracker()

    # 1. Own WL- request (line 928) and GW_005 S- request (line 1003) do NOT set busy_by_others
    for line in (928, 1003):
        rec = _capture_line("lab_v1_20261117_173423.jsonl", line)
        tracker.apply(parse_uplink(rec["topic"], rec["payload"]), received_at=10.0)
    assert tracker.state(now=10.0)["_gateway"]["busy_by_others"] is False

    # 2. Replay the real 128.18s failed MovMulti (S-2122625672602984449, lines 1061..1459)
    all_lines = (CAPTURES / "lab_v1_20261117_173423.jsonl").read_text(encoding="utf-8").splitlines()
    start_ts = _tst_epoch(json.loads(all_lines[1060]))
    end_ts = _tst_epoch(json.loads(all_lines[1458]))
    assert end_ts - start_ts > 128.0

    for idx in range(1061, 1459):
        rec = json.loads(all_lines[idx - 1])
        ts = _tst_epoch(rec)
        tracker.apply(parse_uplink(rec["topic"], rec["payload"]), received_at=ts)
        assert tracker.state(now=ts)["_gateway"]["busy_by_others"] is True

    # Even past 120s (at +125s and at line 1458 +128.177s before motion/response), still busy!
    assert tracker.state(now=start_ts + 125.0)["_gateway"]["busy_by_others"] is True
    ts_1458 = _tst_epoch(json.loads(all_lines[1457]))
    assert tracker.state(now=ts_1458)["_gateway"]["busy_by_others"] is True

    # Line 1459 (`motion/response` rpc-result at +128.181s) finally clears `busy_by_others`
    rec_1459 = json.loads(all_lines[1458])
    tracker.apply(parse_uplink(rec_1459["topic"], rec_1459["payload"]), received_at=end_ts)
    assert tracker.state(now=end_ts)["_gateway"]["busy_by_others"] is False

    # 3. Unanswered vendor request (line 1869 X-2122626750576861186) expires at 300.0s
    l1869 = _capture_line("lab_v1_20261117_173423.jsonl", 1869)
    tracker.apply(parse_uplink(l1869["topic"], l1869["payload"]), received_at=200.0)
    assert tracker.state(now=499.9)["_gateway"]["busy_by_others"] is True

    with caplog.at_level(logging.WARNING, logger="wetlab.lab.mqtt.telemetry"):
        assert tracker.state(now=500.0)["_gateway"]["busy_by_others"] is False
    assert any("X-2122626750576861186" in msg and "expired" in msg for msg in caplog.messages)
