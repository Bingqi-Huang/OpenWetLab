"""Tests for `MqttBackend`: `closed_skills`, `requestId` (#12), and timed replay (#4/#26)."""

from datetime import datetime
import json
from pathlib import Path
import time

from wetlab.lab import registry
from wetlab.lab.lab import Lab
from wetlab.lab.mqtt.backend import DEFAULT_CLOSED_SKILLS, MqttBackend
from wetlab.lab.mqtt.client import MqttClient
from wetlab.lab.store import island, records
from wetlab.lab.store.db import open_db

from .test_client import FakePahoClient

CAPTURES = Path(__file__).resolve().parents[3] / "captures"

EXPECTED_CLOSED_SKILLS = {
    "pcr_start_program",
    "pcr_confirm_finished",
    "pcr_stop",
    "shake_start",
    "shake_stop",
    "transfer",
    "rotate_turntable",
}


def _capture_lines(filename: str) -> list[dict]:
    raw = (CAPTURES / filename).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in raw if line.strip()]


def _capture_line(filename: str, lineno: int) -> dict:
    return _capture_lines(filename)[lineno - 1]


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


def test_closed_skills_exact_set_and_registry_not_available(tmp_path: Path) -> None:
    assert DEFAULT_CLOSED_SKILLS == EXPECTED_CLOSED_SKILLS
    b1 = MqttBackend(raw_dir=tmp_path / "raw1", env_file=tmp_path / "none.env")
    b2 = MqttBackend(raw_dir=tmp_path / "raw2", env_file=tmp_path / "none.env")
    assert b1._now is time.time and b1._sleep is time.sleep
    b1.closed_skills.remove("pcr_start_program")
    assert "pcr_start_program" in b2.closed_skills

    fake_paho = FakePahoClient()
    client = MqttClient(
        host="192.0.2.10",
        raw_dir=tmp_path / "raw",
        env_file=tmp_path / "none.env",
        now_fn=lambda: 1794909921.0,
        paho_client=fake_paho,
    )
    backend = MqttBackend(client=client, now_fn=lambda: 1794909921.0, sleep_fn=lambda _: None)
    for line in (8, 9, 10, 11, 12, 13):
        rec = _capture_line("lab_v1_20261117_173423.jsonl", line)
        backend.receive(rec["topic"], rec["payload"], received_at=1794909921.0)

    conn = open_db(":memory:")
    try:
        lab = Lab(backend, conn)
        for skill_name in sorted(EXPECTED_CLOSED_SKILLS):
            out = registry.call(lab, skill_name)
            assert out["status"] == "not_available"
            assert out["error"]["code"] == "NOT_AVAILABLE"
        assert fake_paho.published == []
    finally:
        conn.close()


def test_request_id_equals_operations_id_and_timed_replay_of_shake_pcr_open_pcr_close(
    tmp_path: Path,
) -> None:
    """#4, #12, #26: Timed replay of `test_session` + `pcr_door_cycle` via `registry.call`."""
    ts_lines = _capture_lines("test_session.jsonl")
    pcr_lines = _capture_lines("pcr_door_cycle_175845.jsonl")
    base_frames = [_capture_line("lab_v1_20261117_173423.jsonl", n) for n in (8, 9, 10, 11, 12, 13)]

    clock = [_tst(ts_lines[0])]
    last_keepalive = [clock[0]]
    scheduled: list[tuple[float, str, str]] = []
    backend: MqttBackend

    # Recorded command slices: (lines, req_lineno, res_lineno, end_lineno)
    scripts = {
        ("T002001", "GetStatus"): (ts_lines, 22, 30, 39),
        ("T002001", "SetClamp"): (ts_lines, 91, 96, 99),
        ("T002001", "SetTargetSpeed"): (ts_lines, 100, 105, 105),
        ("T002001", "StartShakeTimed"): (ts_lines, 106, 120, 129),
        ("PCR002", "SetLidMotor"): (pcr_lines, 2, 7, 9),
        ("PCR002", "OpenDoor"): (pcr_lines, 10, 16, 18),
        ("PCR002", "CloseDoor"): (pcr_lines, 19, 25, 28),
    }

    def _keepalive_gateway_and_idle_peers(up_to: float, active_code: str | None = None) -> None:
        while last_keepalive[0] + 3.0 <= up_to + 1e-9:
            last_keepalive[0] += 3.0
            for bf in base_frames:
                code = json.loads(bf["payload"]).get("DeviceCode")
                if bf["topic"].endswith("GW_001/telemetry") or code not in (
                    active_code,
                    "PCR002",
                    "T002001",
                ):
                    backend.receive(bf["topic"], bf["payload"], received_at=last_keepalive[0])

    def _drain_scheduled(up_to: float) -> None:
        scheduled.sort(key=lambda item: item[0])
        while scheduled and scheduled[0][0] <= up_to + 1e-9:
            at_ts, t, p = scheduled.pop(0)
            _keepalive_gateway_and_idle_peers(at_ts)
            backend.receive(t, p, received_at=at_ts)
        _keepalive_gateway_and_idle_peers(up_to)

    class _TimedReplayPaho(FakePahoClient):
        def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
            super().publish(topic, payload, qos, retain)
            pkt = json.loads(payload)
            dev_code = pkt["DeviceCode"]
            action = pkt["Type"]
            op_id = pkt["Body"]["requestId"]
            src, req_ln, res_ln, end_ln = scripts[(dev_code, action)]
            send_ts = clock[0]
            t_req = _tst(src[req_ln - 1])

            for ln in range(req_ln + 1, res_ln + 1):
                rec = src[ln - 1]
                if "/rpc/request/" in rec["topic"]:
                    continue
                at_ts = send_ts + (_tst(rec) - t_req)
                clock[0] = max(clock[0], at_ts)
                _keepalive_gateway_and_idle_peers(at_ts, active_code=dev_code)
                t_rw, p_rw = _rewrite_rpc(rec, op_id)
                backend.receive(t_rw, p_rw, received_at=at_ts)

            for ln in range(res_ln + 1, end_ln + 1):
                rec = src[ln - 1]
                if "/rpc/request/" in rec["topic"]:
                    continue
                at_ts = send_ts + (_tst(rec) - t_req)
                t_rw, p_rw = _rewrite_rpc(rec, op_id)
                scheduled.append((at_ts, t_rw, p_rw))

            if (dev_code, action) == ("T002001", "SetTargetSpeed"):
                shk_idle = ts_lines[98]
                scheduled.append((send_ts + 1.5, shk_idle["topic"], shk_idle["payload"]))
            # Keep non-acting PCR/shaker peer alive on 3s cadence
            peer = ts_lines[97] if dev_code == "T002001" else ts_lines[125]
            for extra in (1.5, 4.5, 7.5):
                scheduled.append((clock[0] + extra, peer["topic"], peer["payload"]))

    def _sleep(sec: float) -> None:
        target = clock[0] + max(0.0, sec)
        clock[0] = target
        _drain_scheduled(target)

    paho = _TimedReplayPaho()
    client = MqttClient(
        host="192.0.2.10",
        raw_dir=tmp_path / "raw",
        env_file=tmp_path / "none.env",
        now_fn=lambda: clock[0],
        paho_client=paho,
    )
    backend = MqttBackend(client=client, now_fn=lambda: clock[0], sleep_fn=_sleep)

    # Initial telemetry warm-up (all 5 devices + GW_001 heartbeat, PCR Closed+Pressed, shaker idle)
    for bf in base_frames:
        backend.receive(bf["topic"], bf["payload"], received_at=clock[0])
    backend.receive(ts_lines[27]["topic"], ts_lines[27]["payload"], received_at=clock[0])
    backend.receive(ts_lines[38]["topic"], ts_lines[38]["payload"], received_at=clock[0])

    conn = open_db(":memory:")
    try:
        lab = Lab(backend, conn)
        lab.materials.place("LW-001", "pcr_plate_96", "pcr")
        lab.materials.place("LW-002", "pcr_plate_96", "shaker")

        # 1. Read GetStatus where ws=1 clears 2.58s AFTER rpc-result (test_session.jsonl L22..L39)
        lab.do("shaker", "GetStatus")
        assert lab.state()["shaker"]["WorkingStatus"] == 0

        # 2. Immediately invoke `shake` -> `pcr_open` -> `pcr_close` via `registry.call`!
        r_shake = registry.call(lab, "shake", seconds=5, rpm=300)
        assert r_shake["ok"] is True, r_shake

        r_open = registry.call(lab, "pcr_open")
        assert r_open["ok"] is True, r_open
        assert r_open["state_after"]["pcr"]["DoorState"] == "Open"

        r_close = registry.call(lab, "pcr_close")
        assert r_close["ok"] is True, r_close
        assert r_close["state_after"]["pcr"]["DoorState"] == "Closed"
        assert r_close["state_after"]["pcr"]["LidMotorState"] == "Pressed"

        # #12: Every published MQTT packet's `requestId` equals `operations.id`!
        ops = records.list_operations(conn)
        assert len(ops) == len(paho.published) == 7
        for op, (_, raw_payload, _, _) in zip(ops, paho.published):
            pkt = json.loads(raw_payload)
            assert pkt["Body"]["requestId"] == op["id"]
        assert island.get_freeze(conn)["frozen"] is False
    finally:
        conn.close()
