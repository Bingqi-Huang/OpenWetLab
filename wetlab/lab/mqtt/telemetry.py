"""Island telemetry state table, gateway health, and external command tracker (04 section 5.12)."""

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time
from typing import Any

from ..devices import BY_CODE, ISLAND
from .codec import DeviceTelemetry, ExternalRequest, GatewayHeartbeat, ParsedFrame, RpcResult

logger = logging.getLogger(__name__)

GATEWAY_HEARTBEAT_TIMEOUT_S = 10.0
EXTERNAL_REQUEST_EXPIRY_S = 300.0


@dataclass
class _PendingExternal:
    started_at: float
    result_topic: str
    device_code: str | None
    action: str | None


class TelemetryTracker:
    """Merges `GW_001` device telemetry and tracks gateway heartbeat and `busy_by_others`."""

    def __init__(self, *, now_fn: Callable[[], float] = time.time) -> None:
        self._now = now_fn
        self._lock = threading.Lock()
        self._props: dict[str, dict[str, Any]] = {dev.spec.CODE: {} for dev in ISLAND}
        self._last_complete_at: dict[str, float | None] = {dev.spec.CODE: None for dev in ISLAND}
        self._errors: dict[str, str | None] = {dev.spec.CODE: None for dev in ISLAND}
        self._last_heartbeat_at: float | None = None
        self._pending_external: dict[str, _PendingExternal] = {}

    def _prune_expired_locked(self, now: float) -> None:
        expired = [
            (req_id, entry)
            for req_id, entry in self._pending_external.items()
            if (now - entry.started_at) >= EXTERNAL_REQUEST_EXPIRY_S
        ]
        for req_id, entry in expired:
            del self._pending_external[req_id]
            logger.warning(
                "external request %s (%s.%s) expired after %.1fs without result",
                req_id,
                entry.device_code,
                entry.action,
                now - entry.started_at,
            )

    def apply(self, frame: ParsedFrame, *, received_at: float | None = None) -> None:
        """Apply one parsed uplink frame at local timestamp `received_at`."""
        ts = received_at if received_at is not None else self._now()
        with self._lock:
            self._prune_expired_locked(ts)

            if isinstance(frame, GatewayHeartbeat):
                self._last_heartbeat_at = ts
                return

            if isinstance(frame, DeviceTelemetry):
                dev = BY_CODE.get(frame.device_code)
                if dev is None:
                    return
                code = dev.spec.CODE
                err_val = frame.props.get("error")
                for key, val in frame.props.items():
                    if key == "error":
                        continue
                    if key.endswith("_error"):
                        if err_val is None and val is not None and str(val) != "":
                            err_val = val
                        continue
                    self._props[code][key] = val

                is_complete = all(
                    frame.props.get(prop) is not None for prop in dev.spec.STATE_PROPS
                )
                if is_complete:
                    self._last_complete_at[code] = ts

                if err_val is not None and str(err_val) != "":
                    self._errors[code] = str(err_val)
                elif is_complete:
                    self._errors[code] = None
                return

            if isinstance(frame, ExternalRequest):
                existing = self._pending_external.get(frame.request_id)
                if existing is None:
                    self._pending_external[frame.request_id] = _PendingExternal(
                        started_at=ts,
                        result_topic=frame.result_topic,
                        device_code=frame.device_code,
                        action=frame.action,
                    )
                elif frame.result_topic == "motion/response":
                    existing.result_topic = "motion/response"
                return

            if isinstance(frame, RpcResult):
                existing = self._pending_external.get(frame.request_id)
                if existing is not None and existing.result_topic == frame.result_topic:
                    del self._pending_external[frame.request_id]

    def state(self, *, now: float | None = None) -> dict[str, Any]:
        """Snapshot current island telemetry keyed by vendor `CODE` plus `_gateway`."""
        ts = now if now is not None else self._now()
        with self._lock:
            self._prune_expired_locked(ts)
            online = (
                self._last_heartbeat_at is not None
                and (ts - self._last_heartbeat_at) <= GATEWAY_HEARTBEAT_TIMEOUT_S
            )
            out: dict[str, Any] = {
                "_gateway": {
                    "online": online,
                    "busy_by_others": bool(self._pending_external),
                }
            }
            for dev in ISLAND:
                code = dev.spec.CODE
                last_at = self._last_complete_at[code]
                age_s = (ts - last_at) if last_at is not None else None
                out[code] = {
                    **self._props[code],
                    "_age_s": age_s,
                    "_error": self._errors[code],
                }
            return out
