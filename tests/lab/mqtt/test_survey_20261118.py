"""2026-11-18 survey capture tests (#32): stale first frame on StopShake/SingleLoop & codec."""

from datetime import datetime
import json
from pathlib import Path

from wetlab.lab import registry, safety
from wetlab.lab.lab import Lab
from wetlab.lab.mqtt.backend import MqttBackend
from wetlab.lab.mqtt.client import MqttClient
from wetlab.lab.mqtt.codec import UnrecognizedFrame, parse_uplink
from wetlab.lab.mqtt.telemetry import TelemetryTracker
from wetlab.lab.store import island, open_db

from .test_client import FakePahoClient

CAPTURES = Path(__file__).resolve().parents[3] / "captures"


def _lines(filename: str) -> list[dict]:
    raw = (CAPTURES / filename).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in raw if line.strip()]


def _tst(rec: dict) -> float:
    return datetime.fromisoformat(rec["tst"]).timestamp()


def _rewrite_rpc(rec: dict, op_id: str) -> tuple[str, str]:
    topic = rec["topic"]
    payload = json.loads(rec["payload"])
    if "/rpc/" in topic:
        parts = topic.split("/")
        parts[-1] = op_id
        topic = "/".join(parts)
        body = payload.get("Body") or {}
        if "ResponseId" in body:
            body["ResponseId"] = op_id
        if "requestId" in body:
            body["requestId"] = op_id
    return topic, json.dumps(payload, ensure_ascii=False)


def test_20261118_captures_zero_unrecognized_and_shaker_prop_error_detected() -> None:
    for fname in ("20261118_survey_110949.jsonl", "20261118_arm_141121.jsonl"):
        bad = []
        for idx, rec in enumerate(_lines(fname), 1):
            fr = parse_uplink(rec["topic"], rec["payload"])
            if isinstance(fr, UnrecognizedFrame):
                bad.append((fname, idx, fr.reason))
        assert bad == []

    survey = _lines("20261118_survey_110949.jsonl")
    tracker = TelemetryTracker()
    hb = survey[0]
    shk_ok = survey[1417]  # L1418 normal T002001 telemetry
    shk_disc = survey[30445]  # L30446 T002001 serial port closed (*_error fields)
    tracker.apply(parse_uplink(hb["topic"], hb["payload"]), received_at=100.0)
    tracker.apply(parse_uplink(shk_ok["topic"], shk_ok["payload"]), received_at=100.0)
    tracker.apply(parse_uplink(shk_disc["topic"], shk_disc["payload"]), received_at=103.0)

    st = tracker.state(now=103.0)["T002001"]
    assert "串口未打开" in (st["_error"] or "")
    lab_state = {
        "_gateway": {"online": True, "busy_by_others": False},
        "shaker": dict(st),
    }
    ok, reason = safety.check("shaker", "SetClamp", {"clamp": True}, lab_state)
    assert ok is False
    assert "串口未打开" in reason


def test_step4_waits_for_working_status_busy_to_clear_after_return(tmp_path: Path) -> None:
    """#32: Step 4 does not finish on a post-return frame that still has WorkingStatus=1."""
    survey = _lines("20261118_survey_110949.jsonl")
    clock = [_tst(survey[1428])]
    scheduled: list[tuple[float, str, str]] = []
    backend: MqttBackend

    class _BusyFirstFramePaho(FakePahoClient):
        def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
            super().publish(topic, payload, qos, retain)
            pkt = json.loads(payload)
            op_id = pkt["Body"]["requestId"]
            t_rw, p_rw = _rewrite_rpc(survey[1430], op_id)  # L1431 SetTargetSpeed rpc-result
            clock[0] += 0.03
            backend.receive(t_rw, p_rw, received_at=clock[0])
            # Frame 1 (+0.2s after return) STILL has WorkingStatus=1; Frame 2 (+1.4s) clears to 0
            busy_rec = survey[1470]  # L1471 WorkingStatus=1
            clear_rec = survey[1437]  # L1438 WorkingStatus=0
            scheduled.append((clock[0] + 0.2, busy_rec["topic"], busy_rec["payload"]))
            scheduled.append((clock[0] + 1.4, clear_rec["topic"], clear_rec["payload"]))

    def _sleep(sec: float) -> None:
        clock[0] += max(0.0, sec)
        scheduled.sort(key=lambda item: item[0])
        while scheduled and scheduled[0][0] <= clock[0] + 1e-9:
            at_ts, t, p = scheduled.pop(0)
            backend.receive(t, p, received_at=at_ts)

    paho = _BusyFirstFramePaho()
    client = MqttClient(
        host="192.0.2.10",
        raw_dir=tmp_path / "raw",
        env_file=tmp_path / "none.env",
        now_fn=lambda: clock[0],
        paho_client=paho,
    )
    backend = MqttBackend(client=client, now_fn=lambda: clock[0], sleep_fn=_sleep)
    for ln in (1414, 1415, 1416, 1417, 1427):
        rec = survey[ln]
        backend.receive(rec["topic"], rec["payload"], received_at=clock[0])
    # Seed gateway heartbeat
    hb = next(r for r in survey[:20] if r["topic"].endswith("GW_001/telemetry"))
    backend.receive(hb["topic"], hb["payload"], received_at=clock[0])

    conn = open_db(":memory:")
    try:
        lab = Lab(backend, conn)
        t0 = clock[0]
        lab.do("shaker", "SetTargetSpeed", {"rpm": 300})
        assert clock[0] - t0 >= 1.4
        assert lab.state()["shaker"]["WorkingStatus"] == 0
    finally:
        conn.close()


def test_timed_replay_20261118_stop_shake_and_single_loop_stale_first_frame(
    tmp_path: Path,
) -> None:
    """#32: Replay real 2026-11-18 `StartShake` -> `StopShake` and `SingleLoop`:
    when the first frame after `StopShake` return still reports 46 rpm & ElmState=1 (`L1479`),
    `Lab.do` neither raises `VERIFY_FAILED` nor returns early, succeeding on frame 2 (`L1486`).
    """
    survey = _lines("20261118_survey_110949.jsonl")
    hb = next(r for r in survey[:20] if r["topic"].endswith("GW_001/telemetry"))
    clock = [_tst(survey[1417])]
    last_hb = [clock[0]]
    scheduled: list[tuple[float, str, str]] = []
    backend: MqttBackend

    scripts = {
        ("T002001", "SetClamp"): (1419, 1421, 1428),
        ("T002001", "SetTargetSpeed"): (1429, 1431, 1438),
        ("T002001", "StartShake"): (1439, 1441, 1462),
        ("T002001", "StopShake"): (1463, 1472, 1486),
        ("STEERING_PLATFORM001", "SingleLoop"): (4277, 4279, 4293),
    }

    def _keep_hb(up_to: float) -> None:
        while last_hb[0] + 3.0 <= up_to + 1e-9:
            last_hb[0] += 3.0
            backend.receive(hb["topic"], hb["payload"], received_at=last_hb[0])

    class _SurveyReplayPaho(FakePahoClient):
        def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
            super().publish(topic, payload, qos, retain)
            pkt = json.loads(payload)
            key = (pkt["DeviceCode"], pkt["Type"])
            op_id = pkt["Body"]["requestId"]
            req_ln, res_ln, end_ln = scripts[key]
            send_ts = clock[0]
            t_req = _tst(survey[req_ln - 1])

            for ln in range(req_ln + 1, res_ln + 1):
                rec = survey[ln - 1]
                if "/rpc/request/" in rec["topic"]:
                    continue
                at_ts = send_ts + (_tst(rec) - t_req)
                clock[0] = max(clock[0], at_ts)
                _keep_hb(at_ts)
                t_rw, p_rw = _rewrite_rpc(rec, op_id)
                backend.receive(t_rw, p_rw, received_at=at_ts)

            for ln in range(res_ln + 1, end_ln + 1):
                rec = survey[ln - 1]
                if "/rpc/request/" in rec["topic"]:
                    continue
                at_ts = send_ts + (_tst(rec) - t_req)
                t_rw, p_rw = _rewrite_rpc(rec, op_id)
                scheduled.append((at_ts, t_rw, p_rw))

    def _sleep(sec: float) -> None:
        target = clock[0] + max(0.0, sec)
        clock[0] = target
        scheduled.sort(key=lambda item: item[0])
        while scheduled and scheduled[0][0] <= target + 1e-9:
            at_ts, t, p = scheduled.pop(0)
            _keep_hb(at_ts)
            backend.receive(t, p, received_at=at_ts)
        _keep_hb(target)

    paho = _SurveyReplayPaho()
    client = MqttClient(
        host="192.0.2.10",
        raw_dir=tmp_path / "raw",
        env_file=tmp_path / "none.env",
        now_fn=lambda: clock[0],
        paho_client=paho,
    )
    backend = MqttBackend(client=client, now_fn=lambda: clock[0], sleep_fn=_sleep)
    backend.closed_skills.clear()

    # Seed initial state frames (GW_001 heartbeat, PCR, arm, turntable=0, shaker=idle)
    backend.receive(hb["topic"], hb["payload"], received_at=clock[0])
    arm_idle = next(
        r for r in survey[:40] if r["topic"] == "lab/v1/devices/TYPE_ROBOT_ARM001/telemetry"
    )
    backend.receive(arm_idle["topic"], arm_idle["payload"], received_at=clock[0])
    for ln in (1415, 1418, 4275):
        rec = survey[ln - 1]
        backend.receive(rec["topic"], rec["payload"], received_at=clock[0])

    conn = open_db(":memory:")
    try:
        lab = Lab(backend, conn)
        lab.materials.place("LW-001", "96_plate", "shaker")

        # 1. shake_start: L1448 first frame is ShakeState=4 (60 rpm), L1455 (+5.7s) reaches 0!
        r_start = registry.call(lab, "shake_start", rpm=300)
        assert r_start["ok"] is True, r_start
        assert lab.state()["shaker"]["ShakeState"] == 0

        # 2. shake_stop: L1479 (+1.30s after return) has stale 46 rpm & ElmState=1;
        # L1486 (+4.31s after return, +6.58s after request) reaches 0 rpm & ElmState=2!
        t_before_stop = clock[0]
        stale_frame_offset = _tst(survey[1478]) - _tst(survey[1462])
        final_frame_offset = _tst(survey[1485]) - _tst(survey[1462])
        assert 3.0 < stale_frame_offset < 4.0
        assert final_frame_offset > 6.5

        r_stop = registry.call(lab, "shake_stop")
        assert r_stop["ok"] is True, r_stop
        # Proves it did NOT return early on L1479 (+3.58s) and waited for L1486 (+6.58s)!
        assert (clock[0] - t_before_stop) >= final_frame_offset
        assert lab.state()["shaker"]["ActualSpeed"] == 0
        assert lab.state()["shaker"]["ShakeState"] == 1
        assert lab.state()["shaker"]["ElmState"] == 2
        assert island.get_freeze(conn)["frozen"] is False

        # 3. rotate_turntable(90): L4285 has only WorkingStatus=0 (Angles still "0"), L4292 -> "90"!
        backend.receive(arm_idle["topic"], arm_idle["payload"], received_at=clock[0])
        t_before_tt = clock[0]
        tt_final_offset = _tst(survey[4291]) - _tst(survey[4276])
        r_tt = registry.call(lab, "rotate_turntable", angle=90)
        assert r_tt["ok"] is True, r_tt
        assert (clock[0] - t_before_tt) >= tt_final_offset
        assert lab.state()["turntable"]["Angles"] == "90"
    finally:
        conn.close()
