"""Real GW_001 MQTT Backend implementation (docs/spec/04 section 5.12)."""

from collections.abc import Callable
from pathlib import Path
import time
from typing import Any

from ..backend import Backend
from .client import MqttClient
from .codec import ParsedFrame, RpcResult
from .commands import CommandDispatcher
from .telemetry import TelemetryTracker

DEFAULT_CLOSED_SKILLS: frozenset[str] = frozenset(
    {
        "pcr_start_program",
        "pcr_confirm_finished",
        "pcr_stop",
        "shake_start",
        "shake_stop",
        "transfer",
        "rotate_turntable",
    }
)


class MqttBackend:
    """Real island backend wiring `MqttClient`, `TelemetryTracker`, and `CommandDispatcher`."""

    def __init__(
        self,
        *,
        client: MqttClient | None = None,
        host: str | None = None,
        port: int | None = None,
        username: str | None = None,
        password: str | None = None,
        raw_dir: str | Path = Path("runs/raw"),
        env_file: str | Path = Path(".env"),
        now_fn: Callable[[], float] = time.time,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.closed_skills: set[str] = set(DEFAULT_CLOSED_SKILLS)
        self._now = now_fn
        self._sleep = sleep_fn

        self.client = (
            client
            if client is not None
            else MqttClient(
                host=host,
                port=port,
                username=username,
                password=password,
                raw_dir=raw_dir,
                env_file=env_file,
                now_fn=now_fn,
            )
        )
        self.telemetry = TelemetryTracker(now_fn=now_fn)
        self.commands = CommandDispatcher(self.client.publish, now_fn=now_fn)
        self.client.set_frame_handler(self._on_frame)

    def _on_frame(self, frame: ParsedFrame, received_at: float) -> None:
        self.telemetry.apply(frame, received_at=received_at)
        if isinstance(frame, RpcResult):
            self.commands.on_result(frame)

    @property
    def unrecognized_count(self) -> int:
        return self.client.unrecognized_count

    def start(self) -> None:
        """Connect to the MQTT broker and start background receive processing."""
        self.client.start()

    def close(self) -> None:
        """Disconnect from the MQTT broker cleanly."""
        self.client.close()

    def receive(
        self,
        topic: str,
        payload: bytes | str,
        *,
        qos: int = 0,
        retain: bool = False,
        received_at: float | None = None,
    ) -> ParsedFrame:
        """Feed one raw MQTT message through the full archive + parse + state/result pipeline."""
        return self.client.handle_raw_message(
            topic,
            payload,
            qos=qos,
            retain=retain,
            received_at=received_at,
        )

    def call(
        self,
        device_code: str,
        action: str,
        params: dict[str, Any],
        timeout_s: float,
        operation_id: str,
    ) -> dict[str, Any]:
        """Publish one low-level RPC command once and wait for its final result."""
        return self.commands.call(
            device_code,
            action,
            params,
            timeout_s,
            operation_id=operation_id,
        )

    def state(self) -> dict[str, Any]:
        """Return merged `GW_001` device telemetry and gateway health."""
        return self.telemetry.state(now=self.now())

    def now(self) -> float:
        """Return seconds since the epoch on this island's clock (`time.time`)."""
        return self._now()

    def sleep(self, seconds: float) -> None:
        """Sleep `seconds` on this island's clock (`time.sleep`)."""
        self._sleep(seconds)


_: type[Backend] = MqttBackend
