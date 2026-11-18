"""Tests for `wetlab.lab.mqtt.client` (`MqttClient`) using an in-memory fake paho client."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import pytest

from wetlab.lab.mqtt.client import SUBSCRIPTIONS, MqttClient
from wetlab.lab.mqtt.codec import DeviceTelemetry, GatewayHeartbeat, ParsedFrame, UnrecognizedFrame

CAPTURES = Path(__file__).resolve().parents[3] / "captures"


@dataclass
class _FakeMsg:
    topic: str
    payload: bytes
    qos: int = 0
    retain: bool = False


class FakePahoClient:
    """In-memory stand-in for `paho.mqtt.client.Client` that never touches sockets."""

    def __init__(self) -> None:
        self.auth: tuple[str, str] | None = None
        self.reconnect_delays: tuple[int, int] | None = None
        self.connected_to: tuple[str, int, int] | None = None
        self.loop_running = False
        self.disconnected = False
        self.subscriptions: list[list[tuple[str, int]]] = []
        self.published: list[tuple[str, str, int, bool]] = []
        self.on_connect: Any = None
        self.on_message: Any = None

    def username_pw_set(self, username: str, password: str) -> None:
        self.auth = (username, password)

    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None:
        self.reconnect_delays = (min_delay, max_delay)

    def connect(self, host: str, port: int, keepalive: int = 60) -> None:
        self.connected_to = (host, port, keepalive)

    def loop_start(self) -> None:
        self.loop_running = True

    def loop_stop(self) -> None:
        self.loop_running = False

    def disconnect(self) -> None:
        self.disconnected = True

    def subscribe(self, topics: list[tuple[str, int]]) -> None:
        self.subscriptions.append(list(topics))

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))


def _capture_line(filename: str, lineno: int) -> dict:
    lines = (CAPTURES / filename).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[lineno - 1])


def test_dotenv_config_subscriptions_on_reconnect_and_qos0_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("MQTT_HOST", "MQTT_PORT", "MQTT_USER", "MQTT_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "MQTT_HOST=192.0.2.10\nMQTT_PORT=1884\nMQTT_USER=lab_admin\nMQTT_PASSWORD='secret_value'\n",
        encoding="utf-8",
    )
    fake_paho = FakePahoClient()
    client = MqttClient(
        raw_dir=tmp_path / "raw",
        env_file=env_file,
        paho_client=fake_paho,
    )

    assert client.host == "192.0.2.10"
    assert client.port == 1884
    assert fake_paho.auth == ("lab_admin", "secret_value")
    assert fake_paho.reconnect_delays == (1, 10)

    client.start()
    assert fake_paho.connected_to == ("192.0.2.10", 1884, 30)
    assert fake_paho.loop_running is True

    # Initial connect + simulated auto-reconnect both restore all 6 required topic subscriptions
    fake_paho.on_connect(fake_paho, None, None, 0, None)
    fake_paho.on_connect(fake_paho, None, None, 0, None)
    expected_subs = [(topic, 0) for topic in SUBSCRIPTIONS]
    assert len(expected_subs) == 6
    assert fake_paho.subscriptions == [expected_subs, expected_subs]

    client.publish("lab/v1/devices/PCR002/rpc/request/WL-1", {"Type": "GetDoor"})
    assert len(fake_paho.published) == 1
    pub_topic, pub_payload, pub_qos, pub_retain = fake_paho.published[0]
    assert pub_topic == "lab/v1/devices/PCR002/rpc/request/WL-1"
    assert json.loads(pub_payload) == {"Type": "GetDoor"}
    assert pub_qos == 0
    assert pub_retain is False

    client.close()
    assert fake_paho.loop_running is False
    assert fake_paho.disconnected is True


def test_unrecognized_frames_archived_and_counted_without_stopping_subsequent_frames(
    tmp_path: Path,
) -> None:
    fake_paho = FakePahoClient()
    received_frames: list[ParsedFrame] = []
    fixed_ts = 1794908044.0  # 2026-11-17
    client = MqttClient(
        host="192.0.2.10",
        raw_dir=tmp_path / "raw",
        env_file=tmp_path / "nonexistent.env",
        on_frame=lambda frame, _ts: received_frames.append(frame),
        now_fn=lambda: fixed_ts,
        paho_client=fake_paho,
    )

    hb = _capture_line("lab_v1_20261117_173423.jsonl", 8)
    pcr_telem = _capture_line("lab_v1_20261117_173423.jsonl", 11)

    malformed_inputs: list[tuple[str, bytes]] = [
        ("lab/v1/devices/PCR002/telemetry", b"\xff\xfe\xfd"),
        ("lab/v1/devices/PCR002/telemetry", b"{truncated json"),
        ("lab/v1/devices/PCR002/telemetry", b"[1, 2, 3]"),
        ("lab/v1/devices/PCR002/telemetry", b'{"Type": "telemetry", "Body": {}}'),
        (
            "lab/v1/devices/PCR002/rpc/response/WL-9",
            b'{"GatewayCode": "GW_001", "Type": "rpc-result", "Body": {"ResponseId": "WL-9"}}',
        ),
        (
            "lab/v1/unknown/branch",
            b'{"GatewayCode": "GW_001", "Type": "mystery", "Body": {}}',
        ),
    ]

    # Deliver malformed frames via paho's `on_message` callback interleaved with real capture frames
    fake_paho.on_message(fake_paho, None, _FakeMsg(hb["topic"], hb["payload"].encode("utf-8")))
    for topic, raw_bytes in malformed_inputs:
        fake_paho.on_message(fake_paho, None, _FakeMsg(topic, raw_bytes))
    fake_paho.on_message(
        fake_paho, None, _FakeMsg(pcr_telem["topic"], pcr_telem["payload"].encode("utf-8"))
    )

    assert client.unrecognized_count == len(malformed_inputs)
    assert len(received_frames) == 2
    assert isinstance(received_frames[0], GatewayHeartbeat)
    assert isinstance(received_frames[1], DeviceTelemetry)
    assert received_frames[1].device_code == "PCR002"

    # Verify both daily raw JSONL and unrecognized archive JSONL were written
    daily_files = list((tmp_path / "raw").glob("2026-11-*.jsonl"))
    assert len(daily_files) == 1
    daily_lines = daily_files[0].read_text(encoding="utf-8").splitlines()
    assert len(daily_lines) == 2 + len(malformed_inputs)

    unrec_lines = (tmp_path / "raw" / "unrecognized.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(unrec_lines) == len(malformed_inputs)
    reasons = [json.loads(line)["reason"] for line in unrec_lines]
    assert "invalid utf-8" in reasons
    assert "invalid json" in reasons
    assert "missing GatewayCode" in reasons
    assert "rpc-result has neither Data nor code" in reasons
