"""Single-shot RPC command dispatch and result matching (docs/spec/04 section 5.12)."""

from collections.abc import Callable
from dataclasses import dataclass
import threading
import time
from typing import Any

from ..devices import BY_CODE, gateway
from ..errors import DeviceFailed, NoResult
from .codec import RpcResult, build_request


@dataclass
class _PendingCall:
    expected_topic: str
    event: threading.Event
    result: RpcResult | None = None


def _expected_result_topic(device_code: str, action: str) -> str:
    dev = BY_CODE.get(device_code)
    dev_act = dev.spec.ACTIONS.get(action) if dev is not None else None
    act = dev_act or gateway.spec.ACTIONS.get(action)
    return act.result_topic if act is not None else "device"


class CommandDispatcher:
    """Publishes each command strictly once and waits for its matching `RpcResult`."""

    def __init__(
        self,
        publish_fn: Callable[[str, dict[str, Any]], None],
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._publish = publish_fn
        self._now = now_fn
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingCall] = {}

    def on_result(self, result: RpcResult) -> None:
        """Deliver a parsed `RpcResult` from the receive loop to any matching waiter."""
        with self._lock:
            pending = self._pending.get(result.request_id)
            if (
                pending is not None
                and pending.result is None
                and result.result_topic == pending.expected_topic
            ):
                pending.result = result
                pending.event.set()

    def call(
        self,
        device_code: str,
        action: str,
        params: dict[str, Any] | None,
        timeout_s: float,
        operation_id: str,
    ) -> dict[str, Any]:
        """Publish one RPC command (never retrying) using `operation_id` as `requestId`."""
        ts_ms = int(self._now() * 1000)
        expected_topic = _expected_result_topic(device_code, action)
        pending = _PendingCall(expected_topic=expected_topic, event=threading.Event())

        with self._lock:
            self._pending[operation_id] = pending

        try:
            topic, packet = build_request(
                device_code,
                action,
                params,
                operation_id,
                timeout_s,
                timestamp_ms=ts_ms,
            )
            self._publish(topic, packet)
            pending.event.wait(timeout=max(0.0, float(timeout_s)))
        finally:
            with self._lock:
                self._pending.pop(operation_id, None)

        res = pending.result
        if res is None:
            raise NoResult(
                f"{device_code}.{action} ({operation_id}): no result within {timeout_s}s"
            )
        if not res.ok:
            raise DeviceFailed(res.error or f"{device_code}.{action} failed")
        return res.data
