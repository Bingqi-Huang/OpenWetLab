"""paho-mqtt transport wrapper, raw JSONL recorder, and unrecognized frame archive."""

from collections.abc import Callable
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt

from .codec import GATEWAY_CODE, GATEWAY_TELEMETRY_TOPIC, TOPIC_ROOT, ParsedFrame, UnrecognizedFrame, parse_uplink

logger = logging.getLogger(__name__)

SUBSCRIPTIONS: tuple[str, ...] = (
    GATEWAY_TELEMETRY_TOPIC,
    f"{TOPIC_ROOT}/devices/+/telemetry",
    f"{TOPIC_ROOT}/devices/+/rpc/request/+",
    f"{TOPIC_ROOT}/devices/+/rpc/response/+",
    f"{TOPIC_ROOT}/gateway/{GATEWAY_CODE}/motion/request/+",
    f"{TOPIC_ROOT}/gateway/{GATEWAY_CODE}/motion/response/+",
)

DEFAULT_PORT = 1883


def _read_dotenv(env_file: Path) -> dict[str, str]:
    if not env_file.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        values[key.strip()] = val
    return values


class MqttClient:
    """Thin paho-mqtt wrapper that archives all raw frames and isolates unrecognized packets."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        raw_dir: str | Path = Path("runs/raw"),
        env_file: str | Path = Path(".env"),
        on_frame: Callable[[ParsedFrame, float], None] | None = None,
        now_fn: Callable[[], float] = time.time,
        paho_client: Any | None = None,
    ) -> None:
        dotenv = _read_dotenv(Path(env_file))
        self.host = host if host is not None else (os.environ.get("MQTT_HOST") or dotenv.get("MQTT_HOST") or "")
        raw_port = (
            str(port)
            if port is not None
            else (os.environ.get("MQTT_PORT") or dotenv.get("MQTT_PORT") or str(DEFAULT_PORT))
        )
        self.port = int(raw_port)
        self.username = (
            username
            if username is not None
            else (os.environ.get("MQTT_USER") if "MQTT_USER" in os.environ else dotenv.get("MQTT_USER", ""))
        )
        self.password = (
            password
            if password is not None
            else (
                os.environ.get("MQTT_PASSWORD")
                if "MQTT_PASSWORD" in os.environ
                else dotenv.get("MQTT_PASSWORD", "")
            )
        )
        self.raw_dir = Path(raw_dir)
        self._on_frame = on_frame
        self._now = now_fn
        self._io_lock = threading.Lock()
        self._unrecognized_count = 0

        self._paho = paho_client if paho_client is not None else mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.username:
            self._paho.username_pw_set(self.username, self.password)
        self._paho.reconnect_delay_set(min_delay=1, max_delay=10)
        self._paho.on_connect = self._on_connect
        self._paho.on_message = self._on_message

    def set_frame_handler(self, handler: Callable[[ParsedFrame, float], None]) -> None:
        self._on_frame = handler

    @property
    def unrecognized_count(self) -> int:
        with self._io_lock:
            return self._unrecognized_count

    def start(self) -> None:
        """Connect to the configured MQTT broker and start the background network loop."""
        if not self.host:
            raise ValueError("MQTT_HOST is required to start MqttClient")
        self._paho.connect(self.host, self.port, keepalive=30)
        self._paho.loop_start()

    def close(self) -> None:
        """Stop the background network loop and disconnect cleanly."""
        self._paho.loop_stop()
        self._paho.disconnect()

    def publish(self, topic: str, packet: dict[str, Any]) -> None:
        """Publish one JSON command with QoS 0 and `retain=False` (01 section 3.3)."""
        encoded = json.dumps(packet, ensure_ascii=False)
        self._paho.publish(topic, encoded, qos=0, retain=False)

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        _ = userdata, flags, properties
        failed = getattr(reason_code, "is_failure", reason_code != 0)
        if failed:
            logger.error("MQTT connection refused: reason_code=%s", reason_code)
            return
        client.subscribe([(topic, 0) for topic in SUBSCRIPTIONS])

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        _ = client, userdata
        self.handle_raw_message(
            msg.topic,
            msg.payload,
            qos=int(getattr(msg, "qos", 0)),
            retain=bool(getattr(msg, "retain", False)),
        )

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def handle_raw_message(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        received_at: float | None = None,
    ) -> ParsedFrame:
        """Archive one raw MQTT message, parse it, and route or count unrecognized frames."""
        ts = received_at if received_at is not None else self._now()
        dt = datetime.fromtimestamp(ts).astimezone()
        tst = dt.isoformat()
        date_str = dt.strftime("%Y-%m-%d")

        if isinstance(payload, bytes):
            try:
                payload_text: str | None = payload.decode("utf-8")
            except UnicodeDecodeError:
                payload_text = None
        else:
            payload_text = payload

        raw_record: dict[str, Any] = {
            "tst": tst,
            "ts": ts,
            "topic": topic,
            "qos": qos,
            "retain": retain,
            "payload": payload_text if payload_text is not None else payload.decode("utf-8", errors="replace"),
        }
        if payload_text is None and isinstance(payload, bytes):
            raw_record["payload_hex"] = payload.hex()

        frame = parse_uplink(topic, payload)

        with self._io_lock:
            self._append_jsonl(self.raw_dir / f"{date_str}.jsonl", raw_record)
            if isinstance(frame, UnrecognizedFrame):
                self._unrecognized_count += 1
                self._append_jsonl(
                    self.raw_dir / "unrecognized.jsonl",
                    {
                        "tst": tst,
                        "ts": ts,
                        "topic": topic,
                        "raw": frame.raw,
                        "reason": frame.reason,
                    },
                )

        if isinstance(frame, UnrecognizedFrame):
            logger.warning("unrecognized MQTT frame on %s: %s", topic, frame.reason)
            return frame

        if self._on_frame is not None:
            self._on_frame(frame, ts)
        return frame
